#!/usr/bin/env python3
"""
Fix database user passwords after migration.

Reads the REAL passwords from HestiaCP db.conf files,
then updates the corresponding MySQL users on aaPanel.
No file transfer needed — just reads configs and runs ALTER USER.
"""

import argparse
import re
import sys
from pathlib import Path

import yaml

from modules.hestia import HestiaClient
from modules.aapanel_ssh import AAPanelSSH
from modules.utils import console, log


def main():
    parser = argparse.ArgumentParser(description="Fix DB passwords after migration")
    parser.add_argument("--config", "-c", required=True, help="Path to config.yaml")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    hestia_cfg = config["hestia"]
    aapanel_cfg = config["aapanel"]

    # 1. Connect to HestiaCP, extract all DB passwords
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

    # Collect all unique databases from all users
    users = hestia.get_users()
    all_dbs = []
    for user in users:
        dbs = hestia.get_databases(user)
        for db in dbs:
            db["_user"] = user
        all_dbs.extend(dbs)

    hestia.disconnect()

    # Filter: only DBs with passwords
    fixable = [db for db in all_dbs if db.get("PASSWORD")]
    no_pass = [db for db in all_dbs if not db.get("PASSWORD")]

    console.print(f"Found {len(all_dbs)} DBs: {len(fixable)} with password, {len(no_pass)} without")

    if no_pass:
        console.print("[yellow]DBs without password (skipped):[/yellow]")
        for db in no_pass:
            name = db.get("DATABASE") or db.get("DB", "?")
            console.print(f"  - {name}")

    if not fixable:
        console.print("[red]No DBs with passwords to fix![/red]")
        return

    # Show preview
    console.print("\n[bold]Passwords to fix:[/bold]")
    for db in fixable[:10]:
        name = db.get("DATABASE") or db.get("DB", "?")
        user = db.get("DBUSER") or db.get("USER", "?")
        console.print(f"  {name}: user={user}")

    if len(fixable) > 10:
        console.print(f"  ... and {len(fixable) - 10} more")

    confirm = input(f"\nUpdate {len(fixable)} MySQL users? [y/N]: ").strip().lower()
    if confirm not in ("y", "yes"):
        console.print("[red]Aborted[/red]")
        return

    # 2. Connect to aaPanel, run ALTER USER for each
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

    fixed = 0
    failed = 0
    for db in fixable:
        db_name = db.get("DATABASE") or db.get("DB", "")
        db_user = db.get("DBUSER") or db.get("USER", "")
        db_pass = db.get("PASSWORD", "")
        db_host = db.get("HOST", "localhost")

        if not db_name or not db_user or not db_pass:
            continue

        # Build ALTER USER for both % and localhost
        sql = (
            f"ALTER USER IF EXISTS '{db_user}'@'%' IDENTIFIED BY '{db_pass}';\n"
            f"ALTER USER IF EXISTS '{db_user}'@'localhost' IDENTIFIED BY '{db_pass}';\n"
            f"FLUSH PRIVILEGES;\n"
        )

        sql_file = f"{ssh.tmp_dir}/fix_pass_{db_name}.sql"
        ssh.ensure_dir(ssh.tmp_dir)

        if ssh.local:
            with open(sql_file, "w") as f:
                f.write(sql)
        else:
            ssh._sftp.open(sql_file, "w").write(sql)

        code, _, stderr = ssh.exec(f"{mysql_bin} < {sql_file}", timeout=15)
        ssh.exec(f"rm -f {sql_file}", warn_on_error=False)

        if code == 0:
            console.print(f"  [green]✓[/green] {db_name}: user={db_user}")
            fixed += 1
        else:
            # Try without IF EXISTS (older MySQL)
            sql2 = (
                f"SET @x = (SELECT COUNT(*) FROM mysql.user WHERE user='{db_user}');\n"
                f"SET @s = IF(@x > 0, 'ALTER USER ''{db_user}''@''%'' IDENTIFIED BY ''{db_pass}''', 'SELECT 1');\n"
                f"PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;\n"
                f"FLUSH PRIVILEGES;\n"
            )
            sql_file2 = f"{ssh.tmp_dir}/fix_pass2_{db_name}.sql"
            if ssh.local:
                with open(sql_file2, "w") as f:
                    f.write(sql2)
            else:
                ssh._sftp.open(sql_file2, "w").write(sql2)

            code2, _, stderr2 = ssh.exec(f"{mysql_bin} < {sql_file2}", timeout=15)
            ssh.exec(f"rm -f {sql_file2}", warn_on_error=False)

            if code2 == 0:
                console.print(f"  [green]✓[/green] {db_name}: user={db_user} (fallback)")
                fixed += 1
            else:
                console.print(f"  [red]✗[/red] {db_name}: {stderr2[:100]}")
                failed += 1

    ssh.disconnect()

    console.print(f"\n[bold green]Fixed: {fixed}, Failed: {failed}[/bold green]")


if __name__ == "__main__":
    main()
