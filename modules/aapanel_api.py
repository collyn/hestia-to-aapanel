"""
aaPanel REST API client.

Handles authentication token generation and all relevant API endpoints:
site management, database management, SSL certificate deployment,
Let's Encrypt automation, and mail plugin calls.
"""

import hashlib
import json
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from .utils import log, console


class AAPanelAPIError(Exception):
    """Raised when aaPanel API returns an error."""
    pass


class AAPanelAPI:
    """aaPanel REST API client."""

    def __init__(
        self,
        panel_url: str,
        api_key: str,
        timeout: int = 60,
    ):
        self.panel_url = panel_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "HestiaCP-Migration-Tool/1.0",
            "Accept": "application/json",
        })

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _make_signature(self) -> Dict[str, Any]:
        """Generate the request_time + request_token for API auth."""
        t = int(time.time())
        first_md5 = hashlib.md5(self.api_key.encode("utf-8")).hexdigest()
        token = hashlib.md5(
            (str(t) + first_md5).encode("utf-8")
        ).hexdigest()
        return {"request_time": t, "request_token": token}

    def _post(self, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Make an authenticated POST request to aaPanel API.

        Args:
            endpoint: API path, e.g. '/site' or '/database'
            data: Additional POST parameters (action + params)

        Returns:
            Parsed JSON response dict.

        Raises:
            AAPanelAPIError: on API error or HTTP failure.
        """
        url = f"{self.panel_url}{endpoint}"

        # Merge auth params with request data
        payload = self._make_signature()
        if data:
            payload.update(data)

        log.debug(f"API POST {endpoint} action={data.get('action', '?') if data else '?'}")

        try:
            resp = self._session.post(
                url,
                data=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise AAPanelAPIError(f"HTTP error calling {endpoint}: {e}")

        try:
            result = resp.json()
        except json.JSONDecodeError:
            raise AAPanelAPIError(
                f"Invalid JSON from {endpoint}: {resp.text[:500]}"
            )

        # aaPanel API often returns status field
        if isinstance(result, dict):
            if result.get("status") is False:
                msg = result.get("msg", "Unknown API error")
                raise AAPanelAPIError(f"API error on {endpoint}: {msg}")

        return result

    # ------------------------------------------------------------------
    # Site Management
    # ------------------------------------------------------------------

    def add_site(
        self,
        domain: str,
        path: str,
        php_version: str = "81",
        port: int = 80,
        description: str = "",
        domain_aliases: Optional[List[str]] = None,
        create_ftp: bool = False,
        create_db: bool = False,
        db_name: str = "",
        db_user: str = "",
        db_password: str = "",
        db_charset: str = "utf8mb4",
    ) -> Dict[str, Any]:
        """Create a new website in aaPanel.

        Args:
            domain: Primary domain name
            path: Document root path (e.g. /www/wwwroot/example.com)
            php_version: '74', '80', '81', '82', '83' or '00' for static
            port: Port number (default 80)
            description: Site description/note
            domain_aliases: List of additional domain names
            create_ftp: Also create FTP account
            create_db: Also create database
            db_name: Database name (required if create_db=True)
            db_user: Database username (required if create_db=True)
            db_password: Database password (required if create_db=True)
            db_charset: Database charset

        Returns:
            API response with siteStatus, siteId, ftpStatus, databaseStatus.
        """
        if domain_aliases is None:
            domain_aliases = []

        webname = json.dumps({
            "domain": domain,
            "domainlist": domain_aliases,
            "count": len(domain_aliases),
        })

        params: Dict[str, Any] = {
            "action": "AddSite",
            "webname": webname,
            "path": path,
            "type": "PHP" if php_version != "00" else "",
            "version": php_version,
            "port": str(port),
            "ps": description or f"Migrated from HestiaCP",
        }

        if create_ftp:
            params["ftp"] = "true"
            params["ftp_username"] = domain.replace(".", "_")
            params["ftp_password"] = self._generate_password()

        if create_db:
            params["sql"] = "true"
            params["datauser"] = db_user or domain.replace(".", "_")
            params["datapassword"] = db_password or self._generate_password()
            params["codeing"] = db_charset

        log.info(f"Creating site: {domain} (PHP {php_version}) at {path}")
        result = self._post("/site", params)
        log.info(f"Site created: {domain} → siteId={result.get('siteId')}")
        return result

    def delete_site(
        self,
        site_id: int,
        domain: str,
        delete_ftp: bool = False,
        delete_db: bool = False,
        delete_files: bool = False,
    ) -> Dict[str, Any]:
        """Delete a website from aaPanel."""
        params = {
            "action": "DeleteSite",
            "id": site_id,
            "webname": domain,
        }
        if delete_ftp:
            params["ftp"] = 1
        if delete_db:
            params["database"] = 1
        if delete_files:
            params["path"] = 1

        log.info(f"Deleting site: {domain} (id={site_id})")
        return self._post("/site", params)

    def add_domain(self, site_id: int, primary_domain: str, new_domain: str) -> Dict[str, Any]:
        """Add a domain alias to an existing site."""
        params = {
            "action": "AddDomain",
            "id": site_id,
            "webname": primary_domain,
            "domain": new_domain,
        }
        log.info(f"Adding domain alias: {new_domain} → {primary_domain} (id={site_id})")
        return self._post("/site", params)

    def set_php_version(self, site_id: int, version: str) -> Dict[str, Any]:
        """Change PHP version for a site."""
        params = {
            "action": "SetPHPVersion",
            "id": site_id,
            "version": version,
        }
        return self._post("/site", params)

    def stop_site(self, site_id: int, domain: str) -> Dict[str, Any]:
        """Stop/disable a site."""
        return self._post("/site", {"action": "SiteStop", "id": site_id, "name": domain})

    def start_site(self, site_id: int, domain: str) -> Dict[str, Any]:
        """Start/enable a site."""
        return self._post("/site", {"action": "SiteStart", "id": site_id, "name": domain})

    def list_sites(self, page: int = 1, limit: int = 100) -> Dict[str, Any]:
        """List all websites in aaPanel."""
        params = {
            "action": "getData",
            "p": page,
            "limit": limit,
            "table": "sites",
            "search": "",
            "order": "",
            "type": "-1",
        }
        return self._post("/v2/data", params)

    def get_site_by_domain(self, domain: str) -> Optional[Dict[str, Any]]:
        """Find a site in aaPanel by domain name. Returns site info or None."""
        try:
            result = self.list_sites()
            sites = result.get("data", [])
            for site in sites:
                if site.get("name") == domain:
                    return site
            return None
        except AAPanelAPIError:
            return None

    # ------------------------------------------------------------------
    # SSL Certificate Management
    # ------------------------------------------------------------------

    def set_ssl(
        self,
        site_name: str,
        private_key_pem: str,
        certificate_pem: str,
    ) -> Dict[str, Any]:
        """Deploy a custom SSL certificate to a site.

        Args:
            site_name: Site domain name
            private_key_pem: Private key in PEM format
            certificate_pem: Full chain certificate in PEM format
        """
        params = {
            "action": "SetSSL",
            "siteName": site_name,
            "key": private_key_pem,
            "csr": certificate_pem,
        }
        log.info(f"Deploying SSL cert for: {site_name}")
        return self._post("/site", params)

    def enable_ssl(self, site_name: str) -> Dict[str, Any]:
        """Enable SSL for a site."""
        params = {
            "action": "SetSSLConf",
            "siteName": site_name,
        }
        log.info(f"Enabling SSL for: {site_name}")
        return self._post("/site", params)

    def disable_ssl(self, site_name: str) -> Dict[str, Any]:
        """Disable SSL for a site."""
        params = {
            "action": "CloseSSLConf",
            "siteName": site_name,
            "updateOf": "1",
        }
        return self._post("/site", params)

    def force_https(self, site_name: str) -> Dict[str, Any]:
        """Enable HTTP→HTTPS redirect."""
        params = {
            "action": "HttpToHttps",
            "siteName": site_name,
        }
        log.info(f"Forcing HTTPS for: {site_name}")
        return self._post("/site", params)

    def disable_force_https(self, site_name: str) -> Dict[str, Any]:
        """Disable HTTP→HTTPS redirect."""
        params = {
            "action": "CloseToHttps",
            "siteName": site_name,
        }
        return self._post("/site", params)

    def get_ssl_info(self, site_name: str) -> Dict[str, Any]:
        """Get SSL status for a site."""
        return self._post("/site", {"action": "GetSSL", "siteName": site_name})

    # ------------------------------------------------------------------
    # Let's Encrypt (ACME)
    # ------------------------------------------------------------------

    def request_lets_encrypt(
        self,
        site_id: int,
        domains: List[str],
        auth_type: str = "http",
        web_root: str = "",
    ) -> Dict[str, Any]:
        """Request a Let's Encrypt certificate via aaPanel ACME module.

        Args:
            site_id: aaPanel site ID
            domains: List of domains to include in cert
            auth_type: 'http', 'tls', or 'dns'
            web_root: Website root path (for http auth_type)

        Returns:
            ACME order info with 'index' for deployment.
        """
        params = {
            "action": "apply_cert_api",
            "id": site_id,
            "domains": json.dumps(domains),
            "auth_type": auth_type,
            "auth_to": web_root or "",
        }
        log.info(f"Requesting Let's Encrypt for: {domains}")
        return self._post("/acme", params)

    def deploy_lets_encrypt(self, index: str, site_name: str) -> Dict[str, Any]:
        """Deploy an issued Let's Encrypt certificate to a site.

        Args:
            index: ACME order index from request_lets_encrypt()
            site_name: Target site domain name
        """
        params = {
            "action": "SetCertToSite",
            "index": index,
            "siteName": site_name,
        }
        log.info(f"Deploying Let's Encrypt cert: {index} → {site_name}")
        return self._post("/acme", params)

    # ------------------------------------------------------------------
    # Database Management
    # ------------------------------------------------------------------

    def add_database(
        self,
        name: str,
        db_user: str,
        password: str,
        charset: str = "utf8mb4",
        address: str = "%",
        site_id: Optional[int] = None,
        server_id: int = 0,
        notes: str = "",
    ) -> Dict[str, Any]:
        """Create a new MySQL database in aaPanel.

        Args:
            name: Database name
            db_user: Database username
            password: Database password
            charset: Character set (utf8, utf8mb4, gbk, big5)
            address: Access control ('%' = any, '127.0.0.1' = local only)
            site_id: Associated website ID (optional)
            server_id: 0 for local MySQL, or remote server ID
            notes: Description
        """
        params: Dict[str, Any] = {
            "action": "AddDatabase",
            "sid": server_id,
            "name": name,
            "db_user": db_user,
            "password": password,
            "address": address,
            "codeing": charset,
        }
        if site_id:
            params["pid"] = site_id
        if notes:
            params["ps"] = notes

        log.info(f"Creating database: {name} (user={db_user})")
        return self._post("/database", params)

    def delete_database(
        self,
        name: str,
        db_user: str,
        server_id: int = 0,
    ) -> Dict[str, Any]:
        """Delete a MySQL database."""
        params = {
            "action": "DeleteDatabase",
            "sid": server_id,
            "name": name,
            "db_user": db_user,
        }
        return self._post("/database", params)

    def list_databases(self) -> Dict[str, Any]:
        """List all databases in aaPanel."""
        return self._post("/database", {"action": "GetDatabaseList"})

    # ------------------------------------------------------------------
    # Mail (requires aaPanel Mail Server plugin)
    # ------------------------------------------------------------------

    def add_mailbox(
        self,
        email: str,
        password: str,
        full_name: str = "",
        quota: str = "5 GB",
        is_admin: int = 0,
    ) -> Dict[str, Any]:
        """Create a mailbox via aaPanel Mail Server plugin.

        Args:
            email: Full email address (e.g. user@example.com)
            password: Mailbox password
            full_name: Display name
            quota: Quota string (e.g. '5 GB')
            is_admin: 1 for admin, 0 for regular user
        """
        params = {
            "action": "a",
            "name": "mail_sys",
            "s": "add_mailbox",
            "username": email,
            "password": password,
            "full_name": full_name or email.split("@")[0],
            "quota": quota,
            "is_admin": str(is_admin),
        }
        log.info(f"Creating mailbox: {email}")
        return self._post("/plugin", params)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_password(length: int = 20) -> str:
        """Generate a secure random password."""
        import secrets
        import string
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def test_connection(self) -> bool:
        """Test API connectivity by listing sites."""
        try:
            self.list_sites()
            log.info("aaPanel API connection OK")
            return True
        except AAPanelAPIError as e:
            log.error(f"aaPanel API connection failed: {e}")
            return False
