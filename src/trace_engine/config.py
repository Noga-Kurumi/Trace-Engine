"""Configuración pública y validada de TRACE."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class TraceConfig:
    """Opciones que TRACE acepta de sus aplicaciones consumidoras."""

    db_path: Optional[str] = None
    whisper_model_path: Optional[str] = None
    whisper_stream_exe: Optional[str] = None
    whisper_cli_exe: Optional[str] = None
    whisper_threads: int = 4
    whisper_language: str = "es"
    timeline_enabled: bool = True
    trace_clipboard_enabled: bool = True
    trace_screen_enabled: bool = True
    meeting_detection_enabled: bool = True
    timeline_retention_hours: float = 72
    ocr_interval_seconds: float = 12
    ocr_max_chars: int = 4000
    meeting_source_apps: list[str] = field(default_factory=lambda: ["discord"])
    trace_excluded_apps: list[str] = field(default_factory=list)
    clipboard_max_chars: int = 10000
    meeting_metadata_provider: Any = None
    meeting_detector: Any = None
    meeting_capture_factory: Any = None
    meeting_vad_factory: Any = None
    meeting_speaker_factory: Any = None
    meeting_poll_seconds: float = 1.0
    meeting_segment_seconds: float = 12.0
    meeting_silence_seconds: float = 0.6
    meeting_buffer_seconds: float = 30.0
    meeting_end_grace_seconds: float = 5.0
    meeting_asr_timeout_seconds: float = 120.0

    @classmethod
    def from_mapping(cls, values: Optional[Dict[str, Any]] = None) -> "TraceConfig":
        values = dict(values or {})
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: value for key, value in values.items() if key in allowed})

    def as_dict(self) -> Dict[str, Any]:
        return {key: getattr(self, key) for key in self.__dataclass_fields__}
