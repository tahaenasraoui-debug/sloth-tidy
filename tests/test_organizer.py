from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from sloth_tidy import cli
from sloth_tidy import organizer as organizer_mod
from sloth_tidy.config import SlothConfig
from sloth_tidy.extractors import extract_file_context
from sloth_tidy.llm import (
    CategoryEnum,
    FileClassification,
    LocalLLMClient,
    ensure_filename_contract,
)
from sloth_tidy.organizer import (
    HistoryEntry,
    SlothOrganizer,
    append_history,
    read_history,
    resolve_collision,
    write_history,
)


@pytest.fixture
def temp_env(tmp_path: Path):
    """Create isolated watch, target, and history paths for testing."""
    watch_dir = tmp_path / "Downloads"
    target_dir = tmp_path / "Organized"
    history_file = tmp_path / "history.jsonl"
    watch_dir.mkdir()
    target_dir.mkdir()

    config = SlothConfig(
        watch_dirs=[watch_dir],
        target_root=target_dir,
        confidence_threshold=0.55,
        enable_ocr=False,
        enable_vision_model=False,
    )
    return config, watch_dir, target_dir, history_file


def test_pydantic_schema_validation_and_sanitization():
    """Verify Pydantic schema enforces safe filenames, relative subfolders, and confidence bounds."""
    raw_json = """
    {
      "category": "Finance",
      "suggested_filename": "2026-10-19-october electric bill!!.pdf",
      "subfolder": "../../Invoices/2026/..//Electric",
      "confidence": 0.94,
      "summary": "Monthly electricity invoice\\nwith account total."
    }
    """
    parsed = FileClassification.model_validate_json(raw_json)
    assert parsed.category == CategoryEnum.FINANCE
    assert parsed.suggested_filename == "2026-10-19-october-electric-bill-.pdf"
    # Path traversal segments (`..`) must be stripped
    assert ".." not in parsed.subfolder
    assert parsed.subfolder == "Invoices/2026/Electric"
    assert "\n" not in parsed.summary


def test_collision_resolution_standard_and_compound(tmp_path: Path):
    """Ensure existing files are never overwritten and compound extensions stay intact."""
    base_pdf = tmp_path / "2026-10-19-invoice.pdf"
    base_pdf.write_text("first")

    first_collision = resolve_collision(base_pdf)
    assert first_collision.name == "2026-10-19-invoice-1.pdf"
    first_collision.write_text("second")

    second_collision = resolve_collision(base_pdf)
    assert second_collision.name == "2026-10-19-invoice-2.pdf"

    # Test compound extension .tar.gz
    archive = tmp_path / "2026-10-19-backup.tar.gz"
    archive.write_bytes(b"archive")
    archive_collision = resolve_collision(archive)
    assert archive_collision.name == "2026-10-19-backup-1.tar.gz"


def test_dry_run_does_not_move_or_log_history(temp_env):
    """Verify --dry-run leaves source file untouched and does not mutate history.jsonl."""
    config, watch_dir, _target_dir, history_file = temp_env
    sample_file = watch_dir / "lecture_notes.md"
    sample_file.write_text("# Linear Algebra\nEigenvalues and eigenvectors summary.")

    mock_llm = MagicMock(spec=LocalLLMClient)
    mock_llm.classify.return_value = FileClassification(
        category=CategoryEnum.ACADEMIC,
        suggested_filename="2026-10-19-linear-algebra-notes.md",
        subfolder="Mathematics",
        confidence=0.92,
        summary="Linear algebra lecture notes on eigenvalues.",
    )

    organizer = SlothOrganizer(config, llm_client=mock_llm, history_path=history_file)
    entry = organizer.organize_file(sample_file, dry_run=True)

    assert entry is not None
    assert entry.dry_run is True
    assert sample_file.exists()
    assert not Path(entry.destination_path).exists()
    assert read_history(history_file) == []


