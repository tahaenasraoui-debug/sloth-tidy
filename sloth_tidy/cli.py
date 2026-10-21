from __future__ import annotations

import time
from collections import Counter
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

from sloth_tidy.config import HISTORY_FILE, load_config
from sloth_tidy.organizer import (
    HistoryEntry,
    SlothOrganizer,
    is_ignored_file,
    read_history,
)
from sloth_tidy.watcher import SlothDaemon

app = typer.Typer(
    name="sloth-tidy",
    help="SlothTidy: Zero-friction, 100% local AI file organization daemon.",
    add_completion=False,
)
console = Console()

SLOTH_ASCII = r"""
   `""==,,__
      `"==..__"=..__ _    _..-==""_
           .-,`"=/ /\ \""/_)==""``
          ( (    | | | \/ |
           \ '.  |  \;  \ /
            |  \ |   |   ||
       ,-._.'  |_|   |   ||
      .\_/\     -'   ;   Y
     |  `  |        /    |-.
     '. __/_    _.-'     /'
            `'-.._____.-'
"""


def _format_bytes(size: int) -> str:
    val = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024.0:
            return f"{val:.1f} {unit}"
        val /= 1024.0
    return f"{val:.1f} TB"


def _print_entry_event(entry: HistoryEntry) -> None:
    mode_tag = "[yellow][DRY-RUN][/yellow] " if entry.dry_run else "[green][TIDIED][/green] "
    orig_name = Path(entry.original_path).name
    dest_rel = f"{entry.category}/{entry.subfolder}/{Path(entry.destination_path).name}"
    conf_pct = f"{int(entry.confidence * 100)}%"
    console.print(
        f"{mode_tag}[bold cyan]{orig_name}[/bold cyan] -> "
        f"[bold magenta]{dest_rel}[/bold magenta] "
        f"[dim]({conf_pct} | {entry.summary})[/dim]"
    )


@app.command()
def run(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Simulate categorization without moving any files."
    ),
    model: Optional[str] = typer.Option(
        None, "--model", "-m", help="Override the local Ollama model name."
    ),
) -> None:
    """Start the background file-monitoring daemon."""
    config = load_config()
    if model:
        config.model_name = model

    organizer = SlothOrganizer(config)

    def on_error(path: Path, exc: Exception) -> None:
        console.print(f"[bold red][ERROR][/bold red] Failed processing {path.name}: {exc}")

    daemon = SlothDaemon(
        organizer=organizer,
        dry_run=dry_run,
        on_organized_callback=_print_entry_event,
        on_error_callback=on_error,
    )

    active_dirs = daemon.start()
    dirs_str = "\n".join(f"  • {d}" for d in active_dirs) or "  [red]No valid watch directories found![/red]"

    banner = (
        f"[bold green]{SLOTH_ASCII}[/bold green]\n"
        f"[bold]SlothTidy Daemon Active[/bold] — conserving your energy, Abass le roi des Bobbys.\n\n"
        f"[cyan]Inference Endpoint:[/cyan] {config.ollama_endpoint} ([bold]{config.model_name}[/bold])\n"
        f"[cyan]Target Root:[/cyan]        {config.target_root}\n"
        f"[cyan]Mode:[/cyan]               {'DRY-RUN (no files moved)' if dry_run else 'LIVE ATOMIC MOVES'}\n"
        f"[cyan]Watching:[/cyan]\n{dirs_str}\n\n"
        f"[dim]Press Ctrl+C to let the sloth go back to sleep.[/dim]"
    )
    console.print(Panel(banner, title="🦥 Slow & Steady File Daemon", border_style="green"))

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        console.print("\n[yellow]🦥 Yawning and shutting down watcher threads gracefully...[/yellow]")
        daemon.stop()
        console.print("[bold green]SlothTidy stopped cleanly.[/bold green]")


@app.command()
def scan(
    directory: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        readable=True,
        help="Folder to scan and organize in a single pass.",
    ),
    recursive: bool = typer.Option(
        False, "--recursive", "-r", help="Recursively scan subdirectories."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview organization without moving files."
    ),
    model: Optional[str] = typer.Option(
        None, "--model", "-m", help="Override the local Ollama model name."
    ),
) -> None:
    """One-shot scan and tidy of a specific directory."""
    config = load_config()
    if model:
        config.model_name = model

    organizer = SlothOrganizer(config)
    target_dir = directory.expanduser().resolve()

    iterator = target_dir.rglob("*") if recursive else target_dir.glob("*")
    candidates = [p for p in iterator if not is_ignored_file(p, config)]

    if not candidates:
        console.print(f"[yellow]🦥 Nothing to tidy in {target_dir}. Time for a nap![/yellow]")
        return

    results: list[HistoryEntry] = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
    ) as progress:
        task = progress.add_task("🦥 Slowly classifying files...", total=len(candidates))
        for file_path in candidates:
            progress.update(task, description=f"🦥 Inspecting [cyan]{file_path.name}[/cyan]...")
            entry = organizer.organize_file(file_path, dry_run=dry_run)
            if entry:
                results.append(entry)
            progress.advance(task)

    table = Table(
        title=f"SlothTidy Scan Summary ({'DRY-RUN' if dry_run else 'EXECUTED'})",
        box=box.ROUNDED,
        show_lines=True,
    )
    table.add_column("Original File", style="cyan")
    table.add_column("Category / Subfolder", style="magenta")
    table.add_column("New Filename", style="green")
    table.add_column("Conf.", justify="right")
    table.add_column("Summary", style="dim")

    for item in results:
        table.add_row(
            Path(item.original_path).name,
            f"{item.category}/{item.subfolder}",
            Path(item.destination_path).name,
            f"{int(item.confidence * 100)}%",
            item.summary,
        )

    console.print(table)


