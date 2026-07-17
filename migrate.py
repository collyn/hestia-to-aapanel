#!/usr/bin/env python3
"""
HestiaCP → aaPanel Migration Tool

Migrates websites, databases, SSL certificates, DNS records, mail accounts,
and cron jobs from a HestiaCP server to an aaPanel Community server.

Usage:
    python migrate.py --config config.yaml              # Full migration
    python migrate.py --config config.yaml --dry-run    # Preview only
    python migrate.py --config config.yaml --resume     # Resume after interruption
    python migrate.py --config config.yaml --rollback   # Remove migrated sites

Requirements:
    pip install -r requirements.txt
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from modules.utils import (
    console,
    log,
    LOG_FILE,
    MigrationState,
    create_progress,
    print_banner,
    print_summary,
    print_error_context,
    print_success,
)
from modules.hestia import HestiaClient
from modules.aapanel_api import AAPanelAPI, AAPanelAPIError
from modules.aapanel_ssh import AAPanelSSH
from modules.transfer import TransferManager
from modules.transformers import DataTransformer


# ======================================================================
# Main Migrator Class
# ======================================================================

class HestiaToAAPanelMigrator:
    """Orchestrates the complete migration workflow."""

    def __init__(self, config_path: str):
        self.config = self._load_config(config_path)
        self.dry_run = self.config.get("migration", {}).get("dry_run", False)
        self.state = MigrationState()

        # Initialize components (lazy — connect when needed)
        self.hestia: Optional[HestiaClient] = None
        self.api: Optional[AAPanelAPI] = None
        self.ssh: Optional[AAPanelSSH] = None
        self.transfer_mgr: Optional[TransferManager] = None
        self.transformer: Optional[DataTransformer] = None

        # Local temp dir for intermediate files
        self.local_tmp = Path(tempfile.mkdtemp(prefix="hestia2aa_"))

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def _load_config(self, path: str) -> Dict[str, Any]:
        """Load YAML configuration file."""
        config_path = Path(path)
        if not config_path.exists():
            console.print(f"[red]Config file not found: {path}[/red]")
            sys.exit(1)

        with open(config_path) as f:
            config = yaml.safe_load(f)

        # Allow env var overrides
        if os.getenv("AA_PANEL_API_KEY"):
            config.setdefault("aapanel", {})["api_key"] = os.getenv("AA_PANEL_API_KEY")

        return config

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def setup(self):
        """Initialize all connections and components."""
        hestia_cfg = self.config["hestia"]
        aapanel_cfg = self.config["aapanel"]
        migration_cfg = self.config.get("migration", {})

        # Detect if we're running on one of the servers
        hestia_local = hestia_cfg.get("local", False)
        aapanel_local = aapanel_cfg.get("local", False)

        # Hestia client (local or SSH)
        self.hestia = HestiaClient(
            host=hestia_cfg.get("host", "localhost"),
            port=hestia_cfg.get("port", 22),
            user=hestia_cfg.get("user", "root"),
            password=hestia_cfg.get("password"),
            ssh_key=hestia_cfg.get("ssh_key"),
            hestia_path=hestia_cfg.get("hestia_path", "/usr/local/hestia"),
            tmp_dir=migration_cfg.get("hestia_tmp_dir", "/tmp/hestia_migration"),
            local=hestia_local,
        )

        # aaPanel API client
        panel_url = aapanel_cfg["panel_url"]
        if aapanel_local:
            from urllib.parse import urlparse
            parsed = urlparse(panel_url)
            # Preserve the scheme (http/https) from user's config
            scheme = parsed.scheme or "https"
            port = parsed.port or 8888

            if "127.0.0.1" in panel_url or "localhost" in panel_url:
                pass  # User already configured local URL — keep it
            else:
                panel_url = f"{scheme}://127.0.0.1:{port}"
                console.print(f"[cyan]Local mode: using {panel_url} (scheme={scheme}, port={port})[/cyan]")

        self.api = AAPanelAPI(
            panel_url=panel_url,
            api_key=aapanel_cfg["api_key"],
        )

        # aaPanel SSH client (local or SSH)
        self.ssh = AAPanelSSH(
            host=aapanel_cfg.get("host", "localhost"),
            port=aapanel_cfg.get("port", 22),
            user=aapanel_cfg.get("user", "root"),
            password=aapanel_cfg.get("password"),
            ssh_key=aapanel_cfg.get("ssh_key"),
            tmp_dir=migration_cfg.get("aapanel_tmp_dir", "/tmp/aapanel_migration"),
            local=aapanel_local,
            mysql_root_password=aapanel_cfg.get("mysql_root_password", ""),
        )

        # Transfer manager
        self.transfer_mgr = TransferManager(
            hestia_host=hestia_cfg.get("host", "localhost"),
            hestia_port=hestia_cfg.get("port", 22),
            hestia_user=hestia_cfg.get("user", "root"),
            hestia_ssh_key=hestia_cfg.get("ssh_key"),
            hestia_local=hestia_local,
            aapanel_host=aapanel_cfg.get("host", "localhost"),
            aapanel_port=aapanel_cfg.get("port", 22),
            aapanel_user=aapanel_cfg.get("user", "root"),
            aapanel_ssh_key=aapanel_cfg.get("ssh_key"),
            aapanel_local=aapanel_local,
            method=migration_cfg.get("transfer_method", "rsync"),
            max_workers=migration_cfg.get("parallel_workers", 4),
        )

        # Data transformer
        self.transformer = DataTransformer(
            php_mapping=self.config.get("php_mapping"),
            domain_map=migration_cfg.get("domain_map"),
            default_quota=self.config.get("mail", {}).get("default_quota", "5 GB"),
        )

    # ------------------------------------------------------------------
    # Sites cache (avoid re-extracting on resume)
    # ------------------------------------------------------------------

    def _sites_cache_path(self) -> Path:
        return Path(__file__).resolve().parent / "sites_cache.json"

    def _load_sites_cache(self) -> Optional[List[Dict[str, Any]]]:
        """Load previously extracted sites from cache file."""
        path = self._sites_cache_path()
        if not path.exists():
            return None
        try:
            with open(path) as f:
                data = json.load(f)
            # Validate it's not empty and has the right structure
            if isinstance(data, list) and len(data) > 0 and "domain" in data[0]:
                log.info(f"Loaded {len(data)} sites from cache")
                return data
        except (json.JSONDecodeError, KeyError):
            log.warning("Corrupted sites cache, ignoring")
        return None

    def _save_sites_cache(self, sites: List[Dict[str, Any]]):
        """Save extracted sites to cache file (without SSL cert content — too large)."""
        # Keep ssl_certs — they're small text (PEM), needed for Phase 3
        cache_data = []
        for s in sites:
            cache_data.append(s)

        path = self._sites_cache_path()
        with open(path, "w") as f:
            json.dump(cache_data, f, indent=2, default=str)
        log.info(f"Saved {len(cache_data)} sites to cache: {path}")

    # ------------------------------------------------------------------
    # Phase 1: Extract
    # ------------------------------------------------------------------

    def phase_extract(self) -> List[Dict[str, Any]]:
        """Extract all site data from HestiaCP server."""
        console.print("\n[bold cyan]Phase 1: Extracting data from HestiaCP...[/bold cyan]")

        self.hestia.connect()
        try:
            workers = self.config.get("migration", {}).get("parallel_workers", 8)
            sites = self.hestia.extract_all(max_workers=workers)
        finally:
            self.hestia.disconnect()

        # Apply filters: only_domains (whitelist) takes priority over exclude
        only_domains = self.config.get("migration", {}).get("only_domains", [])
        only_domains_file = self.config.get("migration", {}).get("only_domains_file", "")

        # Load domains from file if specified
        if only_domains_file:
            file_path = Path(only_domains_file)
            if file_path.exists():
                with open(file_path) as f:
                    file_domains = [line.strip() for line in f if line.strip() and not line.startswith("#")]
                only_domains = list(set(only_domains + file_domains))
                console.print(f"[cyan]Loaded {len(file_domains)} domains from {only_domains_file}[/cyan]")
            else:
                log.error(f"Domains file not found: {only_domains_file}")

        exclude_domains = set(
            self.config.get("migration", {}).get("exclude_domains", [])
        )
        exclude_users = set(
            self.config.get("migration", {}).get("exclude_users", [])
        )

        if only_domains:
            only_set = set(only_domains)
            sites = [s for s in sites if s.get("domain") in only_set]
            console.print(f"[cyan]Filtered to {len(sites)} domains[/cyan]")
        else:
            sites = [
                s for s in sites
                if s.get("domain") not in exclude_domains
                and s.get("user") not in exclude_users
            ]

        # Filter out failed extractions
        failed = [s for s in sites if s.get("status") == "extraction_failed"]
        sites = [s for s in sites if s.get("status") != "extraction_failed"]

        if failed:
            console.print(f"[yellow]⚠ {len(failed)} sites failed extraction[/yellow]")
            for f in failed:
                console.print(f"  - {f['user']}/{f['domain']}: {f.get('error')}")

        self.state.data["total_sites"] = len(sites)
        self.state.data["started_at"] = datetime.now().isoformat()
        self.state.data["current_phase"] = "extract_done"
        self.state.save()

        console.print(f"[green]✓ Extracted {len(sites)} sites successfully[/green]")
        return sites

    # ------------------------------------------------------------------
    # Phase 2: Transfer files and databases
    # ------------------------------------------------------------------

    def phase_transfer(self, sites: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Transfer web files and database dumps from HestiaCP to local.

        Databases are deduplicated: each unique database is dumped exactly once,
        even if referenced by multiple sites belonging to the same user.
        """
        console.print("\n[bold cyan]Phase 2: Transferring files and databases...[/bold cyan]")

        if self.dry_run:
            console.print("[yellow]DRY RUN: Skipping actual transfers[/yellow]")
            return {}

        do_databases = self.config.get("migration", {}).get("databases", True)
        aapanel_local = self.config.get("aapanel", {}).get("local", False)

        # ---- Step 0: Collect unique databases across all sites ----
        unique_dbs: Dict[str, str] = {}  # db_name → user
        if do_databases:
            for site in sites:
                for db in site.get("databases", []):
                    db_name = db.get("DATABASE") or db.get("DB") or db.get("database", "")
                    if db_name and db_name not in unique_dbs:
                        unique_dbs[db_name] = site["user"]

        if unique_dbs:
            console.print(f"Found [cyan]{len(unique_dbs)} unique databases[/cyan] to dump")

        # ---- Step 1: Create archives + dump databases on HestiaCP ----
        self.hestia.connect()

        # Dump each unique database ONCE
        db_dump_map: Dict[str, str] = {}  # db_name → remote_dump_path
        if unique_dbs:
            console.print("Dumping databases (deduplicated)...")
            with create_progress() as progress:
                db_task = progress.add_task(
                    "[cyan]Dumping databases...", total=len(unique_dbs)
                )
                for db_name, user in unique_dbs.items():
                    try:
                        # Check if dump already exists (from previous run / resume)
                        existing = self.hestia.find_existing_dump(db_name)
                        if existing:
                            db_dump_map[db_name] = existing
                        else:
                            dump_path, filename = self.hestia.dump_database(db_name)
                            db_dump_map[db_name] = dump_path
                        progress.advance(db_task)
                    except Exception as e:
                        log.error(f"Failed to dump database '{db_name}' (user={user}): {e}")
                        progress.advance(db_task)

        # Archive web files (skip if running on aaPanel — Phase 3 uses direct rsync)
        transfers = []
        if not aapanel_local:
            console.print(f"\nArchiving web files for {len(sites)} sites...")
            with create_progress() as progress:
                site_task = progress.add_task(
                    "[cyan]Archiving sites...", total=len(sites)
                )
                for site in sites:
                    domain = site["domain"]
                    user = site["user"]
                    entry: Dict[str, Any] = {"domain": domain}

                    try:
                        existing = self.hestia.find_existing_archive(domain)
                        if existing:
                            entry["hestia_archive_path"] = existing
                        else:
                            archive_path, _ = self.hestia.archive_web_files(user, domain)
                            entry["hestia_archive_path"] = archive_path
                    except Exception as e:
                        log.error(f"Failed to archive {domain}: {e}")
                        entry["hestia_archive_path"] = None

                    # Reference already-dumped databases
                    dbs = site.get("databases", [])
                    if dbs and do_databases:
                        db_dumps = []
                        for db in dbs:
                            db_name = (db.get("DATABASE") or db.get("DB") or db.get("database", ""))
                            if db_name and db_name in db_dump_map:
                                db_dumps.append({
                                    "db_name": db_name,
                                    "dump_path": db_dump_map[db_name],
                                })
                        if db_dumps:
                            entry["hestia_db_dump_path"] = db_dumps[0]["dump_path"]
                            entry["_db_dumps"] = db_dumps

                    transfers.append(entry)
                    progress.advance(site_task)
        else:
            # On aaPanel: skip archiving, create lightweight transfer entries
            console.print("[cyan]Skipping web archives (will rsync directly in Phase 3)[/cyan]")
            for site in sites:
                domain = site["domain"]
                entry: Dict[str, Any] = {"domain": domain, "hestia_archive_path": None}

                dbs = site.get("databases", [])
                if dbs and do_databases:
                    db_dumps = []
                    for db in dbs:
                        db_name = (db.get("DATABASE") or db.get("DB") or db.get("database", ""))
                        if db_name and db_name in db_dump_map:
                            db_dumps.append({
                                "db_name": db_name,
                                "dump_path": db_dump_map[db_name],
                            })
                    if db_dumps:
                        entry["hestia_db_dump_path"] = db_dumps[0]["dump_path"]
                        entry["_db_dumps"] = db_dumps

                transfers.append(entry)

        self.hestia.disconnect()

        # ---- Step 2: Transfer DB dumps to local machine ----
        if aapanel_local:
            console.print(f"\nSkipping archive transfer (direct rsync in Phase 3)")
            console.print(f"Transferring {len(db_dump_map)} database dumps only...")
        else:
            console.print(f"\nTransferring {len(transfers)} site archives + {len(db_dump_map)} db dumps...")

        results = self.transfer_mgr.transfer_sites_batch(
            transfers, str(self.local_tmp)
        )

        # Store the db dump mapping for phase_import to use
        self._db_dump_map = db_dump_map

        self.state.data["current_phase"] = "transfer_done"
        self.state.save()

        # Count success
        success_count = sum(1 for r in results.values() if r.get("success"))
        console.print(f"[green]✓ Transferred {success_count}/{len(results)} sites[/green]")
        return results

    # ------------------------------------------------------------------
    # Phase 3: Import to aaPanel
    # ------------------------------------------------------------------

    def phase_import(
        self,
        sites: List[Dict[str, Any]],
        transfer_results: Dict[str, Dict[str, Any]],
        retry_failed: bool = False,
        force: bool = False,
    ) -> List[Dict[str, Any]]:
        """Import sites, databases, SSL, mail, cron into aaPanel."""
        console.print("\n[bold cyan]Phase 3: Importing to aaPanel...[/bold cyan]")

        aapanel_local = self.config.get("aapanel", {}).get("local", False)

        # If local, try to auto-fix panel availability
        if aapanel_local:
            self.ssh.connect()
            try:
                if not self.ssh.is_panel_running():
                    console.print("[yellow]aaPanel service is not running. Starting...[/yellow]")
                    self.ssh.start_panel()
                    import time
                    time.sleep(3)

                # Auto-detect port if default didn't work
                detected_port = self.ssh.detect_panel_port()
                if detected_port:
                    # Preserve scheme from original config
                    from urllib.parse import urlparse
                    scheme = urlparse(self.config["aapanel"]["panel_url"]).scheme or "https"
                    old_url = self.api.panel_url
                    self.api.panel_url = f"{scheme}://127.0.0.1:{detected_port}"
                    console.print(f"[cyan]Auto-detected aaPanel: {self.api.panel_url}[/cyan]")
            finally:
                self.ssh.disconnect()

        if not self.api.test_connection():
            # If local, provide more help
            if aapanel_local:
                console.print("[yellow]Trying to detect correct port...[/yellow]")
                self.ssh.connect()
                try:
                    port = self.ssh.detect_panel_port()
                    if port:
                        from urllib.parse import urlparse
                        scheme = urlparse(self.config["aapanel"]["panel_url"]).scheme or "https"
                        self.api.panel_url = f"{scheme}://127.0.0.1:{port}"
                        console.print(f"[cyan]Retrying with {self.api.panel_url}...[/cyan]")
                        if self.api.test_connection():
                            console.print(f"[green]Connected! Using {self.api.panel_url}[/green]")
                finally:
                    self.ssh.disconnect()

            if not self.api.test_connection():
                console.print("[red]Cannot connect to aaPanel API. Check config.[/red]")
                return []

        migration_cfg = self.config.get("migration", {})
        do_databases = migration_cfg.get("databases", True)
        do_ssl = migration_cfg.get("ssl_certificates", True)
        do_mail = migration_cfg.get("mail_accounts", False)
        do_cron = migration_cfg.get("cron_jobs", True)

        results: List[Dict[str, Any]] = []

        with create_progress() as progress:
            task_id = progress.add_task(
                "[cyan]Importing sites...", total=len(sites)
            )

            for site in sites:
                domain = site["domain"]
                mapped_domain = self.transformer.map_domain(domain)

                # Skip already migrated (unless --force or --retry-failed)
                if self.state.is_migrated(mapped_domain) and not force:
                    if retry_failed and self.state.is_failed(mapped_domain):
                        log.info(f"Retrying failed site: {mapped_domain}")
                        if mapped_domain in self.state.data["failed_sites"]:
                            self.state.data["failed_sites"].remove(mapped_domain)
                        self.state.save()
                    else:
                        log.info(f"Skipping {mapped_domain} (already migrated)")
                        progress.advance(task_id)
                        continue

                try:
                    result = self._import_site(
                        site,
                        transfer_results.get(domain, {}),
                        do_databases=do_databases,
                        do_ssl=do_ssl,
                        do_mail=do_mail,
                        do_cron=do_cron,
                    )
                    results.append(result)

                    if result.get("success"):
                        self.state.mark_site_migrated(mapped_domain, result)
                        print_success(mapped_domain, {
                            "Site ID": result.get("site_id", "?"),
                            "SSL": "✓" if result.get("ssl_deployed") else "✗",
                            "DB": "✓" if result.get("db_created") else "✗",
                        })
                    else:
                        self.state.mark_site_failed(mapped_domain, result.get("error", "unknown"))
                        print_error_context(mapped_domain, result.get("error", "unknown"))

                except Exception as e:
                    log.exception(f"Unexpected error importing {domain}")
                    results.append({"domain": mapped_domain, "success": False, "error": str(e)})
                    self.state.mark_site_failed(mapped_domain, str(e))
                    print_error_context(mapped_domain, str(e))

                progress.advance(task_id)

        self.state.data["current_phase"] = "import_done"
        self.state.save()

        return results

    def _import_site(
        self,
        site: Dict[str, Any],
        transfer: Dict[str, Any],
        do_databases: bool = True,
        do_ssl: bool = True,
        do_mail: bool = False,
        do_cron: bool = True,
    ) -> Dict[str, Any]:
        """Import a single site into aaPanel.

        Steps:
        1. Upload files to /www/wwwroot/{domain}/
        2. Import databases
        3. Create site via API
        4. Add domain aliases
        5. Deploy SSL
        6. Setup mail accounts
        7. Import cron jobs
        """
        domain = self.transformer.map_domain(site["domain"])
        php_ver = self.transformer.map_php_version(site.get("php_version", "81"))
        user = site.get("user", "unknown")
        result: Dict[str, Any] = {
            "domain": domain,
            "success": False,
            "site_id": None,
            "db_created": False,
            "ssl_deployed": False,
            "mail_created": False,
            "cron_imported": False,
            "error": "",
        }

        aapanel_local = self.config.get("aapanel", {}).get("local", False)

        # --- Step 1: Transfer web files ---
        if not self.dry_run:
            web_root = f"/www/wwwroot/{domain}"

            if aapanel_local:
                # Running ON aaPanel server → rsync directly from Hestia
                # Skip archive+upload+extract entirely — much faster!
                self.ssh.connect()
                try:
                    self.ssh.ensure_dir(web_root)
                finally:
                    self.ssh.disconnect()

                hestia_user = site.get("user", "unknown")
                ok, err = self.transfer_mgr.rsync_site_from_hestia(
                    hestia_user=hestia_user,
                    domain=domain,
                    aapanel_web_root=web_root,
                )
                if ok:
                    log.info(f"Direct rsync OK: {domain} → {web_root}")
                else:
                    log.error(f"Direct rsync failed for {domain}: {err}")

                # Fix permissions locally
                self.ssh.connect()
                try:
                    self.ssh.exec(f"chown -R www:www {web_root} 2>/dev/null || chown -R www-data:www-data {web_root} 2>/dev/null || true")
                finally:
                    self.ssh.disconnect()
            else:
                # Remote aaPanel → use archive approach
                self.ssh.connect()
                try:
                    self.ssh.create_web_root(domain)

                    archive_path = transfer.get("archive_local_path")
                    if archive_path and os.path.exists(archive_path):
                        remote_archive = f"{self.ssh.tmp_dir}/{os.path.basename(archive_path)}"
                        self.ssh.ensure_dir(self.ssh.tmp_dir)

                        ok, err = self.transfer_mgr.transfer(
                            archive_path, remote_archive,
                            direction="upload",
                            local=False,
                        )
                        if ok:
                            self.ssh.exec(
                                f"tar xzf {remote_archive} -C {web_root} 2>/dev/null"
                            )
                            self.ssh.exec(f"chown -R www:www {web_root} 2>/dev/null || true")
                            log.info(f"Extracted web files to {web_root}")
                        else:
                            log.error(f"Failed to upload archive for {domain}: {err}")
                finally:
                    self.ssh.disconnect()

        # --- Step 2: Create site via API (MUST come before DB import) ---
        if self.dry_run:
            log.info(f"[DRY RUN] Would create site: {domain} (PHP {php_ver})")
            result["site_id"] = 0  # fake
            site_id = 0
        else:
            aliases = [self.transformer.map_domain(a) for a in site.get("aliases", [])]

            try:
                api_result = self.api.add_site(
                    domain=domain,
                    path=f"/www/wwwroot/{domain}",
                    php_version=php_ver,
                    port=80,
                    description=f"Migrated from HestiaCP (user: {user})",
                    domain_aliases=aliases,
                    create_db=False,  # DB created separately below
                    create_ftp=False,
                )
                site_id = api_result.get("siteId")
                result["site_id"] = site_id
                log.info(f"Created site: {domain} → siteId={site_id}")
            except AAPanelAPIError as e:
                # Check if site already exists (either from API or error message)
                err_msg = str(e).lower()
                if "already exists" in err_msg:
                    # Site was created in a previous run — try to find its ID
                    existing = self.api.get_site_by_domain(domain)
                    if existing:
                        site_id = existing.get("id")
                        result["site_id"] = site_id
                        log.warning(f"Site {domain} already exists (id={site_id}), reusing")
                    else:
                        # Can't get ID but site exists — continue gracefully
                        result["site_id"] = 0
                        log.warning(f"Site {domain} already exists (could not get ID), continuing")
                else:
                    result["error"] = f"AddSite failed: {e}"
                    return result

        site_id = result["site_id"]

        # --- Step 3: Create databases + import dumps ---
        db_dump_path = transfer.get("db_dump_local_path")
        if do_databases and not self.dry_run:
            dbs = site.get("databases", [])
            for db in dbs:
                # HestiaCP keys: DATABASE/DB, DBUSER, PASSWORD (NOT DBPASS!), HOST, CHARSET
                db_name = db.get("DATABASE") or db.get("DB") or db.get("database", "")
                db_user = db.get("DBUSER") or db.get("USER") or db.get("dbuser", "")
                db_pass = db.get("PASSWORD") or db.get("DBPASS") or db.get("dbpass", "")
                db_host = db.get("HOST") or db.get("host", "localhost")
                db_charset = db.get("CHARSET") or db.get("charset", "utf8mb4")
                log.info(f"DB creds for {domain}: name={db_name}, user={db_user}, "
                         f"pass={'***' if db_pass else 'EMPTY!'}, host={db_host}")

                if not db_name:
                    continue

                # Use ORIGINAL names/passwords from HestiaCP so websites work without reconfiguration
                # Priority: db.conf → wp-config.php → .my.cnf → random (last resort)
                db_password = db_pass
                if not db_password:
                    # Per-domain fallbacks (same logic as fix_db_passwords.py)
                    self.hestia.connect()
                    try:
                        db_password = self.hestia._find_db_password(user, site["domain"]) or ""
                    finally:
                        self.hestia.disconnect()
                if not db_password:
                    db_password = self.transformer._gen_password()
                    log.warning(f"Using generated password for {db_name} (no original found!)")

                # Try aaPanel API first, fall back to direct MySQL
                # Use ORIGINAL db_name, db_user, db_pass so CMS configs work unchanged
                try:
                    self.api.add_database(
                        name=db_name,
                        db_user=db_user or db_name,
                        password=db_password,
                        charset=db_charset,
                        address=self.transformer.db_access_address(db_host),
                        site_id=site_id,
                    )
                    result["db_created"] = True
                    log.info(f"Created database via API: {db_name}")
                except AAPanelAPIError as e:
                    if "already exists" in str(e).lower():
                        result["db_created"] = True
                        log.info(f"Database {db_name} already exists")
                    else:
                        # API failed — create via direct MySQL commands
                        log.warning(f"API database creation failed ({e}), trying direct MySQL...")
                        self.ssh.connect()
                        try:
                            self.ssh.create_mysql_database(
                                db_name=db_name,
                                db_user=db_user or db_name,
                                db_password=db_password,
                                charset=db_charset,
                            )
                            result["db_created"] = True
                            log.info(f"Created database via MySQL: {db_name}")
                        except Exception as mysql_err:
                            log.error(f"Failed to create database {db_name}: {mysql_err}")
                        finally:
                            self.ssh.disconnect()

                # Import dump if available
                if db_dump_path and os.path.exists(db_dump_path) and result.get("db_created"):
                    self.ssh.connect()
                    try:
                        if aapanel_local:
                            self.ssh.import_mysql_dump(db_dump_path, db_name)
                        else:
                            remote_dump = f"{self.ssh.tmp_dir}/{os.path.basename(db_dump_path)}"
                            self.ssh.ensure_dir(self.ssh.tmp_dir)
                            ok, _ = self.transfer_mgr.transfer(
                                db_dump_path, remote_dump,
                                direction="upload", local=False,
                            )
                            if ok:
                                self.ssh.import_mysql_dump(remote_dump, db_name)
                    finally:
                        self.ssh.disconnect()
                break  # Only process first DB for now

        # --- Step 4: Add domain aliases ---

        # --- Step 5: SSL ---
        if do_ssl and site.get("has_ssl"):
            ssl_certs = site.get("ssl_certs", {})
            # If cached data is missing cert content (stripped from old cache), re-read
            if not ssl_certs or not ssl_certs.get("cert"):
                log.info(f"Re-reading SSL certs for {domain} (not in cache)...")
                self.hestia.connect()
                try:
                    ssl_certs = self.hestia.read_ssl_cert(site["user"], site["domain"])
                finally:
                    self.hestia.disconnect()
                # Update site data for future use
                site["ssl_certs"] = ssl_certs
            cert_pem = ssl_certs.get("cert", "") or ssl_certs.get("pem", "")
            key_pem = ssl_certs.get("key", "")

            if cert_pem and key_pem and not self.dry_run:
                try:
                    # Deploy cert files via SSH
                    self.ssh.connect()
                    self.ssh.deploy_ssl_files(domain, cert_pem, key_pem)
                    self.ssh.disconnect()

                    # Register cert via API
                    self.api.set_ssl(domain, key_pem, cert_pem)
                    self.api.enable_ssl(domain)
                    self.api.force_https(domain)
                    result["ssl_deployed"] = True
                    log.info(f"SSL deployed for {domain}")
                except AAPanelAPIError as e:
                    log.error(f"SSL deployment failed for {domain}: {e}")
            elif self.dry_run:
                log.info(f"[DRY RUN] Would deploy SSL for {domain}")

        # --- Step 6: Mail accounts ---
        if do_mail and site.get("mail_accounts"):
            mail_accounts = self.transformer.transform_mail_accounts(
                site["mail_accounts"], domain
            )
            for acc in mail_accounts:
                if acc.get("suspended"):
                    continue
                if not self.dry_run:
                    try:
                        self.api.add_mailbox(
                            email=acc["email"],
                            password=acc["password"],
                            full_name=acc["full_name"],
                            quota=acc["quota"],
                            is_admin=acc["is_admin"],
                        )
                    except AAPanelAPIError as e:
                        log.error(f"Mailbox creation failed: {acc['email']}: {e}")
            result["mail_created"] = True

        # --- Step 7: Cron jobs ---
        if do_cron and site.get("cron_jobs"):
            jobs = self.transformer.transform_cron_jobs(site["cron_jobs"])
            active_jobs = [j for j in jobs if not j.get("suspended")]
            if active_jobs and not self.dry_run:
                self.ssh.connect()
                self.ssh.import_cron_jobs(active_jobs)
                self.ssh.disconnect()
                result["cron_imported"] = True

        # --- Verify ---
        checks = self.config.get("checks", {})
        if checks.get("http_verify") and not self.dry_run:
            self.ssh.connect()
            try:
                ok, code, err = self.ssh.http_check(
                    domain,
                    use_https=result.get("ssl_deployed", False),
                    timeout=checks.get("http_timeout", 30),
                )
                result["http_verified"] = ok
                result["http_code"] = code
                if not ok:
                    log.warning(f"HTTP check failed for {domain}: HTTP {code} - {err}")
            finally:
                self.ssh.disconnect()

        if checks.get("ssl_verify") and result.get("ssl_deployed") and not self.dry_run:
            self.ssh.connect()
            try:
                ssl_info = self.ssh.ssl_check(domain)
                result["ssl_verified"] = ssl_info.get("valid", False)
                result["ssl_expires"] = ssl_info.get("expires", "")
            finally:
                self.ssh.disconnect()

        result["success"] = True
        return result

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    def rollback(self):
        """Remove all sites that were migrated (based on state file)."""
        console.print("\n[bold red]Rollback: Removing migrated sites from aaPanel...[/bold red]")

        if not self.api.test_connection():
            console.print("[red]Cannot connect to aaPanel API[/red]")
            return

        migrated = self.state.data.get("migrated_sites", [])
        if not migrated:
            console.print("[yellow]No sites to rollback[/yellow]")
            return

        console.print(f"Found {len(migrated)} sites to remove")

        for domain in migrated:
            try:
                existing = self.api.get_site_by_domain(domain)
                if existing:
                    site_id = existing.get("id")
                    self.api.delete_site(
                        site_id=site_id,
                        domain=domain,
                        delete_ftp=True,
                        delete_db=True,
                        delete_files=False,  # preserve files
                    )
                    console.print(f"[green]✓ Removed: {domain}[/green]")
                else:
                    console.print(f"[yellow]Site not found (already deleted?): {domain}[/yellow]")
            except AAPanelAPIError as e:
                console.print(f"[red]✗ Failed to remove {domain}: {e}[/red]")

        self.state.data["migrated_sites"] = []
        self.state.data["failed_sites"] = []
        self.state.save()

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self):
        """Clean up temporary files. Preserves remote dumps/archives for resume."""
        # Clean local temp only (intermediate transfer files)
        import shutil
        if self.local_tmp.exists():
            shutil.rmtree(self.local_tmp, ignore_errors=True)
            log.info(f"Cleaned local temp: {self.local_tmp}")

        # NOTE: We do NOT clean remote temp dirs on Hestia/aaPanel.
        # Database dumps and web archives are preserved for --resume.
        # They are small relative to disk space and prevent re-dumping 197 databases.

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    def run(self, resume: bool = False, rollback: bool = False, retry_failed: bool = False, force: bool = False):
        """Run the full migration workflow."""
        print_banner()

        if rollback:
            self.setup()
            self.rollback()
            return

        if resume:
            console.print("[cyan]Resuming from previous state...[/cyan]")
            if self.state.data["total_sites"] == 0:
                console.print("[yellow]No previous state found, starting fresh[/yellow]")

        self.setup()

        try:
            # Phase 1: Extract (cached — skip on resume if already done)
            sites_cache = self._load_sites_cache()
            if resume and sites_cache:
                sites = sites_cache
                console.print(f"[green]Loaded {len(sites)} sites from cache (skipping Phase 1)[/green]")
            else:
                sites = self.phase_extract()
                self._save_sites_cache(sites)
                if not sites:
                    console.print("[yellow]No sites to migrate[/yellow]")
                    return

            # Show preview
            console.print("\n[bold]Sites to migrate:[/bold]")
            for s in sites:
                ssl_mark = "🔒" if s.get("has_ssl") else "  "
                db_count = len(s.get("databases", []))
                alias_count = len(s.get("aliases", []))
                console.print(
                    f"  {ssl_mark} [cyan]{s['domain']}[/cyan] "
                    f"(PHP {s.get('php_version', '?')}, "
                    f"{db_count} DB(s), {alias_count} alias(es))"
                )

            if self.dry_run:
                console.print("\n[yellow]DRY RUN — no changes will be made[/yellow]")
                console.print("[yellow]Add --no-dry-run to config to perform actual migration[/yellow]")
                return

            # Confirmation prompt
            if not self.dry_run:
                console.print(
                    f"\n[yellow]About to migrate {len(sites)} sites from "
                    f"{self.config['hestia']['host']} → {self.config['aapanel']['host']}[/yellow]"
                )
                confirm = input("Proceed? [y/N]: ").strip().lower()
                if confirm not in ("y", "yes"):
                    console.print("[red]Aborted[/red]")
                    return

            # Phase 2: Transfer
            transfer_results = self.phase_transfer(sites)

            # Phase 3: Import
            results = self.phase_import(sites, transfer_results, retry_failed=retry_failed, force=force)

            # Summary
            print_summary(self.state)

            succeeded = sum(1 for r in results if r.get("success"))
            failed = sum(1 for r in results if not r.get("success"))
            console.print(f"\n[bold green]Migration complete: {succeeded} succeeded, {failed} failed[/bold green]")
            console.print(f"Log file: {LOG_FILE}")

        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted. State saved — use --resume to continue[/yellow]")
            self.state.save()
        except Exception as e:
            log.exception("Fatal error")
            console.print(f"\n[red]Fatal error: {e}[/red]")
            console.print(f"State saved. Log: {LOG_FILE}")
            self.state.save()
        finally:
            self.cleanup()


# ======================================================================
# CLI
# ======================================================================

def main():
    parser = argparse.ArgumentParser(
        description="HestiaCP → aaPanel Migration Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python migrate.py --config config.yaml
  python migrate.py --config config.yaml --dry-run
  python migrate.py --config config.yaml --resume
  python migrate.py --config config.yaml --rollback
        """,
    )
    parser.add_argument(
        "--config", "-c",
        required=True,
        help="Path to config.yaml file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only, no actual changes",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from previous interrupted run",
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="Remove all migrated sites from aaPanel",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry sites that failed in a previous run",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore state cache and re-import ALL sites (even previously migrated ones)",
    )

    args = parser.parse_args()

    # Validate config path
    if not Path(args.config).exists():
        console.print(f"[red]Config file not found: {args.config}[/red]")
        sys.exit(1)

    migrator = HestiaToAAPanelMigrator(args.config)

    if args.dry_run:
        migrator.dry_run = True

    migrator.run(
        resume=args.resume,
        rollback=args.rollback,
        retry_failed=args.retry_failed,
        force=args.force,
    )


if __name__ == "__main__":
    main()
