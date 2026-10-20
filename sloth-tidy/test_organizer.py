from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sloth_tidy.config import SlothConfig
from sloth_tidy.extractors import extract_file_context
from sloth_tidy.llm import (
    CategoryEnum,
    FileClassification,
    LocalLLMClient,
    ensure_filename_contract,
)
from sloth_tidy.organizer import (
    SlothOrganizer,
    read_history,
    resolve_collision,
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
    config, watch_dir, target_dir, history_file = temp_env
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
    config, watch_dir, target_dir, history_file = temp_env

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
