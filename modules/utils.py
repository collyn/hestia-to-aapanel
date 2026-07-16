"""
Shared utilities: logging, progress bars, validation helpers.
"""

import os
import sys
import json
import logging
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.panel import Panel
from rich import print as rprint

# ---------------------------------------------------------------------------
# Console & Logging
# ---------------------------------------------------------------------------

console = Console()

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOG_FILE = LOG_DIR / f"migration_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        RichHandler(console=console, rich_tracebacks=True, show_time=False),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)

log = logging.getLogger("hestia2aa")


def get_state_path() -> Path:
    """Return path to the migration state file."""
    return Path(__file__).resolve().parent.parent / "state.json"


# ---------------------------------------------------------------------------
# State Management (resume capability)
# ---------------------------------------------------------------------------

class MigrationState:
    """Persistent state for resumable migration."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or get_state_path()
        self.data: Dict[str, Any] = {
            "version": 1,
            "started_at": None,
            "updated_at": None,
            "completed": False,
            "total_sites": 0,
            "migrated_sites": [],
            "failed_sites": [],
            "current_phase": "init",
            "site_details": {},  # domain → {site_id, db_created, ssl_deployed, ...}
        }
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path) as f:
                    saved = json.load(f)
                self.data.update(saved)
                log.info(f"Loaded existing state: {len(self.data['migrated_sites'])} sites already migrated")
            except (json.JSONDecodeError, KeyError):
                log.warning("Corrupted state file, starting fresh")

    def save(self):
        self.data["updated_at"] = datetime.now().isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self.data, f, indent=2, default=str)

    def mark_site_migrated(self, domain: str, details: Dict[str, Any]):
        if domain not in self.data["migrated_sites"]:
            self.data["migrated_sites"].append(domain)
        self.data["site_details"][domain] = details
        self.save()

    def mark_site_failed(self, domain: str, error: str):
        if domain not in self.data["failed_sites"]:
            self.data["failed_sites"].append(domain)
        self.data["site_details"][domain] = {"error": error}
        self.save()

    def is_migrated(self, domain: str) -> bool:
        return domain in self.data["migrated_sites"]

    def is_failed(self, domain: str) -> bool:
        return domain in self.data["failed_sites"]


# ---------------------------------------------------------------------------
# Progress Helpers
# ---------------------------------------------------------------------------

def create_progress() -> Progress:
    """Create a rich Progress bar with standard columns."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    )


# ---------------------------------------------------------------------------
# Display Helpers
# ---------------------------------------------------------------------------

def print_banner():
    """Print migration tool banner."""
    banner = """
╔══════════════════════════════════════════════════════════╗
║     HestiaCP → aaPanel Migration Tool v1.0              ║
╚══════════════════════════════════════════════════════════╝
    """
    console.print(banner, style="bold cyan")


def print_summary(state: MigrationState):
    """Print migration summary table."""
    table = Table(title="Migration Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Total sites", str(state.data["total_sites"]))
    table.add_row("Migrated", str(len(state.data["migrated_sites"])))
    table.add_row("Failed", str(len(state.data["failed_sites"])))

    if state.data["migrated_sites"]:
        table.add_row("Migrated sites", ", ".join(state.data["migrated_sites"]))
    if state.data["failed_sites"]:
        table.add_row("Failed sites", ", ".join(state.data["failed_sites"]))
    table.add_row("Log file", str(LOG_FILE))

    console.print(table)


def print_error_context(domain: str, error: str, details: Optional[Dict] = None):
    """Print a formatted error message."""
    text = f"[bold red]✗ {domain}[/bold red]\n  Error: {error}"
    if details:
        text += f"\n  Details: {json.dumps(details, indent=2)}"
    console.print(Panel(text, border_style="red"))


def print_success(domain: str, info: Optional[Dict] = None):
    """Print a formatted success message."""
    text = f"[bold green]✓ {domain}[/bold green]"
    if info:
        for k, v in info.items():
            text += f"\n  {k}: {v}"
    console.print(Panel(text, border_style="green"))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_domain(domain: str) -> bool:
    """Basic domain name validation."""
    if not domain or len(domain) > 253:
        return False
    import re
    pattern = r'^([a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$'
    return bool(re.match(pattern, domain))


def ensure_dir(path: str, is_remote: bool = False) -> str:
    """Return mkdir command; no-op here (used by SSH modules)."""
    return path


def checksum_md5(filepath: str) -> str:
    """Compute MD5 checksum of a local file."""
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()
