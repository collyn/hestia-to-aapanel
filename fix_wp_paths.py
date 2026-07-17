#!/usr/bin/env python3
"""
Fix WordPress paths after HestiaCP → aaPanel migration.

WordPress stores old HestiaCP paths (/home/user/web/domain/public_html/)
in the database. aaPanel's open_basedir blocks these paths.
This script auto-detects all WordPress sites on aaPanel and:
1. Fixes open_basedir to allow old paths (quick fix)
2. Runs SQL search-replace to update all old paths → new paths
3. Reloads PHP-FPM

Usage:
    python fix_wp_paths.py --config config.mine.yaml              # auto-scan all
    python fix_wp_paths.py --config config.mine.yaml --dry-run    # preview
    python fix_wp_paths.py --config config.mine.yaml -d list.txt  # specific domains
"""

import argparse
import re
import sys
from typing import Dict, List, Optional, Set

import yaml

from modules.hestia import HestiaClient
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


def find_wp_sites(ssh: AAPanelSSH, domains: Set[str]) -> List[Dict]:
    """Find WordPress sites with their DB credentials."""
    sites = []
    for domain in domains:
        wp_config = f"/www/wwwroot/{domain}/wp-config.php"
        if not ssh.file_exists(wp_config):
            continue
        if ssh.local:
            with open(wp_config) as f:
                content = f.read()
        else:
            _, content, _ = ssh.exec(f"cat {wp_config}")
        db_name = re.search(r"define\s*\(\s*'DB_NAME'\s*,\s*'([^']+)'", content)
        db_user = re.search(r"define\s*\(\s*'DB_USER'\s*,\s*'([^']+)'", content)
        db_pass = re.search(r"define\s*\(\s*'DB_PASSWORD'\s*,\s*'([^']+)'", content)
        prefix  = re.search(r"\$table_prefix\s*=\s*'([^']+)'", content)
        if db_name:
            sites.append({
                "domain": domain,
                "db_name": db_name.group(1),
                "db_user": db_user.group(1) if db_user else "",
                "db_pass": db_pass.group(1) if db_pass else "",
                "prefix": prefix.group(1) if prefix else "wp_",
            })
    return sites


