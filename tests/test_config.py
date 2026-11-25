from __future__ import annotations

import warnings
from pathlib import Path

from sloth_tidy import config as config_mod
from sloth_tidy.config import SlothConfig, load_config, save_config


def test_sloth_config_emits_no_pydantic_warning(tmp_path: Path):
    """Instantiating or validating SlothConfig must not emit Pydantic protected_namespaces warnings."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = SlothConfig(
            watch_dirs=[tmp_path / "Downloads"],
            target_root=tmp_path / "Organized",
            model_name="qwen2.5:1.5b",
            vision_model_name="moondream:latest",
        )
        SlothConfig.model_validate({"model_name": "llama3.2:1b"})

    model_warnings = [
        w
        for w in caught
        if "protected namespace" in str(w.message).lower()
        or "model_" in str(w.message)
    ]
    assert model_warnings == []
    assert cfg.model_name == "qwen2.5:1.5b"


def test_sloth_config_path_expansion_and_toml_roundtrip(tmp_path: Path, monkeypatch):
    """Custom config paths load/save TOML roundtrip without touching default user config dir."""
    ensure_calls: list[bool] = []
    monkeypatch.setattr(config_mod, "ensure_config_dir", lambda: ensure_calls.append(True))

    custom_cfg_path = tmp_path / "custom" / "config.toml"
    gguf_file = tmp_path / "models" / "tiny.gguf"

    cfg = SlothConfig(
        watch_dirs=[str(tmp_path / "inbox")],
        target_root=str(tmp_path / "sorted"),
        gguf_fallback_path=str(gguf_file),
        confidence_threshold=0.7,
        debounce_seconds=1.5,
        enable_ocr=False,
    )
    save_config(cfg, custom_cfg_path)
    loaded = load_config(custom_cfg_path)

    assert ensure_calls == []
    assert loaded.watch_dirs == [(tmp_path / "inbox").resolve()]
    assert loaded.target_root == (tmp_path / "sorted").resolve()
    assert loaded.gguf_fallback_path == gguf_file.resolve()
    assert loaded.confidence_threshold == 0.7
    assert loaded.enable_ocr is False


def test_load_config_default_creates_defaults_and_calls_ensure(tmp_path: Path, monkeypatch):
    """Loading from default CONFIG_FILE triggers ensure_config_dir and writes defaults."""
    ensure_calls: list[bool] = []
    default_cfg_path = tmp_path / "default" / "config.toml"

    monkeypatch.setattr(config_mod, "CONFIG_FILE", default_cfg_path)
    monkeypatch.setattr(config_mod, "ensure_config_dir", lambda: ensure_calls.append(True))

    loaded = load_config(default_cfg_path)
    assert ensure_calls == [True]
    assert default_cfg_path.exists()
    assert loaded.model_name == "qwen2.5:1.5b"
