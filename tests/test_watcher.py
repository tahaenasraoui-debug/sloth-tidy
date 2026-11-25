from __future__ import annotations

import builtins
import errno
import fcntl
import time
from pathlib import Path
from unittest.mock import MagicMock

from watchdog.events import DirCreatedEvent, FileCreatedEvent, FileMovedEvent

from sloth_tidy.config import SlothConfig
from sloth_tidy.llm import CategoryEnum, FileClassification, LocalLLMClient
from sloth_tidy.organizer import SlothOrganizer
from sloth_tidy.watcher import SlothEventHandler, _is_file_locked, wait_until_file_ready


def test_is_file_locked_detects_real_flock_and_release(tmp_path: Path):
    """An active exclusive flock lock on an open descriptor must report as locked."""
    target = tmp_path / "download.bin"
    target.write_bytes(b"payload")

    with target.open("rb+") as writer:
        fcntl.flock(writer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert _is_file_locked(target) is True
        fcntl.flock(writer.fileno(), fcntl.LOCK_UN)

    assert _is_file_locked(target) is False


def test_is_file_locked_handles_blocking_io_and_ewouldblock(tmp_path: Path, monkeypatch):
    """Contended lock errors (BlockingIOError / EWOULDBLOCK / EAGAIN / EACCES) return True."""
    target = tmp_path / "locked.txt"
    target.write_text("data")

    for err in (
        BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable"),
        OSError(errno.EWOULDBLOCK, "Operation would block"),
        OSError(errno.EAGAIN, "Try again"),
        OSError(errno.EACCES, "Permission denied"),
    ):
        monkeypatch.setattr(fcntl, "flock", MagicMock(side_effect=err))
        assert _is_file_locked(target) is True


def test_is_file_locked_missing_readonly_and_missing_fcntl(tmp_path: Path, monkeypatch):
    """Missing or unopenable files return True; missing fcntl falls back gracefully."""
    missing = tmp_path / "missing.txt"
    assert _is_file_locked(missing) is True

    readonly = tmp_path / "readonly.txt"
    readonly.write_text("locked-by-perm")
    readonly.chmod(0o444)
    try:
        assert _is_file_locked(readonly) is True
    finally:
        readonly.chmod(0o644)

    # Simulate platforms without fcntl (ImportError)
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "fcntl":
            raise ImportError("No module named 'fcntl'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    writable = tmp_path / "writable.txt"
    writable.write_text("ok")
    assert _is_file_locked(writable) is False


def test_wait_until_file_ready_handles_zero_byte_files_quickly(tmp_path: Path):
    """0-byte files that remain stable and unlocked become ready promptly instead of timing out."""
    empty_file = tmp_path / "empty.txt"
    empty_file.touch()
    assert empty_file.stat().st_size == 0

    start = time.monotonic()
    ready = wait_until_file_ready(
        empty_file,
        debounce_seconds=0.04,
        max_wait_seconds=5.0,
    )
    elapsed = time.monotonic() - start

    assert ready is True
    assert elapsed < 1.0


def test_wait_until_file_ready_timeout_and_missing_file(tmp_path: Path, monkeypatch):
    """Locked files time out at max_wait_seconds and missing files return False immediately."""
    missing = tmp_path / "gone.txt"
    assert wait_until_file_ready(missing, debounce_seconds=0.04, max_wait_seconds=1.0) is False

    locked = tmp_path / "busy.bin"
    locked.write_bytes(b"123")
    monkeypatch.setattr("sloth_tidy.watcher._is_file_locked", lambda _p: True)

    start = time.monotonic()
    ready = wait_until_file_ready(locked, debounce_seconds=0.04, max_wait_seconds=0.1)
    elapsed = time.monotonic() - start

    assert ready is False
    assert elapsed < 1.0


def test_wait_until_file_ready_waits_for_unlock(tmp_path: Path, monkeypatch):
    """File becomes ready once size stabilizes and lock clears."""
    target = tmp_path / "growing.txt"
    target.write_text("abc")

    lock_states = iter([True, False, False])
    monkeypatch.setattr(
        "sloth_tidy.watcher._is_file_locked",
        lambda _p: next(lock_states, False),
    )

    assert wait_until_file_ready(target, debounce_seconds=0.02, max_wait_seconds=1.0) is True


def test_sloth_event_handler_dispatches_and_ignores(tmp_path: Path, monkeypatch):
    """SlothEventHandler ignores directories/temp files and dispatches valid files."""
    watch_dir = tmp_path / "Downloads"
    target_dir = tmp_path / "Organized"
    watch_dir.mkdir()
    target_dir.mkdir()

    cfg = SlothConfig(watch_dirs=[watch_dir], target_root=target_dir, debounce_seconds=0.5)
    mock_llm = MagicMock(spec=LocalLLMClient)
    mock_llm.classify.return_value = FileClassification(
        category=CategoryEnum.MISC,
        suggested_filename="2026-10-19-sample.txt",
        subfolder="Notes",
        confidence=0.9,
        summary="Sample note.",
    )
    organizer = SlothOrganizer(cfg, llm_client=mock_llm, history_path=tmp_path / "history.jsonl")

    monkeypatch.setattr("sloth_tidy.watcher.wait_until_file_ready", lambda *_args, **_kw: True)

    organized: list[str] = []
    handler = SlothEventHandler(
        organizer=organizer,
        dry_run=True,
        on_organized_callback=lambda entry: organized.append(entry.destination_path),
    )
    try:
        # Directory and .part events must be ignored
        part_file = watch_dir / "file.part"
        part_file.write_text("partial")
        handler.on_created(DirCreatedEvent(str(watch_dir)))
        handler.on_created(FileCreatedEvent(str(part_file)))

        sample = watch_dir / "sample.txt"
        sample.write_text("hello")
        handler.on_moved(FileMovedEvent(str(watch_dir / "tmp"), str(sample)))

        deadline = time.monotonic() + 2.0
        while not organized and time.monotonic() < deadline:
            time.sleep(0.02)

        assert len(organized) == 1
        assert organized[0].endswith("2026-10-19-sample.txt")
    finally:
        handler.shutdown()
