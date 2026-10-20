from __future__ import annotations

import json
import re
import time
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx
from pydantic import BaseModel, Field, field_validator

from sloth_tidy.config import SlothConfig
from sloth_tidy.extractors import ExtractedContext


class CategoryEnum(str, Enum):
    FINANCE = "Finance"
    ACADEMIC = "Academic"
    DEVELOPMENT = "Development"
    MEDIA = "Media"
    ADMINISTRATION = "Administration"
    MISC = "Misc"


DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")
SLUG_CLEAN_RE = re.compile(r"[^a-zA-Z0-9._-]+")


class FileClassification(BaseModel):
    """Strict output contract for local LLM file organization decisions."""

    category: CategoryEnum
    suggested_filename: str = Field(
        description="Formatted as YYYY-MM-DD-descriptive-slug.ext"
    )
    subfolder: str = Field(
        description="Relative subdirectory path inside the category folder, e.g. Invoices/2026"
    )
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(
        description="Concise one-line description of the file contents"
    )

    @field_validator("suggested_filename")
    @classmethod
    def sanitize_filename(cls, v: str) -> str:
        clean = Path(v.strip()).name
        clean = SLUG_CLEAN_RE.sub("-", clean)
        clean = re.sub(r"-{2,}", "-", clean).strip("-")
        if not clean:
            return "2026-01-01-organized-file.bin"
        return clean

    @field_validator("subfolder")
    @classmethod
    def sanitize_subfolder(cls, v: str) -> str:
        raw_parts = v.replace("\\", "/").split("/")
        safe_parts = []
        for part in raw_parts:
            cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", part.strip()).strip("_")
            if cleaned and cleaned not in (".", ".."):
                safe_parts.append(cleaned)
        return "/".join(safe_parts) if safe_parts else "General"

    @field_validator("summary")
    @classmethod
    def sanitize_summary(cls, v: str) -> str:
        single_line = " ".join(v.strip().splitlines())
        return single_line[:200] if single_line else "Organized local file."


SYSTEM_PROMPT = """You are SlothTidy, an offline file classification engine.
Analyze the provided file metadata and content snippet, and respond ONLY with a valid JSON object matching this exact schema:
{
  "category": "Finance" | "Academic" | "Development" | "Media" | "Administration" | "Misc",
  "suggested_filename": "YYYY-MM-DD-descriptive-slug.ext",
  "subfolder": "Relative/Subdirectory/Path",
  "confidence": 0.0 to 1.0,
  "summary": "One-line description of the file contents"
}

Rules:
1. Keep the exact original file extension in `suggested_filename`.
2. Start `suggested_filename` with the YYYY-MM-DD date from the document content if found, otherwise use the file's modified_iso date.
3. Use lowercase kebab-case for the descriptive slug in `suggested_filename`.
4. `subfolder` must be a short, logical relative path (1 to 2 levels deep, e.g., "Receipts", "Computer_Science/Notes", "Screenshots", "Source_Code") and MUST NOT repeat the top-level category name.
5. Output raw JSON only. No markdown fences, no commentary."""


def ensure_filename_contract(
    classification: FileClassification, context: ExtractedContext
) -> FileClassification:
    """Enforce date prefix and original extension preservation on LLM output."""
    name = classification.suggested_filename
    orig_ext = context.extension.lower()

    # Ensure original extension is preserved
    if orig_ext and not name.lower().endswith(orig_ext):
        stem = Path(name).stem
        name = f"{stem}{orig_ext}"

    # Ensure YYYY-MM-DD prefix exists
    if not DATE_PREFIX_RE.match(name):
        name = f"{context.modified_iso}-{name.lstrip('-')}"

    classification.suggested_filename = FileClassification.sanitize_filename(name)
    return classification


def heuristic_fallback_classify(context: ExtractedContext) -> FileClassification:
    """Deterministic fallback classifier if both Ollama and llama.cpp are unreachable."""
    lower_name = context.filename.lower()
    lower_text = context.content_snippet.lower()
    ext = context.extension.lower()

    stem_slug = re.sub(r"[^a-z0-9]+", "-", Path(context.filename).stem.lower()).strip("-")
    if not stem_slug:
        stem_slug = "unnamed-item"
    suggested_name = f"{context.modified_iso}-{stem_slug}{ext}"

    if any(k in lower_name or k in lower_text for k in ("invoice", "receipt", "bank", "tax", "facture", "rib", "salary")):
        return FileClassification(
            category=CategoryEnum.FINANCE,
            suggested_filename=suggested_name,
            subfolder="Statements_And_Receipts",
            confidence=0.68,
            summary=f"Financial document detected via keyword heuristics ({context.filename}).",
        )

    if any(k in lower_name or k in lower_text for k in ("syllabus", "lecture", "exam", "homework", "cours", "td", "tp", "thesis", "arxiv")):
        return FileClassification(
            category=CategoryEnum.ACADEMIC,
            suggested_filename=suggested_name,
            subfolder="Coursework",
            confidence=0.68,
            summary=f"Academic material detected via keyword heuristics ({context.filename}).",
        )

    if ext in {".py", ".rs", ".c", ".cpp", ".h", ".js", ".ts", ".json", ".toml", ".yaml", ".sh", ".sql", ".deb", ".rpm", ".appimage"}:
        return FileClassification(
            category=CategoryEnum.DEVELOPMENT,
            suggested_filename=suggested_name,
            subfolder="Code_And_Packages",
            confidence=0.72,
            summary=f"Developer source or package artifact ({ext} file).",
        )

    if ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mkv", ".mp3", ".wav", ".flac"}:
        sub = "Screenshots" if "screen" in lower_name or "capture" in lower_name else "Gallery"
        return FileClassification(
            category=CategoryEnum.MEDIA,
            suggested_filename=suggested_name,
            subfolder=sub,
            confidence=0.75,
            summary=f"Media file ({context.metadata_summary[:80]}).",
        )

    if any(k in lower_name or k in lower_text for k in ("passport", "id", "cin", "contract", "attestation", "certificate", "insurance")):
        return FileClassification(
            category=CategoryEnum.ADMINISTRATION,
            suggested_filename=suggested_name,
            subfolder="Official_Records",
            confidence=0.65,
            summary=f"Administrative document ({context.filename}).",
        )

    return FileClassification(
        category=CategoryEnum.MISC,
        suggested_filename=suggested_name,
        subfolder="Unsorted",
        confidence=0.50,
        summary=f"General file organized by fallback heuristics ({context.filename}).",
    )


