#!/usr/bin/env python3
"""
Update PHP version for already-migrated sites in aaPanel.

Detects the correct PHP version from HestiaCP nginx config,
then updates each site via aaPanel API (SetPHPVersion).

Usage:
    python fix_php_version.py --config config.mine.yaml --domains domains.txt
    python fix_php_version.py --config config.mine.yaml --all   # all sites in aaPanel
    python fix_php_version.py --config config.mine.yaml --domains domains.txt --dry-run
"""

import argparse
import sys
from pathlib import Path
from typing import Dict, Optional, Set

import yaml

from modules.hestia import HestiaClient
from modules.aapanel_api import AAPanelAPI, AAPanelAPIError
from modules.transformers import DataTransformer
from modules.utils import console, log


def load_domains(filepath: str) -> Set[str]:
    domains = set()
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                domains.add(line)
    return domains


def detect_php_from_hestia(hestia: HestiaClient, domain: str, user: str) -> str:
    """Get PHP version from HestiaCP for a specific domain."""
    return hestia.detect_php_version(user, domain)


def get_site_php_map(hestia: HestiaClient, transformer: DataTransformer) -> Dict[str, str]:
    """Build {domain: php_version} map from HestiaCP."""
    users = hestia.get_users()
    php_map = {}

    for user in users:
        domains = hestia.get_web_domains(user)
        for domain in domains:
            try:
                raw = hestia.detect_php_version(user, domain)
                php_map[domain] = transformer.map_php_version(raw)
            except Exception:
                php_map[domain] = "PHP-81"

    return php_map


def main():
    parser = argparse.ArgumentParser(description="Fix PHP version for migrated sites")
    parser.add_argument("--config", "-c", required=True, help="Path to config.yaml")
    parser.add_argument("--domains", "-d", help="Path to domains.txt (one per line)")
    parser.add_argument("--all", action="store_true", help="Update ALL sites in aaPanel")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    hestia_cfg = config["hestia"]
    aapanel_cfg = config["aapanel"]

    # Get target domains
    target_domains = None
    if args.domains:
        target_domains = load_domains(args.domains)

    # 1. Connect to HestiaCP, detect PHP versions
    console.print("[cyan]Detecting PHP versions from HestiaCP...[/cyan]")
    hestia = HestiaClient(
        host=hestia_cfg.get("host", "localhost"),
        port=hestia_cfg.get("port", 22),
        user=hestia_cfg.get("user", "root"),
        password=hestia_cfg.get("password"),
        ssh_key=hestia_cfg.get("ssh_key"),
        local=hestia_cfg.get("local", False),
    )
    hestia.connect()
    transformer = DataTransformer(php_mapping=config.get("php_mapping"))
    php_map = get_site_php_map(hestia, transformer)
    hestia.disconnect()

    # 2. Connect to aaPanel API
    panel_url = aapanel_cfg["panel_url"]
    if aapanel_cfg.get("local"):
        from urllib.parse import urlparse
        parsed = urlparse(panel_url)
        panel_url = f"{parsed.scheme}://127.0.0.1:{parsed.port or 8888}"

    api = AAPanelAPI(panel_url=panel_url, api_key=aapanel_cfg["api_key"])
    if not api.test_connection():
        console.print("[red]Cannot connect to aaPanel API[/red]")
        sys.exit(1)

    # 3. Get all sites from aaPanel
    result = api.list_sites(limit=9999)
    sites = result.get("data", [])

    # Filter to target domains
    to_update = []
    for site in sites:
        domain = site.get("name", "")
        if target_domains and domain not in target_domains:
            continue
        if not args.all and not target_domains:
            continue

        current_php = site.get("php_version", "?")
        new_php = php_map.get(domain, "PHP-81")

        if current_php != new_php:
            to_update.append({
                "id": site.get("id"),
                "domain": domain,
                "current": current_php,
                "new": new_php,
            })

    if not to_update:
        console.print("[green]All sites already have correct PHP versions![/green]")
        return

    console.print(f"\n[bold]Sites to update ({len(to_update)}):[/bold]")
    for s in to_update:
        console.print(f"  {s['domain']}: {s['current']} → [cyan]{s['new']}[/cyan]")

    if args.dry_run:
        console.print("\n[yellow]DRY RUN — no changes made[/yellow]")
        return

    confirm = input(f"\nUpdate {len(to_update)} sites? [y/N]: ").strip().lower()
    if confirm not in ("y", "yes"):
        console.print("[red]Aborted[/red]")
        return

    # 4. Update
    updated, failed = 0, 0
    for s in to_update:
        try:
            api.set_php_version(s["id"], s["new"])
            console.print(f"  [green]✓[/green] {s['domain']}: {s['new']}")
            updated += 1
        except AAPanelAPIError as e:
            console.print(f"  [red]✗[/red] {s['domain']}: {e}")
            failed += 1

    console.print(f"\n[bold green]Updated: {updated}, Failed: {failed}[/bold green]")


if __name__ == "__main__":
    main()