def main():
    parser = argparse.ArgumentParser(description="Fix WordPress paths after migration")
    parser.add_argument("--config", "-c", required=True, help="config.yaml path")
    parser.add_argument("--domains", "-d", help="Optional: domains.txt (auto-scan if omitted)")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)
    aapanel_cfg = config["aapanel"]
    hestia_cfg = config["hestia"]

    # 1. Connect to aaPanel
    ssh = AAPanelSSH(
        host=aapanel_cfg.get("host", "localhost"),
        port=aapanel_cfg.get("port", 22),
        user=aapanel_cfg.get("user", "root"),
        password=aapanel_cfg.get("password"),
        ssh_key=aapanel_cfg.get("ssh_key"),
        local=aapanel_cfg.get("local", False),
        mysql_root_password=aapanel_cfg.get("mysql_root_password", ""),
    )
    ssh.connect()
    mysql_bin = ssh._mysql_cmd()

    # 2. Get target domains
    if args.domains:
        target = load_domains(args.domains)
    else:
        _, listing, _ = ssh.exec("ls -1 /www/wwwroot/ 2>/dev/null", warn_on_error=False)
        target = {d for d in listing.split("\n") if d.strip() and d not in ("default",)}
        console.print(f"Auto-scanned [cyan]{len(target)} domains[/cyan]")

    # 3. HestiaCP: map domain → user (for old path detection)
    domain_user = {}
    hestia = HestiaClient(
        host=hestia_cfg.get("host", "localhost"), port=hestia_cfg.get("port", 22),
        user=hestia_cfg.get("user", "root"), password=hestia_cfg.get("password"),
        ssh_key=hestia_cfg.get("ssh_key"), local=hestia_cfg.get("local", False),
    )
    hestia.connect()
    for user in hestia.get_users():
        for domain in hestia.get_web_domains(user):
            domain_user[domain] = user
    hestia.disconnect()

    # 4. Find WordPress sites
    wp_sites = find_wp_sites(ssh, target)
    console.print(f"Found [cyan]{len(wp_sites)} WordPress sites[/cyan]")

    if args.dry_run:
        for s in wp_sites:
            old = f"/home/{domain_user.get(s['domain'], '?')}/web/{s['domain']}/public_html"
            console.print(f"  {s['domain']}: {old} → /www/wwwroot/{s['domain']}")
        console.print("[yellow]DRY RUN — no changes[/yellow]")
        ssh.disconnect()
        return

    # 5. Fix each site
    fixed = 0
    for site in wp_sites:
        domain = site["domain"]
        new_path = f"/www/wwwroot/{domain}"
        user = domain_user.get(domain, "???")
        old_path = f"/home/{user}/web/{domain}/public_html"

        # Verify old path exists in DB
        code, db_check, _ = ssh.exec(
            f"{mysql_bin} {site['db_name']} -N -e "
            f"\"SELECT COUNT(*) FROM {site['prefix']}options WHERE option_value LIKE '%{old_path}%'\" 2>/dev/null",
            warn_on_error=False,
        )
        count = int(db_check.strip() or 0)

        if count == 0:
            # Try to detect actual old path from DB
            code, sample, _ = ssh.exec(
                f"{mysql_bin} {site['db_name']} -N -e "
                f"\"SELECT option_value FROM {site['prefix']}options WHERE option_value LIKE '%/home/%' LIMIT 1\" 2>/dev/null",
                warn_on_error=False,
            )
            if code == 0:
                m = re.search(r'(/home/\w+/web/[^/]+/public_html)', sample)
                if m:
                    old_path = m.group(1)

        console.print(f"\n[bold]{domain}[/bold]: {old_path} → {new_path} ({count} refs in DB)")

        # 5a. open_basedir
        basedir = f"/www/server/panel/vhost/open_basedir/nginx/{domain}.conf"
        if ssh.file_exists(basedir):
            _, bd, _ = ssh.exec(f"cat {basedir}", warn_on_error=False)
            if old_path not in bd:
                ssh.exec(f"sed -i 's|$|:{old_path}/|' {basedir}")
                console.print("  [green]✓[/green] open_basedir")

        # 5b. SQL search-replace
        wp_cli = (ssh.exec("which wp 2>/dev/null", warn_on_error=False)[0] == 0)
        if wp_cli:
            code, out, _ = ssh.exec(
                f"cd {new_path} && wp search-replace '{old_path}' '{new_path}' --all-tables --quiet 2>&1",
                timeout=120,
            )
            console.print(f"  [green]✓[/green] WP-CLI" if code == 0 else f"  [yellow]⚠[/yellow] {out[:80]}")
        else:
            tables = [f"{site['prefix']}options", f"{site['prefix']}postmeta", f"{site['prefix']}posts"]
            sql = "\n".join(
                f"UPDATE {t} SET option_value=REPLACE(option_value,'{old_path}','{new_path}') WHERE option_value LIKE '%{old_path}%';"
                if 'options' in t else
                f"UPDATE {t} SET meta_value=REPLACE(meta_value,'{old_path}','{new_path}') WHERE meta_value LIKE '%{old_path}%';"
                if 'postmeta' in t else
                f"UPDATE {t} SET post_content=REPLACE(post_content,'{old_path}','{new_path}') WHERE post_content LIKE '%{old_path}%';"
                for t in tables
            )
            sf = f"{ssh.tmp_dir}/fix_{domain}.sql"
            ssh.ensure_dir(ssh.tmp_dir)
            if ssh.local:
                with open(sf, "w") as f: f.write(sql)
            else:
                with ssh._sftp.open(sf, "w") as f: f.write(sql)
            code, _, err = ssh.exec(f"{mysql_bin} {site['db_name']} < {sf}", timeout=60)
            ssh.exec(f"rm -f {sf}", warn_on_error=False)
            console.print(f"  [green]✓[/green] SQL" if code == 0 else f"  [yellow]⚠[/yellow] {err[:80]}")

        fixed += 1

    if fixed:
        ssh.restart_php_fpm()
    ssh.disconnect()
    console.print(f"\n[bold green]Fixed {fixed} WordPress site(s)[/bold green]")


if __name__ == "__main__":
    main()