def test_atomic_move_collision_and_undo_lifecycle(temp_env):
    """Test full live organization, collision numbering on duplicate drops, and undo rollback."""
    config, watch_dir, _target_dir, history_file = temp_env

    file_a = watch_dir / "receipt_1.txt"
    file_a.write_text("Total paid: 450 MAD")

    file_b = watch_dir / "receipt_2.txt"
    file_b.write_text("Total paid: 450 MAD duplicate")

    mock_llm = MagicMock(spec=LocalLLMClient)
    mock_llm.classify.return_value = FileClassification(
        category=CategoryEnum.FINANCE,
        suggested_filename="2026-10-19-grocery-receipt.txt",
        subfolder="Receipts",
        confidence=0.88,
        summary="Grocery payment receipt.",
    )

    organizer = SlothOrganizer(config, llm_client=mock_llm, history_path=history_file)

    entry_a = organizer.organize_file(file_a, dry_run=False)
    entry_b = organizer.organize_file(file_b, dry_run=False)

    assert entry_a is not None and entry_b is not None
    dest_a = Path(entry_a.destination_path)
    dest_b = Path(entry_b.destination_path)

    assert dest_a.name == "2026-10-19-grocery-receipt.txt"
    assert dest_b.name == "2026-10-19-grocery-receipt-1.txt"
    assert dest_a.exists() and dest_b.exists()
    assert not file_a.exists() and not file_b.exists()

    # Undo the most recent move (file_b)
    restored = organizer.undo(count=1)
    assert len(restored) == 1
    assert file_b.exists()
    assert not dest_b.exists()
    assert dest_a.exists()


def test_filename_contract_enforces_date_and_extension(temp_env):
    """Verify ensure_filename_contract prefixes YYYY-MM-DD and retains original extension."""
    config, watch_dir, _, _ = temp_env
    script_file = watch_dir / "deploy.sh"
    script_file.write_text("#!/usr/bin/env bash\necho 'Deploying service'")

    context = extract_file_context(script_file, config)
    raw_classification = FileClassification(
        category=CategoryEnum.DEVELOPMENT,
        suggested_filename="deployment-script.txt",  # Wrong extension & missing date prefix
        subfolder="Scripts",
        confidence=0.85,
        summary="Bash deployment script.",
    )

    fixed = ensure_filename_contract(raw_classification, context)
    assert fixed.suggested_filename.startswith(context.modified_iso)
    assert fixed.suggested_filename.endswith(".sh")


def test_custom_history_path_skips_ensure_config_dir(temp_env, tmp_path: Path, monkeypatch):
    """Ensure ensure_config_dir is only invoked when using the default HISTORY_FILE."""
    config, watch_dir, _, _ = temp_env
    custom_history = tmp_path / "nested" / "custom-history.jsonl"

    ensure_calls: list[bool] = []
    monkeypatch.setattr(organizer_mod, "ensure_config_dir", lambda: ensure_calls.append(True))

    sample = watch_dir / "note.txt"
    sample.write_text("hello world")

    mock_llm = MagicMock(spec=LocalLLMClient)
    mock_llm.classify.return_value = FileClassification(
        category=CategoryEnum.MISC,
        suggested_filename="2026-10-19-note.txt",
        subfolder="Notes",
        confidence=0.90,
        summary="Simple note.",
    )

    organizer = SlothOrganizer(config, llm_client=mock_llm, history_path=custom_history)
    entry = organizer.organize_file(sample, dry_run=False)
    assert entry is not None
    assert ensure_calls == []
    assert custom_history.exists()

    # Undo also uses write_history with custom_history and must not call ensure_config_dir
    restored = organizer.undo(count=1)
    assert len(restored) == 1
    assert ensure_calls == []

    # Direct append_history / write_history with default HISTORY_FILE does call ensure_config_dir
    default_history = tmp_path / "default-sim" / "history.jsonl"
    monkeypatch.setattr(organizer_mod, "HISTORY_FILE", default_history)
    dummy_entry = HistoryEntry(
        original_path=str(sample),
        destination_path=entry.destination_path,
        category="Misc",
        subfolder="Notes",
        confidence=0.9,
        summary="Simple note.",
        size_bytes=11,
    )
    append_history(dummy_entry, default_history)
    write_history([dummy_entry], default_history)
    assert len(ensure_calls) == 2


