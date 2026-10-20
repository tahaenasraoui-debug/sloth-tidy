from __future__ import annotations

import base64
import io
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PIL import ExifTags, Image
from pydantic import BaseModel
from pypdf import PdfReader

from sloth_tidy.config import SlothConfig

TEXT_EXTENSIONS = {
    ".txt", ".md", ".rst", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".py", ".rs", ".c", ".cpp", ".h",
    ".hpp", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".sh", ".bash",
    ".zsh", ".fish", ".sql", ".tex", ".bib", ".org", ".xml", ".env",
}

IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".gif",
}

ARCHIVE_AND_BINARY_EXTENSIONS = {
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".iso",
    ".dmg", ".deb", ".rpm", ".appimage", ".exe", ".msi", ".apk", ".bin",
    ".so", ".dll", ".dylib", ".mp3", ".wav", ".flac", ".mp4", ".mkv",
    ".mov", ".avi", ".webm", ".ogg",
}


class ExtractedContext(BaseModel):
    """Normalized metadata and content snippet extracted from a local file."""

    filename: str
    extension: str
    size_bytes: int
    modified_iso: str
    mime_type: str
    extractor_used: str
    content_snippet: str
    metadata_summary: str
    base64_image: Optional[str] = None


def _clean_whitespace(text: str, max_chars: int) -> str:
    """Collapse excessive whitespace and bound string length."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    collapsed = "\n".join(lines)
    if len(collapsed) > max_chars:
        return collapsed[:max_chars] + "\n...[truncated]..."
    return collapsed


def extract_pdf(file_path: Path, max_bytes: int = 2048) -> tuple[str, str]:
    """Extract text from the first 2 pages and document metadata of a PDF."""
    try:
        reader = PdfReader(str(file_path), strict=False)
        meta = reader.metadata or {}
        meta_parts = [f"total_pages={len(reader.pages)}"]
        for key in ("/Title", "/Author", "/Subject", "/Creator"):
            val = meta.get(key)
            if val:
                meta_parts.append(f"{key.lstrip('/')}={val}")

        pages_to_read = min(2, len(reader.pages))
        extracted_pages = []
        for idx in range(pages_to_read):
            page_text = reader.pages[idx].extract_text() or ""
            if page_text.strip():
                extracted_pages.append(f"--- Page {idx + 1} ---\n{page_text}")

        combined_text = "\n".join(extracted_pages)
        snippet = _clean_whitespace(combined_text, max_bytes)
        if not snippet:
            snippet = "[Scanned or image-only PDF with no embedded text layer]"
        return snippet, "; ".join(meta_parts)
    except Exception as exc:
        return f"[PDF extraction error: {exc}]", "corrupted_or_encrypted_pdf=True"


def extract_text_file(file_path: Path, max_bytes: int = 2048) -> tuple[str, str]:
    """Read head (first max_bytes) and tail (last 512 bytes) of text or source code."""
    try:
        file_size = file_path.stat().st_size
        tail_size = min(512, max(0, file_size - max_bytes))

        with file_path.open("rb") as f:
            head_bytes = f.read(max_bytes)
            tail_bytes = b""
            if tail_size > 0 and file_size > max_bytes:
                f.seek(max(max_bytes, file_size - tail_size))
                tail_bytes = f.read(tail_size)

        head_text = head_bytes.decode("utf-8", errors="replace")
        tail_text = tail_bytes.decode("utf-8", errors="replace")

        if tail_text.strip():
            combined = f"[HEAD]\n{head_text}\n...\n[TAIL]\n{tail_text}"
        else:
            combined = head_text

        line_count_est = head_text.count("\n") + 1
        meta = f"encoding=utf-8; sampled_lines>={line_count_est}"
        return _clean_whitespace(combined, max_bytes + 512), meta
    except Exception as exc:
        return f"[Text extraction error: {exc}]", "unreadable_text=True"


def extract_image(
    file_path: Path,
    enable_ocr: bool = True,
    enable_vision: bool = False,
    max_bytes: int = 2048,
) -> tuple[str, str, Optional[str]]:
    """Extract image dimensions, EXIF tags, optional Tesseract OCR, and optional base64 thumbnail."""
    meta_parts: list[str] = []
    ocr_text = ""
    b64_thumb: Optional[str] = None

    try:
        with Image.open(file_path) as img:
            width, height = img.size
            meta_parts.append(f"dimensions={width}x{height}")
            meta_parts.append(f"format={img.format}")
            meta_parts.append(f"mode={img.mode}")

            # Extract readable EXIF tags if available
            exif_data = img.getexif()
            if exif_data:
                interesting_tags = {"Make", "Model", "DateTime", "Software", "ImageDescription"}
                for tag_id, value in exif_data.items():
                    tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
                    if tag_name in interesting_tags and value:
                        meta_parts.append(f"exif_{tag_name}={str(value).strip()[:60]}")

            if enable_ocr:
                try:
                    import pytesseract

                    raw_ocr = pytesseract.image_to_string(img, timeout=5)
                    ocr_text = _clean_whitespace(raw_ocr, max_bytes)
                except Exception:
                    ocr_text = ""

            if enable_vision:
                thumb = img.copy()
                thumb.thumbnail((512, 512))
                if thumb.mode not in ("RGB", "L"):
                    thumb = thumb.convert("RGB")
                buf = io.BytesIO()
                thumb.save(buf, format="JPEG", quality=80)
                b64_thumb = base64.b64encode(buf.getvalue()).decode("ascii")

    except Exception as exc:
        return f"[Image open error: {exc}]", "invalid_image=True", None

    if not ocr_text:
        ocr_text = (
            "[No OCR text detected; classify using filename, resolution, and EXIF metadata]"
        )

    return ocr_text, "; ".join(meta_parts), b64_thumb


def extract_binary_metadata(file_path: Path) -> tuple[str, str]:
    """Inspect magic bytes and file attributes without loading binary buffers into memory."""
    magic_hex = ""
    try:
        with file_path.open("rb") as f:
            header = f.read(8)
            magic_hex = header.hex().upper()
    except Exception:
        magic_hex = "UNREADABLE"

    snippet = (
        f"Binary/archive file '{file_path.name}'. "
        "Content buffer intentionally not parsed. Use filename tokens and extension."
    )
    meta = f"magic_header_hex={magic_hex}; binary_fallback=True"
    return snippet, meta


def _looks_like_utf8_text(file_path: Path, sample_size: int = 1024) -> bool:
    """Heuristic check if an unknown extension is plain UTF-8 text."""
    try:
        with file_path.open("rb") as f:
            sample = f.read(sample_size)
        if b"\x00" in sample:
            return False
        sample.decode("utf-8")
        return True
    except Exception:
        return False


def extract_file_context(file_path: Path, config: SlothConfig) -> ExtractedContext:
    """Route a file through the appropriate content extraction pipeline."""
    stat = file_path.stat()
    ext = file_path.suffix.lower()
    modified_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    modified_iso = modified_dt.strftime("%Y-%m-%d")

    guessed_mime, _ = mimetypes.guess_type(file_path.name)
    mime_type = guessed_mime or "application/octet-stream"

    base64_img: Optional[str] = None

    if ext == ".pdf":
        extractor_used = "pdf"
        snippet, meta = extract_pdf(file_path, max_bytes=config.max_text_bytes)
    elif ext in IMAGE_EXTENSIONS:
        extractor_used = "image"
        snippet, meta, base64_img = extract_image(
            file_path,
            enable_ocr=config.enable_ocr,
            enable_vision=config.enable_vision_model,
            max_bytes=config.max_text_bytes,
        )
    elif ext in TEXT_EXTENSIONS:
        extractor_used = "text"
        snippet, meta = extract_text_file(file_path, max_bytes=config.max_text_bytes)
    elif ext in ARCHIVE_AND_BINARY_EXTENSIONS:
        extractor_used = "binary"
        snippet, meta = extract_binary_metadata(file_path)
    elif _looks_like_utf8_text(file_path):
        extractor_used = "text_heuristic"
        snippet, meta = extract_text_file(file_path, max_bytes=config.max_text_bytes)
    else:
        extractor_used = "binary_fallback"
        snippet, meta = extract_binary_metadata(file_path)

    return ExtractedContext(
        filename=file_path.name,
        extension=ext or ".bin",
        size_bytes=stat.st_size,
        modified_iso=modified_iso,
        mime_type=mime_type,
        extractor_used=extractor_used,
        content_snippet=snippet,
        metadata_summary=meta,
        base64_image=base64_img,
    )