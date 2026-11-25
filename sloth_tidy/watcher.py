from __future__ import annotations

import errno
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from watchdog.events import FileMovedEvent, FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from sloth_tidy.config import SlothConfig
from sloth_tidy.organizer import HistoryEntry, SlothOrganizer, is_ignored_file


def _is_file_locked(file_path: Path) -> bool:
    """Check if a file is locked or still held open for exclusive writing."""
    if not file_path.exists():
        return True

    try:
        import fcntl
    except ImportError:
        fcntl = None

    # Try opening in read/write binary mode without truncating
    try:
        with file_path.open("rb+") as f:
            if fcntl is not None:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                except OSError as exc:
                    if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                        return True
                else:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
    except OSError:
        return True

    return not os.access(file_path, os.R_OK | os.W_OK)


def wait_until_file_ready(
    file_path: Path,
    debounce_seconds: float = 2.5,
    max_wait_seconds: float = 60.0,
) -> bool:
    """
    Wait until a file's byte size stabilizes across two consecutive checks
    and no write lock is held. Returns False if the file vanishes or times out.
    """
    deadline = time.monotonic() + max_wait_seconds
    previous_size = -1
    stable_checks = 0
    poll_interval = max(0.001, debounce_seconds / 2.0)

    while time.monotonic() < deadline:
        if not file_path.exists():
            return False

        try:
            current_size = file_path.stat().st_size
        except OSError:
            return False

        if current_size >= 0 and current_size == previous_size and not _is_file_locked(file_path):
            stable_checks += 1
            if stable_checks >= 2:
                return True
        else:
            stable_checks = 0

        previous_size = current_size
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))

    return False


class SlothEventHandler(FileSystemEventHandler):
    """Non-blocking Watchdog handler with per-file debouncing and thread-pool dispatch."""

    def __init__(
        self,
        organizer: SlothOrganizer,
        dry_run: bool = False,
        on_organized_callback: Callable[[HistoryEntry], None] | None = None,
        on_error_callback: Callable[[Path, Exception], None] | None = None,
    ) -> None:
        super().__init__()
        self.organizer = organizer
        self.config: SlothConfig = organizer.config
        self.dry_run = dry_run
        self.on_organized_callback = on_organized_callback
        self.on_error_callback = on_error_callback

        self._in_flight: set[Path] = set()
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="sloth-worker"
        )

    def _schedule_candidate(self, raw_path: str | bytes) -> None:
        path_str = raw_path.decode("utf-8", "replace") if isinstance(raw_path, bytes) else raw_path
        candidate = Path(path_str).expanduser().resolve()

        if is_ignored_file(candidate, self.config):
            return

        with self._lock:
            if candidate in self._in_flight:
                return
            self._in_flight.add(candidate)

        self._executor.submit(self._process_after_debounce, candidate)

    def _process_after_debounce(self, file_path: Path) -> None:
        try:
            ready = wait_until_file_ready(
                file_path,
                debounce_seconds=self.config.debounce_seconds,
            )
            if not ready or is_ignored_file(file_path, self.config):
                return

            entry = self.organizer.organize_file(file_path, dry_run=self.dry_run)
            if entry and self.on_organized_callback:
                self.on_organized_callback(entry)
        except Exception as exc:
            if self.on_error_callback:
                self.on_error_callback(file_path, exc)
        finally:
            with self._lock:
                self._in_flight.discard(file_path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule_candidate(event.src_path)

    def on_moved(self, event: FileMovedEvent) -> None:
        if not event.is_directory:
            self._schedule_candidate(event.dest_path)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class SlothDaemon:
    """Manages the Watchdog observer lifecycle across all configured watch directories."""

    def __init__(
        self,
        organizer: SlothOrganizer,
        dry_run: bool = False,
        on_organized_callback: Callable[[HistoryEntry], None] | None = None,
        on_error_callback: Callable[[Path, Exception], None] | None = None,
    ) -> None:
        self.organizer = organizer
        self.handler = SlothEventHandler(
            organizer=organizer,
            dry_run=dry_run,
            on_organized_callback=on_organized_callback,
            on_error_callback=on_error_callback,
        )
        self.observer = Observer()

    def start(self) -> list[Path]:
        active_dirs: list[Path] = []
        for watch_dir in self.organizer.config.watch_dirs:
            if watch_dir.exists() and watch_dir.is_dir():
                self.observer.schedule(self.handler, str(watch_dir), recursive=False)
                active_dirs.append(watch_dir)
        self.observer.start()
        return active_dirs

    def stop(self) -> None:
        self.observer.stop()
        self.observer.join(timeout=5.0)
        self.handler.shutdown()