def test_dry_run_collision_tracking_assigns_distinct_destinations(temp_env):
    """Simulate duplicate targets in dry-run mode without touching disk."""
    config, watch_dir, target_dir, history_file = temp_env

    files = []
    for idx in range(3):
        f = watch_dir / f"dup_{idx}.pdf"
        f.write_text(f"invoice {idx}")
        files.append(f)

    mock_llm = MagicMock(spec=LocalLLMClient)
    mock_llm.classify.return_value = FileClassification(
        category=CategoryEnum.FINANCE,
        suggested_filename="2026-10-19-electric-bill.pdf",
        subfolder="Bills",
        confidence=0.91,
        summary="Utility bill.",
    )

    organizer = SlothOrganizer(config, llm_client=mock_llm, history_path=history_file)
    entries = [organizer.organize_file(f, dry_run=True) for f in files]

    assert all(e is not None for e in entries)
    resolved_names = [Path(e.destination_path).name for e in entries if e is not None]
    assert resolved_names == [
        "2026-10-19-electric-bill.pdf",
        "2026-10-19-electric-bill-1.pdf",
        "2026-10-19-electric-bill-2.pdf",
    ]
    assert all(f.exists() for f in files)
    assert not (target_dir / "Finance").exists()
    assert read_history(history_file) == []

    # Resetting simulated state and creating an actual on-disk file shifts dry-run to -1
    organizer.reset_simulated_destinations()
    existing_dest = target_dir / "Finance" / "Bills" / "2026-10-19-electric-bill.pdf"
    existing_dest.parent.mkdir(parents=True, exist_ok=True)
    existing_dest.write_text("already organized")

    next_entry = organizer.organize_file(files[0], dry_run=True)
    assert next_entry is not None
    assert Path(next_entry.destination_path).name == "2026-10-19-electric-bill-1.pdf"


def test_low_confidence_routes_to_review_folder(temp_env):
    """Files below confidence_threshold are routed to Misc/Low_Confidence_Review."""
    config, watch_dir, target_dir, history_file = temp_env
    mystery = watch_dir / "mystery.txt"
    mystery.write_text("ambiguous content")

    mock_llm = MagicMock(spec=LocalLLMClient)
    mock_llm.classify.return_value = FileClassification(
        category=CategoryEnum.FINANCE,
        suggested_filename="2026-10-19-mystery.txt",
        subfolder="Invoices",
        confidence=0.30,
        summary="Uncertain item.",
    )

    organizer = SlothOrganizer(config, llm_client=mock_llm, history_path=history_file)
    entry = organizer.organize_file(mystery, dry_run=True)
    assert entry is not None
    assert entry.category == "Misc"
    assert entry.subfolder == "Low_Confidence_Review"
    assert Path(entry.destination_path) == (
        target_dir / "Misc" / "Low_Confidence_Review" / "2026-10-19-mystery.txt"
    )


def test_cli_scan_dry_run_collision_and_clean_daemon_banner(temp_env, monkeypatch):
    """Verify CLI scan --dry-run displays distinct destinations and daemon banner is clean."""
    config, watch_dir, _target_dir, _ = temp_env
    (watch_dir / "a_receipt.txt").write_text("Receipt A")
    (watch_dir / "b_receipt.txt").write_text("Receipt B")

    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(
        LocalLLMClient,
        "classify",
        lambda self, ctx: FileClassification(
            category=CategoryEnum.FINANCE,
            suggested_filename="2026-10-19-receipt.txt",
            subfolder="Receipts",
            confidence=0.90,
            summary="Receipt.",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(cli.app, ["scan", str(watch_dir), "--dry-run"])
    assert result.exit_code == 0
    assert "DRY-RUN" in result.stdout
    assert "2026-10-19-receipt.txt" in result.stdout
    assert "2026-10-19-receipt-1.txt" in result.stdout
    assert (watch_dir / "a_receipt.txt").exists()
    assert (watch_dir / "b_receipt.txt").exists()

    # Check run banner cleanly stops on KeyboardInterrupt without hardcoded personal text
    class DummyDaemon:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return [watch_dir]

        def stop(self):
            pass

    monkeypatch.setattr(cli, "SlothDaemon", DummyDaemon)
    monkeypatch.setattr(cli.time, "sleep", MagicMock(side_effect=KeyboardInterrupt))

    run_result = runner.invoke(cli.app, ["run", "--dry-run"])
    assert run_result.exit_code == 0
    assert "SlothTidy Daemon Active" in run_result.stdout
    assert "Abass" not in run_result.stdout
    assert "Bobbys" not in run_result.stdout