@app.command()
def history(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of recent entries to display."),
) -> None:
    """Show recently organized files and the sloth's lazy audit log."""
    entries = read_history(HISTORY_FILE)
    if not entries:
        console.print("[yellow]🦥 The audit log is empty. No files have been moved yet.[/yellow]")
        return

    recent = list(reversed(entries))[:limit]
    table = Table(
        title=f"🦥 SlothTidy Audit Log (Last {len(recent)} Operations)",
        box=box.SIMPLE_HEAVY,
    )
    table.add_column("ID", style="dim")
    table.add_column("Timestamp (UTC)", style="cyan")
    table.add_column("Source", style="yellow")
    table.add_column("Destination", style="green")
    table.add_column("Category", style="magenta")
    table.add_column("Status")

    for item in recent:
        status = "[red]UNDONE[/red]" if item.undone else "[green]ACTIVE[/green]"
        table.add_row(
            item.id,
            item.timestamp[:19].replace("T", " "),
            Path(item.original_path).name,
            f"{item.category}/{item.subfolder}/{Path(item.destination_path).name}",
            item.category,
            status,
        )

    console.print(table)


@app.command()
def undo(
    count: int = typer.Argument(
        1, help="Number of most recent file moves to roll back."
    ),
) -> None:
    """Roll back the last N file operations to their original paths."""
    config = load_config()
    organizer = SlothOrganizer(config)
    restored = organizer.undo(count=count)

    if not restored:
        console.print("[yellow]🦥 No active file moves found to undo.[/yellow]")
        return

    for item in restored:
        console.print(
            f"[bold green]✔ Restored:[/bold green] "
            f"[magenta]{Path(item.destination_path).name}[/magenta] -> "
            f"[cyan]{item.original_path}[/cyan]"
        )
    console.print(f"\n[bold]Rolled back {len(restored)} operation(s).[/bold]")


@app.command()
def report() -> None:
    """Print a 'Sloth Energy Saved' report card."""
    entries = [e for e in read_history(HISTORY_FILE) if not e.undone and not e.dry_run]
    total_files = len(entries)
    total_bytes = sum(e.size_bytes for e in entries)

    # Assume each manual file inspection, folder creation, and rename takes ~25 seconds
    seconds_saved = total_files * 25
    minutes_saved = seconds_saved / 60.0
    leaves_munched = max(1, int(total_files * 1.8))
    cloud_cost_saved = total_files * 0.004  # Estimated cloud vision/LLM API cost avoided

    cat_counts = Counter(e.category for e in entries)
    breakdown_lines = [
        f"  • [bold magenta]{cat:<15}[/bold magenta]: {cnt} file(s)"
        for cat, cnt in cat_counts.most_common()
    ]
    breakdown_str = "\n".join(breakdown_lines) if breakdown_lines else "  • No files sorted yet."

    card = (
        f"[bold green]{SLOTH_ASCII}[/bold green]\n"
        f"While you rested, [bold cyan]SlothTidy[/bold cyan] sorted [bold yellow]{total_files}[/bold yellow] "
        f"files ({_format_bytes(total_bytes)}) and saved you [bold green]{minutes_saved:.1f} minutes[/bold green] "
        f"of manual toil.\n\n"
        f"[bold]Category Breakdown:[/bold]\n{breakdown_str}\n\n"
        f"[bold]Sloth Conservation Metrics:[/bold]\n"
        f"  • Mental Decisions Avoided : [bold]{total_files * 3}[/bold]\n"
        f"  • Cecropia Leaves Munched  : [bold]{leaves_munched} 🌿[/bold]\n"
        f"  • Cloud API Dollars Spent  : [bold green]$0.00[/bold green] (Est. ${cloud_cost_saved:.2f} saved)\n"
        f"  • Sensitive Bytes Leaked   : [bold green]0 B (100% Local Inference)[/bold green]"
    )

    console.print(
        Panel(
            card,
            title="🦥 Sloth Energy Saved Report Card",
            border_style="bright_green",
            padding=(1, 2),
        )
    )


if __name__ == "__main__":
    app()
