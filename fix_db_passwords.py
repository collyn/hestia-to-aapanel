#!/usr/bin/env python3
"""
Fix database user passwords after migration.

Reads the REAL passwords from HestiaCP db.conf files,
then updates the corresponding MySQL users on aaPanel.
No file transfer needed — just reads configs and runs ALTER USER.

Usage:
    python fix_db_passwords.py --config config.mine.yaml --domains domains.txt
    python fix_db_passwords.py --config config.mine.yaml --domains domains.txt --debug
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

import yaml

from modules.hestia import HestiaClient
from modules.aapanel_ssh import AAPanelSSH
from modules.utils import console, log


def load_domains(filepath: str) -> Set[str]:
    """Load domain list from text file (one per line, # comments)."""
    domains = set()
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                domains.add(line)
    return domains


def read_db_passwords_from_conf(hestia: HestiaClient, user: str) -> Dict[str, str]:
    """Read {db_name: password} from HestiaCP db.conf file.

    db.conf format (one DB per line):
      DB='name' DBUSER='user' PASSWORD='pass' HOST='host' ...
    """
    passwords = {}
    db_conf = f"/usr/local/hestia/data/users/{user}/db.conf"

    code, content, _ = hestia.exec(f"cat {db_conf} 2>/dev/null", warn_on_error=False)
    if code != 0 or not content.strip():
        return passwords

    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Try multiple regex patterns
        db_match = re.search(r"DB='([^']*)'", line)
        pw_match = re.search(r"PASSWORD='([^']*)'", line)

        if db_match and pw_match:
            passwords[db_match.group(1)] = pw_match.group(1)

    return passwords


def read_db_password_from_wp_config(hestia: HestiaClient, user: str, domain: str) -> Optional[str]:
    """Try to read DB password from wp-config.php in the web root."""
    wp_config = f"/home/{user}/web/{domain}/public_html/wp-config.php"
    code, content, _ = hestia.exec(f"cat {wp_config} 2>/dev/null", warn_on_error=False)
    if code == 0:
        match = re.search(r"define\s*\(\s*'DB_PASSWORD'\s*,\s*'([^']+)'", content)
        if match:
            return match.group(1)
    return None


def read_db_password_from_my_cnf(hestia: HestiaClient, user: str, domain: str) -> Optional[str]:
    """Try to read DB password from .my.cnf in the web root."""
    my_cnf = f"/home/{user}/web/{domain}/.my.cnf"
    code, content, _ = hestia.exec(f"cat {my_cnf} 2>/dev/null", warn_on_error=False)
    if code == 0:
        match = re.search(r"password\s*=\s*(\S+)", content)
        if match:
            return match.group(1)
    return None


def main():
    parser = argparse.ArgumentParser(description="Fix DB passwords after migration")
    parser.add_argument("--config", "-c", required=True, help="Path to config.yaml")
    parser.add_argument("--domains", "-d", help="Path to domains.txt (one domain per line)")
    parser.add_argument("--debug", action="store_true", help="Show raw db.conf content")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    hestia_cfg = config["hestia"]
    aapanel_cfg = config["aapanel"]
    target_domains = load_domains(args.domains) if args.domains else None

    # 1. Connect to HestiaCP
    console.print("[cyan]Reading passwords from HestiaCP...[/cyan]")
    hestia = HestiaClient(
        host=hestia_cfg.get("host", "localhost"),
        port=hestia_cfg.get("port", 22),
        user=hestia_cfg.get("user", "root"),
        password=hestia_cfg.get("password"),
        ssh_key=hestia_cfg.get("ssh_key"),
        local=hestia_cfg.get("local", False),
    )
    hestia.connect()

    users = hestia.get_users()

    # Build: domain → [(db_name, db_user, db_pass)]
    fixes: List[Dict] = []

    for user in users:
        # Read all passwords from db.conf
        db_passwords = read_db_passwords_from_conf(hestia, user)

        if args.debug and db_passwords:
            console.print(f"\n[bold]User {user} — db.conf passwords:[/bold]")
            for name, pw in db_passwords.items():
                console.print(f"  {name}: {'***' if pw else 'EMPTY'}")
            if not db_passwords:
                # Show raw content
                db_conf = f"/usr/local/hestia/data/users/{user}/db.conf"
                code, content, _ = hestia.exec(f"cat {db_conf} 2>/dev/null", warn_on_error=False)
                console.print(f"  [dim]Raw db.conf ({'exists' if code==0 else 'NOT FOUND'}):[/dim]")
                for line in content.split("\n")[:5]:
                    console.print(f"  [dim]  {line[:200]}[/dim]")

        # Get databases for this user (from CLI, no passwords)
        dbs = hestia.get_databases(user)

        domains = hestia.get_web_domains(user)
        for domain in domains:
            if target_domains and domain not in target_domains:
                continue

            # Find matching DBs for this domain
            matched = hestia.get_databases_for_domain(user, domain)
            for db in matched:
                db_name = db.get("DATABASE") or db.get("DB", "")
                db_user = db.get("DBUSER") or db.get("USER", "")

                # Get password: db.conf first, then wp-config, then .my.cnf
                db_pass = db_passwords.get(db_name, "")
                if not db_pass:
                    db_pass = read_db_password_from_wp_config(hestia, user, domain) or ""
                if not db_pass:
                    db_pass = read_db_password_from_my_cnf(hestia, user, domain) or ""

                if db_name and db_user:
                    fixes.append({
                        "domain": domain,
                        "db_name": db_name,
                        "db_user": db_user,
                        "db_pass": db_pass,
                    })

    hestia.disconnect()

    # Show summary
    with_pass = [f for f in fixes if f["db_pass"]]
    without_pass = [f for f in fixes if not f["db_pass"]]

    console.print(f"\nFound {len(fixes)} DBs: [green]{len(with_pass)} with password[/green], "
                  f"[red]{len(without_pass)} without[/red]")

    if without_pass:
        console.print("[yellow]DBs without password (will be skipped):[/yellow]")
        for f in without_pass:
            console.print(f"  - {f['domain']}: {f['db_name']}")

    if not with_pass:
        console.print("[red]No passwords found![/red]")
        if not args.debug:
            console.print("[yellow]Try running with --debug to see raw db.conf content[/yellow]")
        return

    # Preview
    console.print(f"\n[bold]Passwords to fix ({len(with_pass)}):[/bold]")
    for f in with_pass[:10]:
        console.print(f"  {f['domain']}: {f['db_name']} user={f['db_user']}")
    if len(with_pass) > 10:
        console.print(f"  ... and {len(with_pass) - 10} more")

    confirm = input(f"\nUpdate {len(with_pass)} MySQL users? [y/N]: ").strip().lower()
    if confirm not in ("y", "yes"):
        console.print("[red]Aborted[/red]")
        return

    # 2. Connect to aaPanel, run ALTER USER
    console.print("\n[cyan]Updating passwords on aaPanel...[/cyan]")
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
    console.print(f"MySQL auth: {mysql_bin}")

    fixed, failed = 0, 0
    for f in with_pass:
        sql = (
            f"CREATE USER IF NOT EXISTS '{f['db_user']}'@'%' IDENTIFIED BY '{f['db_pass']}';\n"
            f"ALTER USER '{f['db_user']}'@'%' IDENTIFIED BY '{f['db_pass']}';\n"
            f"CREATE USER IF NOT EXISTS '{f['db_user']}'@'localhost' IDENTIFIED BY '{f['db_pass']}';\n"
            f"ALTER USER '{f['db_user']}'@'localhost' IDENTIFIED BY '{f['db_pass']}';\n"
            f"FLUSH PRIVILEGES;\n"
        )
        sql_file = f"{ssh.tmp_dir}/fix_{f['db_name']}.sql"
        ssh.ensure_dir(ssh.tmp_dir)

        if ssh.local:
            with open(sql_file, "w") as fh:
                fh.write(sql)
        else:
            with ssh._sftp.open(sql_file, "w") as fh:
                fh.write(sql)

        code, _, stderr = ssh.exec(f"{mysql_bin} < {sql_file}", timeout=20)
        ssh.exec(f"rm -f {sql_file}", warn_on_error=False)

        if code == 0:
            console.print(f"  [green]✓[/green] {f['domain']}: {f['db_user']}")
            fixed += 1
        else:
            console.print(f"  [red]✗[/red] {f['domain']}: {stderr[:120]}")
            failed += 1

    ssh.disconnect()
    console.print(f"\n[bold green]Fixed: {fixed}, Failed: {failed}[/bold green]")


if __name__ == "__main__":
    main()
