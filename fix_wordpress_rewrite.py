#!/usr/bin/env python3
"""
Apply WordPress Nginx rewrite rules to migrated sites.

aaPanel stores rewrite templates at /www/server/panel/rewrite/
This script copies the WordPress template to each site's rewrite config.

Usage:
    python fix_wordpress_rewrite.py --config config.mine.yaml --domains domains.txt
    python fix_wordpress_rewrite.py --config config.mine.yaml --all
    python fix_wordpress_rewrite.py --config config.mine.yaml --domains domains.txt --dry-run
"""

import argparse
import sys
from pathlib import Path
from typing import Set

import yaml

from modules.aapanel_ssh import AAPanelSSH
from modules.utils import console


def load_domains(filepath: str) -> Set[str]:
    domains = set()
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                domains.add(line)
    return domains


def is_wordpress(ssh: AAPanelSSH, domain: str) -> bool:
    """Check if a site is WordPress by looking for wp-config.php."""
    return ssh.file_exists(f"/www/wwwroot/{domain}/wp-config.php")


def main():
    parser = argparse.ArgumentParser(description="Apply WordPress rewrite rules")
    parser.add_argument("--config", "-c", required=True, help="Path to config.yaml")
    parser.add_argument("--domains", "-d", help="Path to domains.txt")
    parser.add_argument("--all", action="store_true", help="All WordPress sites in /www/wwwroot/")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)
    aapanel_cfg = config["aapanel"]

    ssh = AAPanelSSH(
        host=aapanel_cfg.get("host", "localhost"),
        port=aapanel_cfg.get("port", 22),
        user=aapanel_cfg.get("user", "root"),
        password=aapanel_cfg.get("password"),
        ssh_key=aapanel_cfg.get("ssh_key"),
        local=aapanel_cfg.get("local", False),
    )
    ssh.connect()

    # WordPress rewrite template in aaPanel
    WP_REWRITE_SRC = "/www/server/panel/rewrite/wordpress.conf"
    REWRITE_DIR = "/www/server/panel/vhost/rewrite"

    if not ssh.file_exists(WP_REWRITE_SRC):
        console.print(f"[red]WordPress rewrite template not found: {WP_REWRITE_SRC}[/red]")
        console.print("[yellow]Make sure nginx and WordPress rewrite templates are installed in aaPanel[/yellow]")
        ssh.disconnect()
        sys.exit(1)

    # Collect target domains
    target_domains = None
    if args.domains:
        target_domains = load_domains(args.domains)

    if args.all:
        # Find all directories in /www/wwwroot/
        _, listing, _ = ssh.exec("ls -1 /www/wwwroot/ 2>/dev/null", warn_on_error=False)
        all_dirs = [d for d in listing.split("\n") if d.strip() and d != "default"]
        if target_domains:
            all_dirs = [d for d in all_dirs if d in target_domains]
    elif target_domains:
        all_dirs = list(target_domains)
    else:
        console.print("[red]Specify --domains or --all[/red]")
        sys.exit(1)

    # Filter to WordPress sites only
    wp_sites = []
    for domain in all_dirs:
        if is_wordpress(ssh, domain):
            wp_sites.append(domain)

    if not wp_sites:
        console.print("[yellow]No WordPress sites found[/yellow]")
        ssh.disconnect()
        return

    console.print(f"\n[bold]WordPress sites found: {len(wp_sites)}[/bold]")

    # Check which ones already have rewrite rules
    to_apply = []
    for domain in wp_sites:
        rewrite_conf = f"{REWRITE_DIR}/{domain}.conf"
        if not ssh.file_exists(rewrite_conf):
            to_apply.append(domain)

    if not to_apply:
        console.print("[green]All WordPress sites already have rewrite rules![/green]")
        ssh.disconnect()
        return

    console.print(f"Sites needing rewrite: [cyan]{len(to_apply)}[/cyan]")
    for d in to_apply[:10]:
        console.print(f"  {d}")
    if len(to_apply) > 10:
        console.print(f"  ... and {len(to_apply) - 10} more")

    if args.dry_run:
        console.print("\n[yellow]DRY RUN — no changes made[/yellow]")
        ssh.disconnect()
        return

    confirm = input(f"\nApply WordPress rewrites to {len(to_apply)} sites? [y/N]: ").strip().lower()
    if confirm not in ("y", "yes"):
        console.print("[red]Aborted[/red]")
        ssh.disconnect()
        return

    # Read the WordPress rewrite template
    if ssh.local:
        with open(WP_REWRITE_SRC) as f:
            wp_rules = f.read()
    else:
        wp_rules = ""
        # Read via SSH cat
        code, content, _ = ssh.exec(f"cat {WP_REWRITE_SRC}")
        if code == 0:
            wp_rules = content

    if not wp_rules:
        console.print("[red]Failed to read WordPress rewrite template[/red]")
        ssh.disconnect()
        return

    # Apply to each site
    applied = 0
    for domain in to_apply:
        rewrite_conf = f"{REWRITE_DIR}/{domain}.conf"

        if ssh.local:
            with open(rewrite_conf, "w") as f:
                f.write(wp_rules)
        else:
            # Write via SFTP
            ssh.ensure_dir(REWRITE_DIR)
            with ssh._sftp.open(rewrite_conf, "w") as f:
                f.write(wp_rules)

        console.print(f"  [green]✓[/green] {domain}")
        applied += 1

    # Reload nginx
    ssh.restart_nginx()
    console.print(f"\n[bold green]Applied rewrites to {applied} sites + nginx reloaded[/bold green]")
    ssh.disconnect()


if __name__ == "__main__":
    main()
