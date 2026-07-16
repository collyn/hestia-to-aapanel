"""
aaPanel server operations via SSH or local execution.

Handles file system operations, MySQL database import,
SSL certificate file deployment, crontab management, and
post-migration verification on the aaPanel server.

Supports two modes:
- SSH mode: connects to a remote aaPanel server
- Local mode: runs commands directly (when script runs on the aaPanel server)
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .utils import log, console

# paramiko is optional (only needed for SSH mode)
try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False


class AAPanelSSH:
    """Client wrapper for aaPanel server (SSH or local)."""

    # aaPanel path constants
    WWW_ROOT = "/www/wwwroot"
    NGINX_VHOST = "/www/server/panel/vhost/nginx"
    APACHE_VHOST = "/www/server/panel/vhost/apache"
    CERT_DIR = "/www/server/panel/vhost/cert"
    PANEL_DIR = "/www/server/panel"

    def __init__(
        self,
        host: str = "localhost",
        port: int = 22,
        user: str = "root",
        password: Optional[str] = None,
        ssh_key: Optional[str] = None,
        tmp_dir: str = "/tmp/aapanel_migration",
        local: bool = False,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.ssh_key = Path(ssh_key).expanduser() if ssh_key else None
        self.tmp_dir = tmp_dir
        self.local = local
        self._client = None  # paramiko SSHClient (only when local=False)
        self._sftp = None     # paramiko SFTPClient (only when local=False)

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self):
        """Establish SSH connection (no-op in local mode)."""
        if self.local:
            # Verify aaPanel installation is accessible locally
            if not Path(self.PANEL_DIR).exists():
                raise RuntimeError(
                    f"aaPanel not found at {self.PANEL_DIR}. "
                    "Are you sure this is an aaPanel server? "
                    "Set aapanel.local=false to use remote SSH."
                )
            log.info("Local mode: running on aaPanel server")
            return

        if not HAS_PARAMIKO:
            raise RuntimeError("paramiko is required for SSH mode. Install: pip install paramiko")

        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: Dict[str, Any] = {
            "hostname": self.host,
            "port": self.port,
            "username": self.user,
            "timeout": 30,
        }
        if self.password:
            connect_kwargs["password"] = self.password
        elif self.ssh_key and self.ssh_key.exists():
            connect_kwargs["key_filename"] = str(self.ssh_key)
        else:
            connect_kwargs["allow_agent"] = True

        self._client.connect(**connect_kwargs)
        self._sftp = self._client.open_sftp()
        log.info(f"Connected to aaPanel server: {self.host}:{self.port}")

    def disconnect(self):
        """Close SSH connection (no-op in local mode)."""
        if self.local:
            return
        if self._sftp:
            self._sftp.close()
        if self._client:
            self._client.close()
        log.info("Disconnected from aaPanel server")

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()

    # ------------------------------------------------------------------
    # Raw execution
    # ------------------------------------------------------------------

    def exec(self, command: str, timeout: int = 120, warn_on_error: bool = True) -> Tuple[int, str, str]:
        """Execute command. Uses local subprocess or SSH depending on mode.
        Returns (exit_code, stdout, stderr).

        Args:
            warn_on_error: If False, non-zero exit codes are NOT logged as warnings.
        """
        log.debug(f"Exec ({'local' if self.local else 'ssh'}): {command[:120]}...")

        if self.local:
            try:
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                code = result.returncode
                out = result.stdout.strip()
                err = result.stderr.strip()
            except subprocess.TimeoutExpired:
                code, out, err = 124, "", "Command timed out"
            except Exception as e:
                code, out, err = 1, "", str(e)
        else:
            if self._client is None:
                raise RuntimeError("Not connected. Call connect() first.")
            stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
            code = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")

        if code != 0 and warn_on_error:
            log.warning(f"Command exited {code}: {command[:100]}")
            if err:
                log.warning(f"stderr: {err[:300]}")

        return code, out, err

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def ensure_dir(self, path: str):
        """Create directory if it doesn't exist."""
        self.exec(f"mkdir -p {path}")

    def file_exists(self, path: str) -> bool:
        """Check if a file exists on the server."""
        exit_code, _, _ = self.exec(f"test -e {path}", warn_on_error=False)
        return exit_code == 0

    def create_web_root(self, domain: str) -> str:
        """Create the /www/wwwroot/{domain}/ directory."""
        path = f"{self.WWW_ROOT}/{domain}"
        self.ensure_dir(path)
        # aaPanel typically expects www:www ownership
        self.exec(f"chown -R www:www {path} 2>/dev/null || chown -R www-data:www-data {path} 2>/dev/null || true")
        log.info(f"Created web root: {path}")
        return path

    # ------------------------------------------------------------------
    # Database import
    # ------------------------------------------------------------------

    def import_mysql_dump(self, dump_path: str, db_name: str) -> bool:
        """Import a MySQL dump file into a database.

        Uses mysql command. The database must already exist in aaPanel.
        """
        if not self.file_exists(dump_path):
            log.error(f"Dump file not found: {dump_path}")
            return False

        # Try mysql with root credentials
        cmd = f"mysql {db_name} < {dump_path} 2>&1"
        exit_code, stdout, stderr = self.exec(cmd, timeout=600)

        if exit_code != 0:
            log.error(f"MySQL import failed for {db_name}: {stderr}")
            return False

        log.info(f"Imported MySQL dump into: {db_name}")
        return True

    def db_connection_test(self, db_name: str, db_user: str, db_pass: str) -> bool:
        """Test MySQL database connection with given credentials."""
        cmd = (
            f"mysql -u{db_user} -p'{db_pass}' {db_name} "
            f"-e 'SELECT 1 AS test;' 2>&1"
        )
        exit_code, stdout, _ = self.exec(cmd)
        success = exit_code == 0 and "test" in stdout
        if success:
            log.info(f"DB connection OK: {db_name} as {db_user}")
        else:
            log.error(f"DB connection FAILED: {db_name} as {db_user}")
        return success

    # ------------------------------------------------------------------
    # SSL certificate file deployment
    # ------------------------------------------------------------------

    def deploy_ssl_files(self, domain: str, cert_pem: str, key_pem: str) -> bool:
        """Write SSL certificate files to aaPanel's cert directory.

        aaPanel stores certs at:
          /www/server/panel/vhost/cert/{domain}/fullchain.pem
          /www/server/panel/vhost/cert/{domain}/privkey.pem
        """
        cert_path = f"{self.CERT_DIR}/{domain}"
        self.ensure_dir(cert_path)

        fullchain_file = f"{cert_path}/fullchain.pem"
        privkey_file = f"{cert_path}/privkey.pem"

        # Write files (local or via SFTP)
        try:
            if self.local:
                with open(fullchain_file, "w") as f:
                    f.write(cert_pem)
                with open(privkey_file, "w") as f:
                    f.write(key_pem)
            else:
                with self._sftp.open(fullchain_file, "w") as f:
                    f.write(cert_pem)
                with self._sftp.open(privkey_file, "w") as f:
                    f.write(key_pem)

            self.exec(f"chmod 600 {cert_path}/*.pem")
            log.info(f"Deployed SSL files for: {domain}")
            return True
        except Exception as e:
            log.error(f"Failed to write SSL files for {domain}: {e}")
            return False

    # ------------------------------------------------------------------
    # Crontab management
    # ------------------------------------------------------------------

    def import_cron_jobs(self, cron_jobs: List[Dict[str, Any]]) -> bool:
        """Import cron jobs into root crontab.

        Args:
            cron_jobs: List of cron job dicts with keys: MIN, HOUR, DAY, MONTH, WDAY, CMD
        """
        if not cron_jobs:
            log.info("No cron jobs to import")
            return True

        entries = []
        for job in cron_jobs:
            minute = job.get("MIN", "*")
            hour = job.get("HOUR", "*")
            day = job.get("DAY", "*")
            month = job.get("MONTH", "*")
            wday = job.get("WDAY", "*")
            cmd = job.get("CMD", "")
            if not cmd:
                continue
            # Format: MIN HOUR DAY MONTH WDAY COMMAND
            entries.append(f"{minute} {hour} {day} {month} {wday} {cmd}")

        if not entries:
            return True

        # Get existing crontab (may fail if no crontab exists yet)
        exit_code, existing, _ = self.exec("crontab -l 2>/dev/null", warn_on_error=False)
        if exit_code != 0:
            existing = ""

        # Append new entries (avoiding duplicates)
        new_content = existing.strip()
        for entry in entries:
            if entry not in new_content:
                new_content += f"\n{entry}"

        new_content += "\n"

        # Write new crontab
        self.exec(f"echo '{new_content}' | crontab -")
        log.info(f"Imported {len(entries)} cron jobs")
        return True

    # ------------------------------------------------------------------
    # Service management
    # ------------------------------------------------------------------

    def restart_nginx(self):
        """Restart nginx to apply config changes."""
        self.exec("service nginx reload 2>/dev/null || nginx -s reload 2>/dev/null")
        log.info("Nginx reloaded")

    def restart_php_fpm(self, version: Optional[str] = None):
        """Restart PHP-FPM service."""
        if version:
            self.exec(f"service php{version}-fpm reload 2>/dev/null || true")
        else:
            self.exec("service php-fpm reload 2>/dev/null || service php*-fpm reload 2>/dev/null || true")
        log.info("PHP-FPM reloaded")

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def http_check(self, domain: str, use_https: bool = False, timeout: int = 30) -> Tuple[bool, int, str]:
        """Perform HTTP request to verify site accessibility.

        Returns (success, http_code, error_message).
        """
        scheme = "https" if use_https else "http"
        cmd = (
            f"curl -sS -o /dev/null -w '%{{http_code}}' "
            f"--max-time {timeout} "
            f"-H 'Host: {domain}' "
            f"{scheme}://127.0.0.1/"
        )
        exit_code, stdout, stderr = self.exec(cmd, timeout=timeout + 5)

        if exit_code != 0:
            return False, 0, f"curl failed: {stderr}"

        http_code = int(stdout.strip()) if stdout.strip().isdigit() else 0
        if 200 <= http_code < 400:
            log.info(f"HTTP OK: {domain} → {http_code}")
            return True, http_code, ""
        else:
            return False, http_code, f"HTTP {http_code}"

    def ssl_check(self, domain: str) -> Dict[str, Any]:
        """Check SSL certificate validity for a domain.

        Returns dict with: valid, issuer, expires, error.
        """
        cmd = (
            f"echo | openssl s_client -servername {domain} "
            f"-connect 127.0.0.1:443 2>/dev/null | "
            f"openssl x509 -noout -issuer -enddate 2>/dev/null"
        )
        exit_code, stdout, _ = self.exec(cmd, timeout=30)

        result: Dict[str, Any] = {"valid": False, "issuer": "", "expires": "", "error": ""}

        if exit_code != 0 or not stdout:
            result["error"] = "SSL handshake or cert parse failed"
            return result

        for line in stdout.split("\n"):
            if line.startswith("issuer="):
                result["issuer"] = line.replace("issuer=", "").strip()
            elif line.startswith("notAfter="):
                result["expires"] = line.replace("notAfter=", "").strip()

        result["valid"] = bool(result["issuer"] and result["expires"])
        if result["valid"]:
            log.info(f"SSL OK: {domain} (expires: {result['expires']})")
        else:
            log.warning(f"SSL check failed for {domain}: {result.get('error')}")

        return result

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup_temp(self):
        """Remove temporary files on aaPanel server."""
        self.exec(f"rm -rf {self.tmp_dir}")
