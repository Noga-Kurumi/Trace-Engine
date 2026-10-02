"""API pública y multiplataforma de TRACE.

El núcleo de TRACE no depende de APIs del sistema operativo. Los recolectores
disponibles se activan de forma opcional según la plataforma y dependencias
instaladas; la persistencia y recuperación funcionan en cualquier sistema.
"""

import logging
import platform
from typing import Any, Dict, Optional

from trace_engine.storage.timeline_db import TimelineDB
from trace_engine.config import TraceConfig

logger = logging.getLogger(__name__)


class TraceEngine:
    """Motor de contexto de bajo consumo con ciclo de vida explícito.

    En Windows activa los recolectores existentes cuando están disponibles.
    En otros sistemas sigue siendo útil como timeline y backend de búsqueda,
    y permite que el consumidor inyecte eventos desde sus propios recolectores.
    """

    def __init__(self, config: Optional[Dict[str, Any] | TraceConfig] = None,
                 db: Optional[TimelineDB] = None, transcriber=None):
        self.config = (config if isinstance(config, TraceConfig)
                       else TraceConfig.from_mapping(config))
        values = self.config.as_dict()
        self.db = db or TimelineDB(self.config.db_path)
        self.transcription_service = None
        self.streaming_transcriber = None
        if self.config.whisper_stream_exe and self.config.whisper_model_path:
            from trace_engine.transcription import StreamingTranscriber
            self.streaming_transcriber = StreamingTranscriber(
                self.config.whisper_stream_exe, self.config.whisper_model_path,
                language=self.config.whisper_language,
                n_threads=int(self.config.whisper_threads or 4))
        if self.config.whisper_model_path and transcriber is None:
            from trace_engine.transcription import TranscriptionService
            # Preferir whisper_cli_exe (whisper-cli.exe, Vulkan batch).
            cli_exe = self.config.whisper_cli_exe or None
            self.transcription_service = TranscriptionService(
                self.config.whisper_model_path,
                n_threads=int(self.config.whisper_threads or 4),
                language=self.config.whisper_language,
                whisper_cli_exe=cli_exe,
            )
        if transcriber is not None:
            self.transcription_service = transcriber
        self.collector = None
        self._started = False
        self._closed = False
        self._stop_succeeded = True

    @property
    def capabilities(self) -> Dict[str, bool]:
        """Capacidades nativas disponibles en esta plataforma."""
        return {
            "timeline": True,
            "windows_collectors": platform.system() == "Windows",
        }

    def start(self) -> None:
        """Inicia los recolectores compatibles sin hacer fallar el núcleo."""
        if self._closed:
            raise RuntimeError("TRACE is closed")
        if self._started:
            return
        self._started = True
        if platform.system() != "Windows":
            logger.info("TRACE iniciado sin recolectores nativos para %s",
                        platform.system())
            return
        try:
            from trace_engine.collectors.coordinator import ContextCollector
            collector_config = self.config.as_dict()
            collector_config["transcription_service"] = self.transcription_service
            self.collector = ContextCollector(config=collector_config, db=self.db)
            self.collector.start()
        except Exception as exc:
            logger.warning("No se pudieron activar los recolectores nativos: %s",
                           exc, exc_info=True)

    def pause_collectors(self) -> None:
        """Pause context capture without closing shared speech or storage."""
        if self.collector is not None:
            self.collector.stop()
            self.collector = None
        self._started = False

    def stop(self) -> bool:
        """Release resources even when collectors were never started."""
        if self._closed:
            return self._stop_succeeded
        success = True
        try:
            self.pause_collectors()
        except Exception:
            logger.exception("Failed to stop context collectors")
            success = False
        if self.transcription_service is not None:
            try:
                success = (self.transcription_service.close() is not False) and success
            except Exception:
                logger.exception("Failed to close transcription service")
                success = False
            finally:
                self.transcription_service = None
        if self.streaming_transcriber is not None:
            try:
                self.streaming_transcriber.stop()
            except Exception:
                logger.exception("Failed to stop streaming transcription")
                success = False
        try:
            self.db.close()
        except Exception:
            logger.exception("Failed to close timeline database")
            success = False
        self._closed = True
        self._stop_succeeded = success
        return success

    def insert(self, source: str, content: str, app_name: str = "",
               window_title: str = "", timestamp: Optional[float] = None) -> bool:
        """Inyecta un evento desde un recolector externo."""
        return self.db.insert(source, app_name, window_title, content, timestamp)

    def search(self, query: str, limit: int = 20):
        """Busca eventos recientes por palabras clave."""
        return self.db.search_by_keywords(query, limit)

    def transcribe(self, audio, *, source: str = "interactive"):
        """Transcribe audio con prioridad interactiva por defecto."""
        if self.transcription_service is None:
            raise RuntimeError("TRACE no tiene un servicio Whisper configurado")
        from trace_engine.transcription import TranscriptionPriority
        return self.transcription_service.transcribe(
            audio, source=source, priority=TranscriptionPriority.INTERACTIVE)

    def close(self) -> None:
        """Alias de stop() para facilitar integración con context managers."""
        self.stop()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
