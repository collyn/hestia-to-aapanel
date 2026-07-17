"""
Data transformation: HestiaCP format → aaPanel API parameters.

Handles:
- PHP version detection and mapping
- Database credential normalization
- DNS record format conversion
- Domain name and path mapping
"""

import re
import secrets
import string
from typing import Any, Dict, List, Optional


class DataTransformer:
    """Transforms HestiaCP extracted data into aaPanel-compatible formats."""

    def __init__(
        self,
        php_mapping: Optional[Dict[str, str]] = None,
        domain_map: Optional[Dict[str, str]] = None,
        default_quota: str = "5 GB",
    ):
        self.php_mapping = php_mapping or {
            "7.4": "74",
            "8.0": "80",
            "8.1": "81",
            "8.2": "82",
            "8.3": "83",
            "8.4": "84",
        }
        self.default_php = "81"  # PHP-81
        self.domain_map = domain_map or {}
        self.default_quota = default_quota

    # ------------------------------------------------------------------
    # Domain & Path mapping
    # ------------------------------------------------------------------

    def map_domain(self, domain: str) -> str:
        """Apply domain name mapping (e.g., for renaming)."""
        return self.domain_map.get(domain, domain)

    def aa_panel_path(self, domain: str) -> str:
        """Convert HestiaCP domain to aaPanel web root path."""
        mapped = self.map_domain(domain)
        return f"/www/wwwroot/{mapped}"

    # ------------------------------------------------------------------
    # PHP version
    # ------------------------------------------------------------------

    def map_php_version(self, hestia_version: str) -> str:
        """Map detected PHP version to aaPanel format.

        Args:
            hestia_version: Detected version like '74', '8.1', '81', etc.

        Returns:
            aaPanel PHP version string: 'PHP-74', 'PHP-80', 'PHP-81', etc.
        """
        version = hestia_version.strip()

        # Already in aaPanel format with prefix (e.g., 'PHP-74')
        if version.upper().startswith("PHP"):
            return version

        # Convert '7.4' or '74' → 'PHP-74'
        if "." in version:
            parts = version.split(".")
            numeric = f"{parts[0]}{parts[1]}"
        elif version.isdigit() and len(version) == 2:
            numeric = version
        else:
            numeric = self.php_mapping.get(version, self.default_php)

        return f"PHP-{numeric}"

    # ------------------------------------------------------------------
    # Database credentials
    # ------------------------------------------------------------------

    def normalize_db_name(self, raw_name: str, domain: str) -> str:
        """Normalize database name for aaPanel.

        aaPanel convention: removes prefixes, converts to safe format.
        """
        # Remove common HestiaCP user prefixes
        name = raw_name
        if "_" in name:
            parts = name.split("_", 1)
            if len(parts) == 2:
                name = parts[1]  # strip hestia user prefix

        # Only allow alphanumeric + underscore
        name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
        return name[:64]  # MySQL max 64 chars

    def normalize_db_user(self, raw_user: str, domain: str) -> str:
        """Normalize database username for aaPanel."""
        user = raw_user
        if "_" in user:
            parts = user.split("_", 1)
            if len(parts) == 2:
                user = parts[1]
        user = re.sub(r"[^a-zA-Z0-9_]", "_", user)
        return user[:32]  # MySQL max 32 chars

    def db_access_address(self, hestia_host: str) -> str:
        """Map HestiaCP database host to aaPanel access address.

        '%' = any IP, '127.0.0.1' = local only.
        """
        host = hestia_host.lower().strip()
        if host in ("localhost", "127.0.0.1", "::1"):
            return "127.0.0.1"
        if host == "%":
            return "%"
        # Remote host — keep as-is
        return host

    # ------------------------------------------------------------------
    # DNS records
    # ------------------------------------------------------------------

    def transform_dns_records(
        self, hestia_records: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Convert HestiaCP DNS records to aaPanel DNS plugin format.

        aaPanel DNS Manager plugin expected format:
          {type, name, value, ttl, priority}
        """
        records = []
        for rec in hestia_records:
            record = {
                "type": rec.get("TYPE", rec.get("type", "")).upper(),
                "name": rec.get("RECORD", rec.get("record", "")),
                "value": rec.get("VALUE", rec.get("value", "")),
                "ttl": int(rec.get("TTL", rec.get("ttl", 3600))),
                "priority": int(rec.get("PRIORITY", rec.get("priority", 0)) or 0),
            }
            records.append(record)
        return records

    # ------------------------------------------------------------------
    # Mail accounts
    # ------------------------------------------------------------------

    def transform_mail_accounts(
        self, hestia_mail: List[Dict[str, Any]], domain: str
    ) -> List[Dict[str, Any]]:
        """Convert HestiaCP mail accounts to aaPanel mail plugin format.

        Returns list of dicts suitable for add_mailbox() API.
        """
        accounts = []
        for acc in hestia_mail:
            account_name = acc.get("ACCOUNT", acc.get("account", ""))
            if not account_name:
                continue

            accounts.append({
                "email": f"{account_name}@{domain}",
                "password": acc.get("PASSWORD", acc.get("password", "")),
                "full_name": account_name,
                "quota": acc.get("QUOTA", acc.get("quota", self.default_quota)),
                "is_admin": 0,
                "suspended": acc.get("SUSPENDED", acc.get("suspended", "no")) == "yes",
            })
        return accounts

    # ------------------------------------------------------------------
    # Cron jobs
    # ------------------------------------------------------------------

    def transform_cron_jobs(
        self, hestia_cron: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Normalize cron job format. Already compatible with standard crontab."""
        jobs = []
        for job in hestia_cron:
            jobs.append({
                "min": job.get("MIN", job.get("min", "*")),
                "hour": job.get("HOUR", job.get("hour", "*")),
                "day": job.get("DAY", job.get("day", "*")),
                "month": job.get("MONTH", job.get("month", "*")),
                "wday": job.get("WDAY", job.get("wday", "*")),
                "cmd": job.get("CMD", job.get("cmd", "")),
                "suspended": job.get("SUSPENDED", job.get("suspended", "no")) == "yes",
            })
        return jobs

    # ------------------------------------------------------------------
    # Site metadata for aaPanel AddSite
    # ------------------------------------------------------------------

    def build_add_site_params(
        self,
        site_data: Dict[str, Any],
        create_db: bool = False,
        create_ftp: bool = False,
    ) -> Dict[str, Any]:
        """Build the complete parameter set for aaPanel AddSite API.

        Args:
            site_data: Extracted data from HestiaCP
            create_db: Whether to create database with site creation
            create_ftp: Whether to create FTP with site creation

        Returns:
            Dict of parameters for AAPanelAPI.add_site()
        """
        domain = self.map_domain(site_data["domain"])
        php_ver = self.map_php_version(site_data.get("php_version", "81"))
        aliases = [self.map_domain(a) for a in site_data.get("aliases", [])]

        params: Dict[str, Any] = {
            "domain": domain,
            "path": self.aa_panel_path(domain),
            "php_version": php_ver,
            "port": 80,
            "description": f"Migrated from HestiaCP (user: {site_data.get('user', 'unknown')})",
            "domain_aliases": aliases,
            "create_ftp": create_ftp,
            "create_db": create_db,
        }

        if create_db:
            # Use first database from site's list, or generate defaults
            dbs = site_data.get("databases", [])
            if dbs:
                db_info = dbs[0]
                db_name = db_info.get("DATABASE", db_info.get("database", ""))
                db_user = db_info.get("DBUSER", db_info.get("dbuser", ""))
                params["db_name"] = self.normalize_db_name(db_name, domain)
                params["db_user"] = self.normalize_db_user(db_user, domain)
                params["db_password"] = db_info.get(
                    "DBPASS", db_info.get("dbpass", self._gen_password())
                )
            else:
                safe_name = domain.replace(".", "_").replace("-", "_")
                params["db_name"] = safe_name
                params["db_user"] = safe_name[:16]
                params["db_password"] = self._gen_password()

        return params

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _gen_password(length: int = 20) -> str:
        """Generate a secure random password."""
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
        return "".join(secrets.choice(alphabet) for _ in range(length))
