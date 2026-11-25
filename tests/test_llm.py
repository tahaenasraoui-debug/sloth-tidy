from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import httpx

from sloth_tidy.config import SlothConfig
from sloth_tidy.extractors import ExtractedContext
from sloth_tidy.llm import (
    CategoryEnum,
    LocalLLMClient,
    heuristic_fallback_classify,
)


def _make_context(
    filename: str = "document.txt",
    extension: str = ".txt",
    content_snippet: str = "plain text",
    base64_image: str | None = None,
) -> ExtractedContext:
    return ExtractedContext(
        filename=filename,
        extension=extension,
        size_bytes=128,
        modified_iso="2026-10-19",
        mime_type="text/plain",
        extractor_used="text",
        content_snippet=content_snippet,
        metadata_summary="dims=100x100",
        base64_image=base64_image,
    )


def test_heuristic_fallback_classify_all_categories():
    """Verify deterministic fallback classification covers every category branch."""
    finance = heuristic_fallback_classify(
        _make_context("scan_01.pdf", ".pdf", "Monthly bank statement and invoice total.")
    )
    assert finance.category == CategoryEnum.FINANCE
    assert finance.subfolder == "Statements_And_Receipts"

    academic = heuristic_fallback_classify(
        _make_context("week3.pdf", ".pdf", "Algorithm lecture notes and homework exam.")
    )
    assert academic.category == CategoryEnum.ACADEMIC
    assert academic.subfolder == "Coursework"

    dev = heuristic_fallback_classify(
        _make_context("server.py", ".py", "def main(): pass")
    )
    assert dev.category == CategoryEnum.DEVELOPMENT
    assert dev.subfolder == "Code_And_Packages"

    screenshot = heuristic_fallback_classify(
        _make_context("Screenshot_2026.png", ".png", "")
    )
    assert screenshot.category == CategoryEnum.MEDIA
    assert screenshot.subfolder == "Screenshots"

    photo = heuristic_fallback_classify(
        _make_context("vacation_photo.jpg", ".jpg", "")
    )
    assert photo.category == CategoryEnum.MEDIA
    assert photo.subfolder == "Gallery"

    admin = heuristic_fallback_classify(
        _make_context("rental_contract.pdf", ".pdf", "Signed lease agreement.")
    )
    assert admin.category == CategoryEnum.ADMINISTRATION
    assert admin.subfolder == "Official_Records"

    misc = heuristic_fallback_classify(
        _make_context("random_notes.txt", ".txt", "Groceries: apples, bread, milk.")
    )
    assert misc.category == CategoryEnum.MISC
    assert misc.subfolder == "Unsorted"


def test_extract_json_object_handles_fences_and_prose():
    """Markdown code fences and surrounding prose should be stripped before JSON parsing."""
    client = LocalLLMClient(SlothConfig())
    raw = 'Here is the result:\n```json\n{"category": "Misc", "a": {"b": 1}}\n```\nThanks!'
    assert client._extract_json_object(raw) == '{"category": "Misc", "a": {"b": 1}}'


def test_call_ollama_success_vision_and_retries(tmp_path: Path, monkeypatch):
    """Test mocked Ollama HTTP calls including vision payloads, retries, and failures."""
    cfg = SlothConfig(
        watch_dirs=[tmp_path],
        target_root=tmp_path / "out",
        enable_vision_model=True,
        vision_model_name="moondream:latest",
    )
    client = LocalLLMClient(cfg, max_retries=3)
    captured_payloads: list[dict] = []
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        payload = json.loads(request.content.decode("utf-8"))
        captured_payloads.append(payload)

        if attempts["count"] == 1:
            return httpx.Response(500, json={"error": "transient"})

        content = json.dumps(
            {
                "category": "Media",
                "suggested_filename": "architecture-diagram.txt",
                "subfolder": "Diagrams",
                "confidence": 0.91,
                "summary": "System diagram image.",
            }
        )
        return httpx.Response(200, json={"message": {"content": f"```json\n{content}\n```"}})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real_client(transport=transport))
    monkeypatch.setattr("sloth_tidy.llm.time.sleep", lambda _s: None)

    ctx = _make_context("diagram.png", ".png", "diagram", base64_image="ZmFrZV9iNjQ=")
    result = client.classify(ctx)

    assert attempts["count"] == 2
    assert captured_payloads[0]["model"] == "moondream:latest"
    assert captured_payloads[0]["messages"][1]["images"] == ["ZmFrZV9iNjQ="]
    assert result.category == CategoryEnum.MEDIA
    assert result.suggested_filename == "2026-10-19-architecture-diagram.png"


def test_call_llama_cpp_and_heuristic_fallback_chain(tmp_path: Path, monkeypatch):
    """When Ollama fails, classify falls back to llama-cpp-python and finally heuristics."""
    cfg = SlothConfig(watch_dirs=[tmp_path], target_root=tmp_path / "out")
    client = LocalLLMClient(cfg, max_retries=1)

    # Without gguf_fallback_path, _call_llama_cpp returns None
    assert client._call_llama_cpp(_make_context()) is None

    # Force Ollama HTTP calls to fail
    def failing_handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(failing_handler)
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real_client(transport=transport))

    # Configure existing GGUF file and fake llama_cpp module
    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"GGUF")
    cfg.gguf_fallback_path = gguf_path

    fake_llama_instance = MagicMock()
    fake_llama_instance.create_chat_completion.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "category": "Development",
                            "suggested_filename": "build-script",
                            "subfolder": "Scripts",
                            "confidence": 0.87,
                            "summary": "Build automation script.",
                        }
                    )
                }
            }
        ]
    }
    fake_llama_cls = MagicMock(return_value=fake_llama_instance)
    fake_module = ModuleType("llama_cpp")
    fake_module.Llama = fake_llama_cls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llama_cpp", fake_module)

    ctx = _make_context("build.sh", ".sh", "#!/bin/bash")
    res = client.classify(ctx)
    assert res.category == CategoryEnum.DEVELOPMENT
    assert res.suggested_filename == "2026-10-19-build-script.sh"
    fake_llama_cls.assert_called_once()

    # Calling again reuses cached _llama_instance
    client.classify(ctx)
    fake_llama_cls.assert_called_once()

    # If llama_cpp also fails, deterministic heuristic fallback takes over
    fake_llama_instance.create_chat_completion.side_effect = RuntimeError("inference failed")
    invoice_ctx = _make_context("march_invoice.pdf", ".pdf", "Total invoice amount due")
    fallback_res = client.classify(invoice_ctx)
    assert fallback_res.category == CategoryEnum.FINANCE
    assert fallback_res.suggested_filename == "2026-10-19-march-invoice.pdf"
