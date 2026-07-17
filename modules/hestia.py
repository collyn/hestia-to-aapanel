"""
HestiaCP data extraction via SSH or local execution.

Supports two modes:
- SSH mode: connects to a remote HestiaCP server over SSH
- Local mode: runs v-* CLI commands directly (when script runs on the HestiaCP server)
"""

import json
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .utils import log, console

# paramiko is optional (only needed for SSH mode)
try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False

# ---------------------------------------------------------------------------
# Known HestiaCP web server templates and their PHP versions
# ---------------------------------------------------------------------------
PHP_FPM_TEMPLATES = {
    "PHP-7_4": "74",
    "PHP-8_0": "80",
    "PHP-8_1": "81",
    "PHP-8_2": "82",
    "PHP-8_3": "83",
    "PHP-8_4": "84",
}


class HestiaClient:
    """Client wrapper for HestiaCP server (SSH or local)."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 22,
        user: str = "root",
        password: Optional[str] = None,
        ssh_key: Optional[str] = None,
        hestia_path: str = "/usr/local/hestia",
        tmp_dir: str = "/root/hestia_migration_dumps",
        local: bool = False,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.ssh_key = Path(ssh_key).expanduser() if ssh_key else None
        self.hestia_path = Path(hestia_path)
        self.bin_path = self.hestia_path / "bin"
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
            # Verify we can access Hestia bin path locally
            if not self.bin_path.exists():
                raise RuntimeError(
                    f"HestiaCP bin path not found: {self.bin_path}. "
                    "Are you sure this is a HestiaCP server? "
                    "Set hestia.local=false to use remote SSH."
                )
            log.debug(f"Local mode: using HestiaCP at {self.hestia_path}")
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

        try:
            self._client.connect(**connect_kwargs)
            self._sftp = self._client.open_sftp()
            log.debug(f"Connected to HestiaCP server: {self.host}:{self.port}")
        except Exception as e:
            log.error(f"Failed to connect to HestiaCP server: {e}")
            raise

    def disconnect(self):
        """Close SSH connection (no-op in local mode)."""
        if self.local:
            return
        if self._sftp:
            self._sftp.close()
        if self._client:
            self._client.close()
        log.debug("Disconnected from HestiaCP server")

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()

    # ------------------------------------------------------------------
    # Raw command execution
    # ------------------------------------------------------------------

    def exec(self, command: str, timeout: int = 120, warn_on_error: bool = True) -> Tuple[int, str, str]:
        """Execute a command. Uses local subprocess or SSH depending on mode.
        Returns (exit_code, stdout, stderr).

        Args:
            warn_on_error: If False, non-zero exit codes are NOT logged as warnings
                           (use for expected failures like `test -f` checking file existence).
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
                out = result.stdout.strip()
                err = result.stderr.strip()
                code = result.returncode
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

    def exec_json(self, command: str, timeout: int = 120) -> Dict[str, Any]:
        """Execute a v-* command with 'json' format and return parsed result."""
        exit_code, stdout, stderr = self.exec(command, timeout)
        if exit_code != 0:
            log.error(f"Command failed: {command}\n{stderr}")
            return {}
        try:
            return json.loads(stdout) if stdout else {}
        except json.JSONDecodeError:
            log.warning(f"Non-JSON output from: {command[:100]}")
            log.debug(f"Output: {stdout[:500]}")
            return {}

    # ------------------------------------------------------------------
    # Data extraction
    # ------------------------------------------------------------------

    def get_users(self) -> List[str]:
        """List all HestiaCP users."""
        result = self.exec_json(f"{self.bin_path}/v-list-users json")
        if isinstance(result, list):
            return [u for u in result if u not in ("admin",)]
        if isinstance(result, dict):
            return [k for k in result.keys() if k not in ("admin",)]
        return []

    def get_web_domains(self, user: str) -> List[str]:
        """List all web domains for a given user."""
        result = self.exec_json(f"{self.bin_path}/v-list-web-domains {user} json")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return list(result.keys())
        return []

    def get_web_domain_detail(self, user: str, domain: str) -> Dict[str, Any]:
        """Get detailed info for a specific web domain."""
        return self.exec_json(f"{self.bin_path}/v-list-web-domain {user} {domain} json")

    def get_databases(self, user: str) -> List[Dict[str, Any]]:
        """List all databases for a user.

        HestiaCP CLI does NOT include passwords in JSON output for security.
        We read passwords directly from the db.conf file.
        """
        result = self.exec_json(f"{self.bin_path}/v-list-databases {user} json")
        dbs: List[Dict[str, Any]] = []
        if isinstance(result, list):
            dbs = result
        elif isinstance(result, dict):
            dbs = [{"DATABASE": k, **v} for k, v in result.items()]

        # Read passwords from db.conf (CLI strips them)
        db_conf = f"/usr/local/hestia/data/users/{user}/db.conf"
        exit_code, content, _ = self.exec(f"cat {db_conf} 2>/dev/null", warn_on_error=False)
        if exit_code == 0:
            # Parse: DB='name' DBUSER='user' PASSWORD='pass' HOST='host' ...
            for line in content.split("\n"):
                line = line.strip()
                if not line:
                    continue
                # Extract password
                pw_match = re.search(r"PASSWORD='([^']*)'", line)
                db_match = re.search(r"DB='([^']*)'", line)
                if pw_match and db_match:
                    db_name = db_match.group(1)
                    password = pw_match.group(1)
                    # Find matching DB entry and inject password
                    for db_entry in dbs:
                        entry_name = db_entry.get("DATABASE") or db_entry.get("DB", "")
                        if entry_name == db_name:
                            db_entry["PASSWORD"] = password
                            break

        return dbs

    def get_databases_for_domain(self, user: str, domain: str) -> List[Dict[str, Any]]:
        """Find databases actually used by a specific domain.

        HestiaCP stores databases per-user, not per-domain. This method scans
        the web root for CMS config files to find the actual DB name.
        """
        all_dbs = self.get_databases(user)
        if not all_dbs:
            return []

        web_root = f"/home/{user}/web/{domain}/public_html"

        # Check multiple locations for wp-config.php (some setups move it up one level)
        wp_candidates = [
            f"{web_root}/wp-config.php",
            f"/home/{user}/web/{domain}/wp-config.php",
            f"{web_root}/wp/wp-config.php",
        ]

        # Known config files and their DB name patterns (with wp-config candidates)
        config_checks = []
        for wp_path in wp_candidates:
            config_checks.append((wp_path, r"define\s*\(\s*'DB_NAME'\s*,\s*'([^']+)'"))

        config_checks.extend([
            (f"{web_root}/.env", r"DB_DATABASE=(\S+)"),
            (f"{web_root}/config.php", r"'database'\s*=>\s*'([^']+)'"),
            (f"{web_root}/app/etc/env.php", r"'dbname'\s*=>\s*'([^']+)'"),
            (f"{web_root}/sites/default/settings.php", r"'database'\s*=>\s*'([^']+)'"),
            (f"{web_root}/configuration.php", r"\$db\s*=\s*'([^']+)'"),
        ])

        for config_path, pattern in config_checks:
            exit_code, content, _ = self.exec(
                f"cat {config_path} 2>/dev/null", warn_on_error=False
            )
            if exit_code == 0 and content:
                match = re.search(pattern, content)
                if match:
                    db_name = match.group(1)
                    # Find this DB in the all_dbs list
                    for db in all_dbs:
                        db_entry_name = (db.get("DATABASE") or db.get("DB") or db.get("database", ""))
                        if db_entry_name == db_name or db_entry_name.endswith(f"_{db_name}"):
                            log.info(f"Matched DB {db_entry_name} → {domain} (via {config_path})")
                            return [db]

                    # DB name found in config but NOT in HestiaCP DB list
                    # → create synthetic entry from config file credentials
                    log.warning(f"DB '{db_name}' found in {config_path} but not in HestiaCP list — extracting from config")
                    db_user = ""
                    db_pass = ""
                    db_host = "localhost"

                    # Extract full credentials from wp-config.php
                    user_match = re.search(r"define\s*\(\s*'DB_USER'\s*,\s*'([^']+)'", content)
                    pass_match = re.search(r"define\s*\(\s*'DB_PASSWORD'\s*,\s*'([^']+)'", content)
                    host_match = re.search(r"define\s*\(\s*'DB_HOST'\s*,\s*'([^']+)'", content)

                    if user_match:
                        db_user = user_match.group(1)
                    if pass_match:
                        db_pass = pass_match.group(1)
                    if host_match:
                        db_host = host_match.group(1)

                    log.info(f"Extracted from config: DB={db_name}, USER={db_user}, HOST={db_host}")
                    return [{
                        "DATABASE": db_name,
                        "DB": db_name,
                        "DBUSER": db_user,
                        "PASSWORD": db_pass,
                        "HOST": db_host,
                        "TYPE": "mysql",
                        "CHARSET": "utf8mb4",
                    }]

        # Heuristic: match by domain name parts in database name
        # Normalize both sides: strip non-alphanumeric for fuzzy matching
        domain_parts_raw = set()
        for part in domain.lower().split("."):
            if len(part) >= 3 and part not in ("com", "vn", "net", "org", "biz", "info", "online", "gov", "edu"):
                domain_parts_raw.add(part)
                # Also without hyphens/underscores (e.g., "s-tech" → "stech", "asahi_lux" → "asahilux")
                clean = re.sub(r'[^a-z0-9]', '', part)
                if clean and clean != part:
                    domain_parts_raw.add(clean)

        matched = []
        for db in all_dbs:
            db_name = (db.get("DATABASE") or db.get("DB") or db.get("database", "")).lower()
            db_clean = re.sub(r'[^a-z0-9]', '', db_name)

            for part in domain_parts_raw:
                if part in db_name or part in db_clean:
                    matched.append(db)
                    break

        if matched:
            log.info(f"Heuristic matched {len(matched)} DB(s) for {domain}: {[d.get('DATABASE','') for d in matched]}")
            for db in matched:
                if not db.get("PASSWORD"):
                    db["PASSWORD"] = self._find_db_password(user, domain) or ""
            return matched

        # Last resort: if user has exactly 1 DB, assume it belongs to this domain
        if len(all_dbs) == 1:
            db = all_dbs[0]
            if not db.get("PASSWORD"):
                db["PASSWORD"] = self._find_db_password(user, domain) or ""
            log.info(f"Single DB user {user}: assigning {db.get('DATABASE','')} → {domain}")
            return [db]

        # Last resort: if site has wp-config.php but we couldn't match, return first DB
        wp_check = f"{web_root}/wp-config.php"
        code, _, _ = self.exec(f"test -f {wp_check}", warn_on_error=False)
        if code == 0 and all_dbs:
            db = all_dbs[0]
            if not db.get("PASSWORD"):
                db["PASSWORD"] = self._find_db_password(user, domain) or ""
            log.warning(f"WordPress detected for {domain} but no DB matched — using {db.get('DATABASE','')}")
            return [db]

        log.debug(f"No DB matched for {domain}, returning empty")
        return []

    def _find_db_password(self, user: str, domain: str) -> Optional[str]:
        """Find DB password from per-domain config files (fallback)."""
        # wp-config.php
        wp_config = f"/home/{user}/web/{domain}/public_html/wp-config.php"
        code, content, _ = self.exec(f"cat {wp_config} 2>/dev/null", warn_on_error=False)
        if code == 0:
            match = re.search(r"define\s*\(\s*'DB_PASSWORD'\s*,\s*'([^']+)'", content)
            if match:
                return match.group(1)

        # .my.cnf
        my_cnf = f"/home/{user}/web/{domain}/.my.cnf"
        code, content, _ = self.exec(f"cat {my_cnf} 2>/dev/null", warn_on_error=False)
        if code == 0:
            match = re.search(r"password\s*=\s*(\S+)", content)
            if match:
                return match.group(1)

        return None

    def get_dns_domains(self, user: str) -> List[str]:
        """List all DNS domains for a user."""
        result = self.exec_json(f"{self.bin_path}/v-list-dns-domains {user} json")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return list(result.keys())
        return []

    def get_dns_records(self, user: str, domain: str) -> List[Dict[str, Any]]:
        """Get all DNS records for a domain."""
        result = self.exec_json(f"{self.bin_path}/v-list-dns-records {user} {domain} json")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return list(result.values())
        return []

    def get_mail_domains(self, user: str) -> List[str]:
        """List all mail domains for a user."""
        result = self.exec_json(f"{self.bin_path}/v-list-mail-domains {user} json")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return list(result.keys())
        return []

    def get_mail_accounts(self, user: str, domain: str) -> List[Dict[str, Any]]:
        """Get all mail accounts for a domain."""
        result = self.exec_json(f"{self.bin_path}/v-list-mail-accounts {user} {domain} json")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return list(result.values())
        return []

    def get_cron_jobs(self, user: str) -> List[Dict[str, Any]]:
        """List all cron jobs for a user."""
        result = self.exec_json(f"{self.bin_path}/v-list-cron-jobs {user} json")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return list(result.values())
        return []

    def get_system_ips(self) -> Dict[str, Any]:
        """Get system IP configuration."""
        return self.exec_json(f"{self.bin_path}/v-list-sys-ips json")

    # ------------------------------------------------------------------
    # File system operations
    # ------------------------------------------------------------------

    def get_mysql_admin_credentials(self) -> Dict[str, str]:
        """Read MySQL admin credentials from HestiaCP config."""
        mysql_conf = "/usr/local/hestia/conf/mysql.conf"
        exit_code, content, _ = self.exec(f"cat {mysql_conf}", warn_on_error=False)
        if exit_code != 0:
            log.error("Cannot read MySQL config")
            return {}

        creds = {}
        for line in content.split("\n"):
            match = re.match(r"(\w+)='(.*)'", line.strip())
            if match:
                creds[match.group(1).lower()] = match.group(2)

        return creds

    def detect_php_version(self, user: str, domain: str) -> str:
        """Detect PHP version from HestiaCP config files.

        Detection priority:
        1. Nginx config fastcgi_pass socket (most reliable, per-domain)
        2. PHP-FPM pool config listen directive
        3. Backend template name (only if it contains explicit version)
        4. System default PHP
        """
        # Method 1: Nginx config — contains actual fastcgi_pass socket
        nginx_conf = f"/home/{user}/conf/web/{domain}/nginx.conf"
        exit_code, content, _ = self.exec(f"cat {nginx_conf} 2>/dev/null", warn_on_error=False)
        if exit_code == 0:
            # Look for: fastcgi_pass unix:/run/php/php8.1-fpm-{user}.sock;
            match = re.search(r"fastcgi_pass\s+unix:/run/php/php(\d+)\.(\d+)-fpm", content)
            if match:
                ver = f"{match.group(1)}{match.group(2)}"
                log.debug(f"PHP {ver} detected via nginx config for {domain}")
                return ver
            # Alternative: fastcgi_pass 127.0.0.1:9000 style → check upstream
            match = re.search(r"fastcgi_pass\s+unix:/var/run/php/php(\d+)\.(\d+)-fpm", content)
            if match:
                ver = f"{match.group(1)}{match.group(2)}"
                log.debug(f"PHP {ver} detected via nginx config (alt path) for {domain}")
                return ver

        # Apache config
        apache_conf = f"/home/{user}/conf/web/{domain}/apache2.conf"
        exit_code, content, _ = self.exec(f"cat {apache_conf} 2>/dev/null", warn_on_error=False)
        if exit_code == 0:
            match = re.search(r"php(\d+)\.(\d+)-fpm", content)
            if match:
                ver = f"{match.group(1)}{match.group(2)}"
                log.debug(f"PHP {ver} detected via apache config for {domain}")
                return ver

        # Method 2: PHP-FPM pool config listen directive
        php_conf = f"/home/{user}/conf/web/{domain}/php-fpm.conf"
        exit_code, content, _ = self.exec(f"cat {php_conf} 2>/dev/null", warn_on_error=False)
        if exit_code == 0:
            match = re.search(r"listen\s*=\s*/run/php/php(\d+)\.(\d+)", content)
            if match:
                ver = f"{match.group(1)}{match.group(2)}"
                log.debug(f"PHP {ver} detected via fpm pool for {domain}")
                return ver
            match = re.search(r"php(\d+)\.(\d+)-fpm", content)
            if match:
                ver = f"{match.group(1)}{match.group(2)}"
                log.debug(f"PHP {ver} detected via fpm pool (alt) for {domain}")
                return ver

        # Method 3: Backend template name (only explicit versions)
        detail = self.get_web_domain_detail(user, domain)
        backend_tpl = detail.get("BACKEND", "") or detail.get("BACKEND_TPL", "") or ""
        if backend_tpl:
            for tpl_name, version in PHP_FPM_TEMPLATES.items():
                if tpl_name in backend_tpl:
                    log.debug(f"PHP {version} detected via backend template for {domain}")
                    return version
            match = re.search(r"PHP[-_]?(\d)[-_]?(\d)", backend_tpl)
            if match:
                ver = f"{match.group(1)}{match.group(2)}"
                log.debug(f"PHP {ver} detected via template name for {domain}")
                return ver

        # Method 4: System default PHP
        exit_code, content, _ = self.exec("php -r 'echo PHP_VERSION;' 2>/dev/null", warn_on_error=False)
        if exit_code == 0 and content:
            parts = content.split(".")
            if len(parts) >= 2:
                ver = f"{parts[0]}{parts[1]}"
                log.warning(f"PHP {ver} detected via system default for {domain} (may be wrong)")
                return ver

        log.warning(f"Cannot detect PHP version for {domain}, using default 81")
        return "81"

    def get_web_root(self, user: str, domain: str) -> str:
        """Get the web document root for a domain."""
        return f"/home/{user}/web/{domain}/public_html/"

    def get_ssl_cert_paths(self, user: str, domain: str) -> Dict[str, str]:
        """Get SSL certificate file paths for a domain."""
        ssl_dir = f"/home/{user}/conf/web/{domain}/ssl/"
        return {
            "cert": f"{ssl_dir}{domain}.crt",
            "key": f"{ssl_dir}{domain}.key",
            "ca": f"{ssl_dir}{domain}.ca",
            "pem": f"{ssl_dir}{domain}.pem",
        }

    def ssl_exists(self, user: str, domain: str) -> bool:
        """Check if SSL certificates exist for a domain."""
        paths = self.get_ssl_cert_paths(user, domain)
        # warn_on_error=False: file not found is expected, not an error
        exit_code, stdout, _ = self.exec(
            f"test -f {paths['cert']} && test -f {paths['key']} && echo OK",
            warn_on_error=False,
        )
        return exit_code == 0 and "OK" in stdout

    def read_ssl_cert(self, user: str, domain: str) -> Dict[str, str]:
        """Read SSL certificate and key contents."""
        paths = self.get_ssl_cert_paths(user, domain)
        result = {}
        for name, path in paths.items():
            exit_code, content, _ = self.exec(f"cat {path} 2>/dev/null", warn_on_error=False)
            if exit_code == 0 and content:
                result[name] = content
        return result

    def get_domain_aliases(self, user: str, domain: str) -> List[str]:
        """Get domain aliases from web domain detail."""
        detail = self.get_web_domain_detail(user, domain)
        aliases_str = detail.get("ALIAS", "")
        if not aliases_str or aliases_str == "none":
            return []
        return [a.strip() for a in aliases_str.split(",") if a.strip()]

    # ------------------------------------------------------------------
    # Database dump
    # ------------------------------------------------------------------

    def find_existing_dump(self, db_name: str) -> Optional[str]:
        """Check if a dump file already exists for this database.

        Returns the path to the existing dump, or None.
        """
        exit_code, stdout, _ = self.exec(
            f"ls -t {self.tmp_dir}/{db_name}_*.sql 2>/dev/null | head -1",
            warn_on_error=False,
        )
        path = stdout.strip() if stdout else ""
        if path and exit_code == 0:
            # Verify it's not empty
            check_code, size_str, _ = self.exec(
                f"stat -c%s {path} 2>/dev/null || wc -c < {path}",
                warn_on_error=False,
            )
            try:
                if int(size_str.strip() or 0) > 0:
                    log.info(f"Reusing existing dump: {path} ({size_str.strip()} bytes)")
                    return path
            except ValueError:
                pass
        return None

    def dump_database(
        self,
        db_name: str,
        output_path: Optional[str] = None,
    ) -> Tuple[str, str]:
        """Dump a MySQL database using mysqldump.

        Returns (remote_dump_path, dump_filename).

        Uses --defaults-extra-file to avoid password-in-command-line warnings
        and shell escaping issues with special characters.
        """
        creds = self.get_mysql_admin_credentials()
        db_user = creds.get("user", "root")
        db_pass = creds.get("password", "")

        filename = f"{db_name}_{datetime.now():%Y%m%d_%H%M%S}.sql"
        remote_path = f"{self.tmp_dir}/{filename}"

        # Create tmp dir if not exists
        self.exec(f"mkdir -p {self.tmp_dir}")

        # Use mysql config file approach to avoid password-in-command warnings
        # and shell escaping issues
        my_cnf = f"{self.tmp_dir}/.my.cnf"
        self.exec(
            f"printf '[client]\\nuser={db_user}\\npassword=\"{db_pass}\"\\n' > {my_cnf} && chmod 600 {my_cnf}"
        )

        dump_cmd = (
            f"mysqldump --defaults-extra-file={my_cnf} "
            f"--single-transaction --routines --triggers "
            f"--add-drop-table --extended-insert "
            f"--no-tablespaces "
            f"{db_name} > {remote_path} 2>/dev/null"
        )

        exit_code, stdout, stderr = self.exec(dump_cmd, timeout=600)

        # Clean up temp my.cnf
        self.exec(f"rm -f {my_cnf}", warn_on_error=False)

        if exit_code != 0:
            raise RuntimeError(f"mysqldump failed for {db_name}: {stderr}")

        # Verify dump file is not empty
        check_code, size_str, _ = self.exec(
            f"stat -c%s {remote_path} 2>/dev/null || wc -c < {remote_path}",
            warn_on_error=False,
        )
        try:
            file_size = int(size_str.strip()) if size_str else 0
        except ValueError:
            file_size = 0

        if file_size == 0:
            raise RuntimeError(
                f"mysqldump produced empty file for {db_name}. "
                f"Database may not exist or is empty."
            )
        if file_size < 100:
            # Very small dump — might just be a "no tables" DB
            log.warning(f"Database {db_name} dump is very small ({file_size} bytes)")

        log.info(f"Dumped database: {db_name} → {remote_path} ({file_size} bytes)")
        return remote_path, filename

    def find_existing_archive(self, domain: str) -> Optional[str]:
        """Check if a web archive already exists for this domain."""
        exit_code, stdout, _ = self.exec(
            f"ls -t {self.tmp_dir}/{domain}_*.tar.gz 2>/dev/null | head -1",
            warn_on_error=False,
        )
        path = stdout.strip() if stdout else ""
        if path and exit_code == 0:
            check_code, size_str, _ = self.exec(
                f"stat -c%s {path} 2>/dev/null || wc -c < {path}",
                warn_on_error=False,
            )
            try:
                if int(size_str.strip() or 0) > 0:
                    log.info(f"Reusing existing archive: {path} ({size_str.strip()} bytes)")
                    return path
            except ValueError:
                pass
        return None

    # ------------------------------------------------------------------
    # Web files archive
    # ------------------------------------------------------------------

    def archive_web_files(
        self,
        user: str,
        domain: str,
    ) -> Tuple[str, str]:
        """Create a tar.gz archive of web files (public_html/ contents only).

        Returns (remote_tar_path, tar_filename).
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{domain}_{timestamp}.tar.gz"
        remote_path = f"{self.tmp_dir}/{filename}"

        self.exec(f"mkdir -p {self.tmp_dir}")

        # Archive ONLY public_html/ contents → aaPanel expects files at web root directly
        # -C changes to public_html dir, . means everything inside it
        tar_cmd = (
            f"tar czf {remote_path} "
            f"-C /home/{user}/web/{domain}/public_html . "
            f"2>/dev/null"
        )
        exit_code, _, stderr = self.exec(tar_cmd, timeout=300)
        if exit_code != 0:
            log.warning(f"tar warning for {domain}: {stderr}")

        log.info(f"Archived web files: {domain} → {remote_path}")
        return remote_path, filename

    def cleanup_temp(self):
        """Remove temporary files on Hestia server."""
        self.exec(f"rm -rf {self.tmp_dir}")

    # ------------------------------------------------------------------
    # Full site extraction
    # ------------------------------------------------------------------

    def extract_all(self, max_workers: int = 8) -> List[Dict[str, Any]]:
        """Extract complete data for all websites (parallel).

        Uses multiple SSH connections to extract domains concurrently.

        Args:
            max_workers: Number of parallel SSH connections for extraction.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        users = self.get_users()
        log.info(f"Found {len(users)} HestiaCP users: {users}")

        # Build flat list of (user, domain) pairs
        all_domains: List[Tuple[str, str]] = []
        for user in users:
            domains = self.get_web_domains(user)
            log.info(f"User '{user}': {len(domains)} domains")
            for domain in domains:
                all_domains.append((user, domain))

        log.info(f"Total: {len(all_domains)} domains to extract (parallel={max_workers})")

        if max_workers <= 1:
            # Sequential mode
            all_sites = []
            for user, domain in all_domains:
                try:
                    all_sites.append(self._extract_site(user, domain))
                except Exception as e:
                    log.error(f"Failed to extract {user}/{domain}: {e}")
                    all_sites.append({"user": user, "domain": domain, "error": str(e), "status": "extraction_failed"})
            return all_sites

        # Parallel mode: each thread gets its own SSH connection
        from .utils import create_progress
        all_sites = []

        def _extract_one(user_domain: Tuple[str, str]) -> Dict[str, Any]:
            user, domain = user_domain
            # Create a fresh SSH connection for this thread
            client = HestiaClient(
                host=self.host, port=self.port, user=self.user,
                password=self.password, ssh_key=str(self.ssh_key) if self.ssh_key else None,
                hestia_path=str(self.hestia_path), tmp_dir=self.tmp_dir,
                local=self.local,
            )
            try:
                client.connect()
                return client._extract_site(user, domain)
            except Exception as e:
                log.error(f"Failed to extract {user}/{domain}: {e}")
                return {"user": user, "domain": domain, "error": str(e), "status": "extraction_failed"}
            finally:
                client.disconnect()

        with create_progress() as progress:
            task = progress.add_task("[cyan]Extracting sites...", total=len(all_domains))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(_extract_one, d): d for d in all_domains}
                for future in as_completed(futures):
                    all_sites.append(future.result())
                    progress.advance(task)

        log.info(f"Extracted {len(all_sites)} sites total")
        return all_sites

    def _extract_site(self, user: str, domain: str) -> Dict[str, Any]:
        """Extract all data for one site."""
        log.debug(f"Extracting: {user} / {domain}")

        domain_detail = self.get_web_domain_detail(user, domain)
        php_version = self.detect_php_version(user, domain)
        aliases = self.get_domain_aliases(user, domain)
        has_ssl = self.ssl_exists(user, domain)
        ip = domain_detail.get("IP", "") if isinstance(domain_detail, dict) else ""

        # Detect web server type
        proxy_tpl = domain_detail.get("PROXY", "") if isinstance(domain_detail, dict) else ""
        tpl = domain_detail.get("TPL", "") if isinstance(domain_detail, dict) else ""
        web_type = "PHP"
        if proxy_tpl and "nginx" in proxy_tpl.lower():
            web_type = "PHP"  # nginx proxy → PHP backend

        site: Dict[str, Any] = {
            "user": user,
            "domain": domain,
            "aliases": aliases,
            "php_version": php_version,
            "web_type": web_type,
            "ip": ip,
            "has_ssl": has_ssl,
            "web_root": self.get_web_root(user, domain),
            "domain_detail": domain_detail,
            # Databases (only those matching this domain)
            "databases": self.get_databases_for_domain(user, domain),
            # DNS (optional)
            "dns_records": [] if not self.get_dns_domains(user) else self.get_dns_records(user, domain) if domain in self.get_dns_domains(user) else [],
            # Mail (optional)
            "mail_accounts": [] if domain not in self.get_mail_domains(user) else self.get_mail_accounts(user, domain),
            # Cron
            "cron_jobs": self.get_cron_jobs(user),
            # SSL cert content
            "ssl_certs": self.read_ssl_cert(user, domain) if has_ssl else {},
        }

        return site