class LocalLLMClient:
    """Local inference client targeting Ollama REST API with llama-cpp-python and heuristic fallbacks."""

    def __init__(self, config: SlothConfig, max_retries: int = 3) -> None:
        self.config = config
        self.max_retries = max_retries
        self._llama_instance = None

    def _build_user_prompt(self, context: ExtractedContext) -> str:
        return (
            f"Filename: {context.filename}\n"
            f"Extension: {context.extension}\n"
            f"Size (bytes): {context.size_bytes}\n"
            f"Modified Date (YYYY-MM-DD): {context.modified_iso}\n"
            f"MIME Type: {context.mime_type}\n"
            f"Extractor: {context.extractor_used}\n"
            f"Metadata: {context.metadata_summary}\n"
            f"Content Snippet:\n{context.content_snippet}\n"
        )

    def _extract_json_object(self, raw_text: str) -> str:
        """Extract the outermost JSON object even if the model wraps it in markdown fences."""
        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            return cleaned[start : end + 1]
        return cleaned

    def _call_ollama(self, context: ExtractedContext) -> Optional[FileClassification]:
        """Call local Ollama `/api/chat` with structured JSON schema enforcement and retry logic."""
        url = f"{self.config.ollama_endpoint.rstrip('/')}/api/chat"
        use_vision = (
            self.config.enable_vision_model
            and context.base64_image is not None
        )
        active_model = (
            self.config.vision_model_name if use_vision else self.config.model_name
        )

        user_message: dict = {
            "role": "user",
            "content": self._build_user_prompt(context),
        }
        if use_vision and context.base64_image:
            user_message["images"] = [context.base64_image]

        payload = {
            "model": active_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                user_message,
            ],
            "stream": False,
            "format": FileClassification.model_json_schema(),
            "options": {
                "temperature": 0.1,
                "num_predict": 256,
            },
        }

        backoff = 0.5
        for attempt in range(1, self.max_retries + 1):
            try:
                with httpx.Client(timeout=25.0) as client:
                    response = client.post(url, json=payload)
                    response.raise_for_status()
                    data = response.json()
                    raw_content = data.get("message", {}).get("content", "")
                    json_str = self._extract_json_object(raw_content)
                    parsed = FileClassification.model_validate_json(json_str)
                    return ensure_filename_contract(parsed, context)
            except (httpx.HTTPError, json.JSONDecodeError, ValueError):
                if attempt < self.max_retries:
                    time.sleep(backoff)
                    backoff *= 2.0
                else:
                    return None
        return None

    def _call_llama_cpp(self, context: ExtractedContext) -> Optional[FileClassification]:
        """Fallback inference using direct `llama-cpp-python` bindings if configured."""
        if not self.config.gguf_fallback_path or not self.config.gguf_fallback_path.exists():
            return None

        try:
            from llama_cpp import Llama

            if self._llama_instance is None:
                self._llama_instance = Llama(
                    model_path=str(self.config.gguf_fallback_path),
                    n_ctx=2048,
                    n_threads=4,
                    verbose=False,
                )

            response = self._llama_instance.create_chat_completion(
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": self._build_user_prompt(context)},
                ],
                response_format={
                    "type": "json_object",
                    "schema": FileClassification.model_json_schema(),
                },
                temperature=0.1,
                max_tokens=256,
            )
            raw_content = response["choices"][0]["message"]["content"]
            json_str = self._extract_json_object(raw_content)
            parsed = FileClassification.model_validate_json(json_str)
            return ensure_filename_contract(parsed, context)
        except Exception:
            return None

    def classify(self, context: ExtractedContext) -> FileClassification:
        """Classify a file using Ollama, falling back to llama.cpp, then deterministic rules."""
        result = self._call_ollama(context)
        if result is not None:
            return result

        result = self._call_llama_cpp(context)
        if result is not None:
            return result

        fallback = heuristic_fallback_classify(context)
        return ensure_filename_contract(fallback, context)