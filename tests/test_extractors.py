from __future__ import annotations

import base64
import io
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

from PIL import Image
from pypdf import PdfWriter

from sloth_tidy.config import SlothConfig
from sloth_tidy.extractors import (
    extract_binary_metadata,
    extract_file_context,
    extract_image,
    extract_pdf,
    extract_text_file,
)


def test_extract_text_file_small_large_and_empty(tmp_path: Path):
    """Verify text extraction for small, large (head+tail), and 0-byte text files."""
    empty = tmp_path / "empty.txt"
    empty.touch()
    snippet_empty, meta_empty = extract_text_file(empty, max_bytes=256)
    assert snippet_empty == ""
    assert "encoding=utf-8" in meta_empty

    small = tmp_path / "small.py"
    small.write_text("print('hello')\n\n\nprint('world')\n")
    snippet_small, _ = extract_text_file(small, max_bytes=256)
    assert "[HEAD]" not in snippet_small
    assert "print('hello')\nprint('world')" == snippet_small

    large = tmp_path / "large.log"
    large.write_text(("HEADLINE\n" * 60) + ("TAILLINE\n" * 80))
    snippet_large, _ = extract_text_file(large, max_bytes=256)
    assert "[HEAD]" in snippet_large
    assert "[TAIL]" in snippet_large


def test_extract_pdf_metadata_blank_embedded_text_and_corrupted(tmp_path: Path, monkeypatch):
    """Verify PDF extraction for blank/metadata PDFs, text PDFs, and corrupted PDFs."""
    blank_pdf = tmp_path / "scanned.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Title": "Tax Return 2026", "/Author": "Jane Doe"})
    with blank_pdf.open("wb") as f:
        writer.write(f)

    snippet_blank, meta_blank = extract_pdf(blank_pdf, max_bytes=512)
    assert snippet_blank == "[Scanned or image-only PDF with no embedded text layer]"
    assert "total_pages=1" in meta_blank
    assert "Title=Tax Return 2026" in meta_blank
    assert "Author=Jane Doe" in meta_blank

    # Mock PdfReader to simulate multi-page embedded text extraction
    fake_page_1 = MagicMock()
    fake_page_1.extract_text.return_value = "Invoice #42\nAmount: $120.00"
    fake_page_2 = MagicMock()
    fake_page_2.extract_text.return_value = "Page 2 terms"
    fake_reader = MagicMock()
    fake_reader.metadata = {"/Title": "Invoice 42"}
    fake_reader.pages = [fake_page_1, fake_page_2]
    monkeypatch.setattr("sloth_tidy.extractors.PdfReader", lambda *_a, **_kw: fake_reader)

    snippet_text, meta_text = extract_pdf(blank_pdf, max_bytes=512)
    assert "--- Page 1 ---" in snippet_text
    assert "Invoice #42" in snippet_text
    assert "--- Page 2 ---" in snippet_text
    assert "total_pages=2" in meta_text

    # Corrupted PDF error path
    monkeypatch.undo()
    broken_pdf = tmp_path / "broken.pdf"
    broken_pdf.write_bytes(b"not a valid pdf")
    err_snippet, err_meta = extract_pdf(broken_pdf)
    assert err_snippet.startswith("[PDF extraction error:")
    assert err_meta == "corrupted_or_encrypted_pdf=True"


def test_extract_image_metadata_vision_ocr_and_corrupted(tmp_path: Path, monkeypatch):
    """Verify image dimensions, RGBA->RGB vision thumbnail encoding, OCR, and error handling."""
    img_path = tmp_path / "screenshot.png"
    Image.new("RGBA", (640, 480), color=(255, 0, 0, 128)).save(img_path, format="PNG")

    # Vision thumbnail generation without OCR
    snippet, meta, b64 = extract_image(img_path, enable_ocr=False, enable_vision=True)
    assert "[No OCR text detected" in snippet
    assert "dimensions=640x480" in meta
    assert "format=PNG" in meta
    assert "mode=RGBA" in meta
    assert b64 is not None
    decoded = base64.b64decode(b64)
    with Image.open(io.BytesIO(decoded)) as thumb:
        assert thumb.format == "JPEG"

    # Mock pytesseract OCR success
    fake_tesseract = ModuleType("pytesseract")
    fake_tesseract.image_to_string = lambda *_a, **_kw: "Quarterly Revenue Report\nTotal: $5,000"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pytesseract", fake_tesseract)
    ocr_snippet, _, _ = extract_image(img_path, enable_ocr=True, enable_vision=False)
    assert "Quarterly Revenue Report" in ocr_snippet

    # OCR failure falls back cleanly
    fake_tesseract.image_to_string = MagicMock(side_effect=RuntimeError("tesseract missing"))  # type: ignore[attr-defined]
    fallback_snippet, _, _ = extract_image(img_path, enable_ocr=True, enable_vision=False)
    assert "[No OCR text detected" in fallback_snippet

    # Invalid image bytes
    bad_img = tmp_path / "corrupt.png"
    bad_img.write_bytes(b"broken image bytes")
    bad_snippet, bad_meta, bad_b64 = extract_image(bad_img, enable_ocr=False)
    assert bad_snippet.startswith("[Image open error:")
    assert bad_meta == "invalid_image=True"
    assert bad_b64 is None


def test_extract_file_context_routing_and_binary_fallback(tmp_path: Path):
    """Verify extract_file_context dispatches to the expected extractor by file type."""
    cfg = SlothConfig(watch_dirs=[tmp_path], target_root=tmp_path / "out", enable_ocr=False)

    # Known binary extension
    archive = tmp_path / "release.zip"
    archive.write_bytes(b"PK\x03\x04\x14\x00\x00\x00rest")
    ctx_bin = extract_file_context(archive, cfg)
    assert ctx_bin.extractor_used == "binary"
    assert "magic_header_hex=504B030414000000" in ctx_bin.metadata_summary

    # Unknown extension with UTF-8 content triggers text_heuristic
    note = tmp_path / "README"
    note.write_text("Project readme notes without extension.")
    ctx_txt_heur = extract_file_context(note, cfg)
    assert ctx_txt_heur.extractor_used == "text_heuristic"
    assert ctx_txt_heur.extension == ".bin"
    assert "Project readme notes" in ctx_txt_heur.content_snippet

    # Unknown extension with NUL bytes triggers binary_fallback
    blob = tmp_path / "mystery.custombin"
    blob.write_bytes(b"\x00\x01\x02\xff\xfe\xfd")
    ctx_bin_fb = extract_file_context(blob, cfg)
    assert ctx_bin_fb.extractor_used == "binary_fallback"
    assert "magic_header_hex=000102FFFEFD" in ctx_bin_fb.metadata_summary

    # Unreadable path in extract_binary_metadata
    _, unreadable_meta = extract_binary_metadata(tmp_path / "does-not-exist.bin")
    assert "magic_header_hex=UNREADABLE" in unreadable_meta
