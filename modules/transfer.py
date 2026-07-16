"""
File and database transfer between HestiaCP and aaPanel servers.

Handles:
- SCP/rsync of web file archives
- SCP of MySQL dump files
- Parallel transfers with ThreadPoolExecutor
"""

import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .utils import log, console, create_progress


class TransferManager:
    """Manages file transfers between HestiaCP and aaPanel servers."""

    def __init__(
        self,
        hestia_host: str,
        hestia_port: int = 22,
        hestia_user: str = "root",
        hestia_ssh_key: Optional[str] = None,
        hestia_local: bool = False,
        aapanel_host: str = "",
        aapanel_port: int = 22,
        aapanel_user: str = "root",
        aapanel_ssh_key: Optional[str] = None,
        aapanel_local: bool = False,
        method: str = "rsync",
        max_workers: int = 4,
    ):
        self.hestia_host = hestia_host
        self.hestia_port = hestia_port
        self.hestia_user = hestia_user
        self.hestia_key = Path(hestia_ssh_key).expanduser() if hestia_ssh_key else None
        self.hestia_local = hestia_local
        self.aapanel_host = aapanel_host
        self.aapanel_port = aapanel_port
        self.aapanel_user = aapanel_user
        self.aapanel_key = Path(aapanel_ssh_key).expanduser() if aapanel_ssh_key else None
        self.aapanel_local = aapanel_local
        self.method = method
        self.max_workers = max_workers

    # ------------------------------------------------------------------
    # SSH options builder
    # ------------------------------------------------------------------

    def _ssh_opts(self, key: Optional[Path], port: int) -> List[str]:
        """Build SSH options for scp/rsync."""
        opts = [
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            "-p", str(port),
        ]
        if key and key.exists():
            opts.extend(["-i", str(key)])
        return opts

    def _scp(self, remote_host: str, remote_user: str, remote_path: str,
             local_path: str, key: Optional[Path], port: int,
             direction: str = "download") -> Tuple[bool, str]:
        """Transfer a file via SCP.

        Args:
            direction: 'download' (remote→local) or 'upload' (local→remote)
        """
        ssh_opts = self._ssh_opts(key, port)

        if direction == "download":
            src = f"{remote_user}@{remote_host}:{remote_path}"
            dst = local_path
        else:
            src = local_path
            dst = f"{remote_user}@{remote_host}:{remote_path}"

        # Build scp command with SSH options
        cmd = ["scp"]
        # scp -o Option needs to go before source/dest for each -o
        for i in range(0, len(ssh_opts), 2):
            if i + 1 < len(ssh_opts):
                cmd.extend([ssh_opts[i], ssh_opts[i + 1]])

        cmd.extend([src, dst])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode == 0:
                return True, ""
            else:
                return False, result.stderr.strip()
        except subprocess.TimeoutExpired:
            return False, "SCP timeout (10 min)"
        except Exception as e:
            return False, str(e)

    def _rsync(self, remote_host: str, remote_user: str, remote_path: str,
               local_path: str, key: Optional[Path], port: int,
               direction: str = "download") -> Tuple[bool, str]:
        """Transfer a file via rsync over SSH."""
        ssh_opts = " ".join(self._ssh_opts(key, port))
        rsh = f"ssh {ssh_opts}"

        if direction == "download":
            src = f"{remote_user}@{remote_host}:{remote_path}"
            dst = local_path
        else:
            src = local_path
            dst = f"{remote_user}@{remote_host}:{remote_path}"

        cmd = [
            "rsync", "-avz",
            "--partial",
            "--timeout=300",
            "-e", rsh,
            src, dst,
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode == 0:
                return True, ""
            else:
                return False, result.stderr.strip()
        except subprocess.TimeoutExpired:
            return False, "Rsync timeout"
        except Exception as e:
            return False, str(e)

    # ------------------------------------------------------------------
    # Transfer file
    # ------------------------------------------------------------------

    def transfer(
        self,
        remote_path: str,
        local_path: str,
        target_host: str = "",    # 'hestia' or 'aapanel'
        direction: str = "download",  # download (from hestia) or upload (to aapanel)
        local: bool = False,      # True if source/target is local (skip SCP, use cp)
    ) -> Tuple[bool, str]:
        """Transfer a single file between servers.

        For download: Hestia → local
        For upload: local → aaPanel

        When local=True, uses cp instead of SCP (running on that server).
        """
        # Skip if local file already exists (from previous run / resume)
        if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
            log.info(f"Skipping transfer, already exists: {os.path.basename(local_path)} ({os.path.getsize(local_path)} bytes)")
            return True, ""

        # Ensure local directory exists
        local_dir = os.path.dirname(local_path)
        if local_dir:
            os.makedirs(local_dir, exist_ok=True)

        # Local copy — no network transfer needed
        if local:
            try:
                if direction == "download":
                    src, dst = remote_path, local_path
                else:
                    src, dst = local_path, remote_path
                subprocess.run(["cp", src, dst], check=True, timeout=300)
                return True, ""
            except Exception as e:
                return False, str(e)

        if direction == "download":
            host = self.hestia_host
            user = self.hestia_user
            key = self.hestia_key
            port = self.hestia_port
        else:
            host = self.aapanel_host
            user = self.aapanel_user
            key = self.aapanel_key
            port = self.aapanel_port

        if self.method == "rsync":
            return self._rsync(host, user, remote_path, local_path, key, port, direction)
        else:
            return self._scp(host, user, remote_path, local_path, key, port, direction)

    # ------------------------------------------------------------------
    # Direct server-to-server rsync (skip intermediate temp)
    # ------------------------------------------------------------------

    def rsync_site_from_hestia(
        self,
        hestia_user: str,
        domain: str,
        aapanel_web_root: str,
    ) -> Tuple[bool, str]:
        """Rsync web files directly from HestiaCP to aaPanel web root.

        This avoids the double-transfer (Hestia→temp→aaPanel).
        Use when running on the aaPanel server itself.

        Rsyncs: Hestia:/home/{user}/web/{domain}/public_html/ → aapanel_web_root/
        And:    Hestia:/home/{user}/conf/web/{domain}/ → alongside web files
        """
        hestia_src = f"{self.hestia_user}@{self.hestia_host}:/home/{hestia_user}/web/{domain}/"
        dst = aapanel_web_root

        ssh_opts = " ".join(self._ssh_opts(self.hestia_key, self.hestia_port))
        rsh = f"ssh {ssh_opts}"

        # Ensure destination exists
        os.makedirs(dst, exist_ok=True)

        cmd = [
            "rsync", "-avz",
            "--partial",
            "--timeout=300",
            "--exclude=stats",
            "--exclude=logs",
            "-e", rsh,
            hestia_src,
            dst + "/",
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if result.returncode == 0:
                # Also rsync nginx/apache/php-fpm configs for reference
                conf_src = f"{self.hestia_user}@{self.hestia_host}:/home/{hestia_user}/conf/web/{domain}/"
                # Store configs in a subdirectory for reference
                conf_dst = os.path.join(aapanel_web_root, "_hestia_configs")
                os.makedirs(conf_dst, exist_ok=True)
                subprocess.run(
                    ["rsync", "-avz", "--partial", "--timeout=60", "-e", rsh, conf_src, conf_dst + "/"],
                    capture_output=True, text=True, timeout=120,
                )
                log.info(f"Direct rsync: {domain} → {aapanel_web_root}")
                return True, ""
            else:
                return False, result.stderr.strip()
        except subprocess.TimeoutExpired:
            return False, "Direct rsync timeout (10 min)"
        except Exception as e:
            return False, str(e)

    # ------------------------------------------------------------------
    # Batch transfers
    # ------------------------------------------------------------------

    def transfer_sites_batch(
        self,
        transfers: List[Dict[str, Any]],
        local_tmp_dir: str,
    ) -> Dict[str, Dict[str, Any]]:
        """Transfer multiple site archives + DB dumps in parallel.

        Args:
            transfers: List of dicts with keys:
                - domain: str
                - hestia_archive_path: str (remote tar.gz path on Hestia server)
                - hestia_db_dump_path: str (remote .sql path on Hestia server, optional)
            local_tmp_dir: Local directory to store downloaded files

        Returns:
            Dict mapping domain → {archive_local_path, db_dump_local_path, success, error}
        """
        results: Dict[str, Dict[str, Any]] = {}

        # Build list of all files to transfer
        tasks: List[Dict[str, Any]] = []
        for t in transfers:
            domain = t["domain"]
            results[domain] = {
                "archive_local_path": None,
                "db_dump_local_path": None,
                "success": True,
                "error": "",
            }

            archive_remote = t.get("hestia_archive_path")
            if archive_remote:
                local_archive = os.path.join(
                    local_tmp_dir, "archives", os.path.basename(archive_remote)
                )
                tasks.append({
                    "domain": domain,
                    "type": "archive",
                    "remote_path": archive_remote,
                    "local_path": local_archive,
                })

            db_dump_remote = t.get("hestia_db_dump_path")
            if db_dump_remote:
                local_db = os.path.join(
                    local_tmp_dir, "dumps", os.path.basename(db_dump_remote)
                )
                tasks.append({
                    "domain": domain,
                    "type": "db_dump",
                    "remote_path": db_dump_remote,
                    "local_path": local_db,
                })

        if not tasks:
            return results

        with create_progress() as progress:
            task_id = progress.add_task(
                "[cyan]Transferring files...", total=len(tasks)
            )

            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {}
                for task in tasks:
                    future = executor.submit(
                        self.transfer,
                        task["remote_path"],
                        task["local_path"],
                        direction="download",
                        local=self.hestia_local,
                    )
                    futures[future] = task

                for future in as_completed(futures):
                    task = futures[future]
                    domain = task["domain"]
                    try:
                        success, error = future.result()
                        if success:
                            if task["type"] == "archive":
                                results[domain]["archive_local_path"] = task["local_path"]
                            elif task["type"] == "db_dump":
                                results[domain]["db_dump_local_path"] = task["local_path"]
                        else:
                            results[domain]["success"] = False
                            results[domain]["error"] += f"{task['type']}: {error}; "
                            log.error(f"Transfer failed for {domain}/{task['type']}: {error}")
                    except Exception as e:
                        results[domain]["success"] = False
                        results[domain]["error"] += f"{task['type']}: {e}; "
                        log.error(f"Transfer exception for {domain}: {e}")

                    progress.advance(task_id)

        return results
