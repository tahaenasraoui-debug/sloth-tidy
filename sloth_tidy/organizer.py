from __future__ import annotations

import shutil
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from sloth_tidy.config import HISTORY_FILE, SlothConfig, ensure_config_dir
from sloth_tidy.extractors import extract_file_context
from sloth_tidy.llm import CategoryEnum, FileClassification, LocalLLMClient

COMPOUND_EXTENSIONS = {".tar.gz", ".tar.bz2", ".tar.xz", ".user.js"}


class HistoryEntry(BaseModel):
    """Audit log entry stored in `~/.config/sloth-tidy/history.jsonl`."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    timestamp: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    original_path: str
    destination_path: str
    category: str
    subfolder: str
    confidence: float
    summary: str
    size_bytes: int
    dry_run: bool = False
    undone: bool = False


def split_stem_and_suffix(filename: str) -> tuple[str, str]:
    """Split filename into stem and extension, preserving compound extensions like `.tar.gz`."""
    lower = filename.lower()
    for comp in COMPOUND_EXTENSIONS:
        if lower.endswith(comp):
            stem = filename[: -len(comp)]
            suffix = filename[-len(comp) :]
            return (stem or "file"), suffix

    p = Path(filename)
    return (p.stem or "file"), p.suffix


def resolve_collision(
    target_path: Path, reserved_paths: set[Path] | None = None
) -> Path:
    """Never overwrite existing files. Append `-1`, `-2`, etc. if a path already exists or is reserved."""
    reserved = reserved_paths or set()

    def is_taken(candidate: Path) -> bool:
        if candidate.exists():
            return True
        if not reserved:
            return False
        return candidate in reserved or candidate.resolve() in reserved

    if not is_taken(target_path):
        return target_path

    parent = target_path.parent
    stem, suffix = split_stem_and_suffix(target_path.name)

    counter = 1
    while True:
        candidate = parent / f"{stem}-{counter}{suffix}"
        if not is_taken(candidate):
            return candidate
        counter += 1


def is_ignored_file(file_path: Path, config: SlothConfig) -> bool:
    """Check whether a path is hidden, temporary, incomplete, or already inside target_root."""
    if not file_path.exists() or not file_path.is_file() or file_path.is_symlink():
        return True

    name = file_path.name
    for prefix in config.ignored_prefixes:
        if name.startswith(prefix):
            return True

    lower_name = name.lower()
    for ext in config.ignored_extensions:
        if lower_name.endswith(ext.lower()):
            return True

    try:
        resolved = file_path.resolve()
        target_resolved = config.target_root.resolve()
        if target_resolved in resolved.parents or resolved == target_resolved:
            return True
    except Exception:
        return True

    return False


def _is_default_history_path(history_path: Path) -> bool:
    return history_path.expanduser().resolve() == HISTORY_FILE.expanduser().resolve()


def append_history(entry: HistoryEntry, history_path: Path = HISTORY_FILE) -> None:
    """Append a single operation record to the JSONL audit history."""
    if _is_default_history_path(history_path):
        ensure_config_dir()
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as f:
        f.write(entry.model_dump_json() + "\n")


def read_history(history_path: Path = HISTORY_FILE) -> list[HistoryEntry]:
    """Read all history records from the JSONL audit log."""
    if not history_path.exists():
        return []
    entries: list[HistoryEntry] = []
    with history_path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entries.append(HistoryEntry.model_validate_json(stripped))
            except Exception:
                continue
    return entries


def write_history(
    entries: list[HistoryEntry], history_path: Path = HISTORY_FILE
) -> None:
    """Atomically rewrite the history JSONL file after undo updates."""
    if _is_default_history_path(history_path):
        ensure_config_dir()
    history_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = history_path.with_suffix(".jsonl.tmp")
    with temp_path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(entry.model_dump_json() + "\n")
    temp_path.replace(history_path)


class SlothOrganizer:
    """Coordinates file extraction, LLM classification, collision-safe moving, and rollback."""

    def __init__(
        self,
        config: SlothConfig,
        llm_client: LocalLLMClient | None = None,
        history_path: Path = HISTORY_FILE,
    ) -> None:
        self.config = config
        self.llm = llm_client or LocalLLMClient(config)
        self.history_path = history_path
        self._simulated_destinations: set[Path] = set()
        self._dry_run_lock = threading.Lock()

    def reset_simulated_destinations(self) -> None:
        """Clear simulated dry-run destination paths tracked for collision previewing."""
        with self._dry_run_lock:
            self._simulated_destinations.clear()

    def compute_destination_parts(
        self,
        classification: FileClassification,
        reserved_paths: set[Path] | None = None,
    ) -> tuple[str, str, Path]:
        """Compute category, subfolder, and collision-safe destination path."""
        if classification.confidence < self.config.confidence_threshold:
            category_dir = CategoryEnum.MISC.value
            subfolder_dir = "Low_Confidence_Review"
        else:
            category_dir = classification.category.value
            subfolder_dir = classification.subfolder

        raw_dest = (
            self.config.target_root
            / category_dir
            / subfolder_dir
            / classification.suggested_filename
        )
        return (
            category_dir,
            subfolder_dir,
            resolve_collision(raw_dest, reserved_paths=reserved_paths),
        )

    def compute_destination(self, classification: FileClassification) -> Path:
        """Backward-compatible helper returning only the resolved path."""
        _, _, destination = self.compute_destination_parts(classification)
        return destination

    def organize_file(
        self, file_path: Path, dry_run: bool = False
    ) -> HistoryEntry | None:
        """Extract context, classify via local LLM, and safely move the file."""
        is_dry_run = bool(dry_run)
        file_path = file_path.expanduser().resolve()
        if is_ignored_file(file_path, self.config):
            return None

        context = extract_file_context(file_path, self.config)
        classification = self.llm.classify(context)

        if is_dry_run:
            with self._dry_run_lock:
                category, subfolder, destination = self.compute_destination_parts(
                    classification,
                    reserved_paths=self._simulated_destinations,
                )
                self._simulated_destinations.add(destination.resolve())
        else:
            category, subfolder, destination = self.compute_destination_parts(classification)

        entry = HistoryEntry(
            original_path=str(file_path),
            destination_path=str(destination),
            category=category,
            subfolder=subfolder,
            confidence=classification.confidence,
            summary=classification.summary,
            size_bytes=context.size_bytes,
            dry_run=is_dry_run,
            undone=False,
        )

        if not is_dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Re-verify collision right before moving in case of concurrent events
            final_dest = resolve_collision(destination)
            entry.destination_path = str(final_dest)
            shutil.move(str(file_path), str(final_dest))
            append_history(entry, self.history_path)

        return entry

    def undo(self, count: int = 1) -> list[HistoryEntry]:
        """Restore the last N organized files back to their original paths."""
        if count <= 0:
            return []

        entries = read_history(self.history_path)
        active_indices = [
            idx
            for idx, item in enumerate(entries)
            if not item.undone and not item.dry_run
        ]

        target_indices = list(reversed(active_indices))[:count]
        restored: list[HistoryEntry] = []

        for idx in target_indices:
            entry = entries[idx]
            current_loc = Path(entry.destination_path)
            orig_loc = Path(entry.original_path)

            if not current_loc.exists():
                continue

            orig_loc.parent.mkdir(parents=True, exist_ok=True)
            safe_restore_path = resolve_collision(orig_loc)
            shutil.move(str(current_loc), str(safe_restore_path))

            entry.original_path = str(safe_restore_path)
            entry.undone = True
            restored.append(entry)

        if restored:
            write_history(entries, self.history_path)

        return restored
