from __future__ import annotations

import tomllib
from pathlib import Path
from typing import List, Optional

import tomli_w
from pydantic import BaseModel, Field, field_validator

CONFIG_DIR = Path.home() / ".config" / "sloth-tidy"
CONFIG_FILE = CONFIG_DIR / "config.toml"
HISTORY_FILE = CONFIG_DIR / "history.jsonl"


class SlothConfig(BaseModel):
    """Configuration schema for SlothTidy daemon and CLI."""

    watch_dirs: List[Path] = Field(
        default_factory=lambda: [
            Path.home() / "Downloads",
            Path.home() / "Desktop",
        ]
    )
    target_root: Path = Field(
        default_factory=lambda: Path.home() / "Documents" / "Organized"
    )
    ollama_endpoint: str = "http://localhost:11434"
    model_name: str = "qwen2.5:1.5b"
    vision_model_name: str = "moondream:latest"
    gguf_fallback_path: Optional[Path] = None
    confidence_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    debounce_seconds: float = Field(default=2.5, ge=0.5, le=60.0)
    max_text_bytes: int = Field(default=2048, ge=256, le=32768)
    enable_ocr: bool = True
    enable_vision_model: bool = False
    ignored_extensions: List[str] = Field(
        default_factory=lambda: [
            ".crdownload",
            ".part",
            ".tmp",
            ".download",
            ".partial",
            ".swp",
            ".lock",
            ".aria2",
            ".ut!",
        ]
    )
    ignored_prefixes: List[str] = Field(
        default_factory=lambda: [".", "~$", "__"]
    )

    @field_validator("watch_dirs", mode="before")
    @classmethod
    def expand_watch_dirs(cls, v: list[str | Path]) -> list[Path]:
        return [Path(p).expanduser().resolve() for p in v]

    @field_validator("target_root", mode="before")
    @classmethod
    def expand_target_root(cls, v: str | Path) -> Path:
        return Path(v).expanduser().resolve()

    @field_validator("gguf_fallback_path", mode="before")
    @classmethod
    def expand_gguf_path(cls, v: Optional[str | Path]) -> Optional[Path]:
        if not v:
            return None
        return Path(v).expanduser().resolve()

    def to_serializable_dict(self) -> dict:
        """Convert Path objects to strings for TOML serialization."""
        data = {
            "watch_dirs": [str(p) for p in self.watch_dirs],
            "target_root": str(self.target_root),
            "ollama_endpoint": self.ollama_endpoint,
            "model_name": self.model_name,
            "vision_model_name": self.vision_model_name,
            "confidence_threshold": self.confidence_threshold,
            "debounce_seconds": self.debounce_seconds,
            "max_text_bytes": self.max_text_bytes,
            "enable_ocr": self.enable_ocr,
            "enable_vision_model": self.enable_vision_model,
            "ignored_extensions": self.ignored_extensions,
            "ignored_prefixes": self.ignored_prefixes,
        }
        if self.gguf_fallback_path is not None:
            data["gguf_fallback_path"] = str(self.gguf_fallback_path)
        return data


def ensure_config_dir() -> None:
    """Create configuration directory and history file if absent."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not HISTORY_FILE.exists():
        HISTORY_FILE.touch()


def load_config(config_path: Path = CONFIG_FILE) -> SlothConfig:
    """Load configuration from TOML file, creating defaults if missing."""
    ensure_config_dir()
    if not config_path.exists():
        default_cfg = SlothConfig()
        save_config(default_cfg, config_path)
        return default_cfg

    with config_path.open("rb") as f:
        raw_data = tomllib.load(f)
    return SlothConfig.model_validate(raw_data)


def save_config(config: SlothConfig, config_path: Path = CONFIG_FILE) -> None:
    """Persist configuration to TOML file."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("wb") as f:
        tomli_w.dump(config.to_serializable_dict(), f)