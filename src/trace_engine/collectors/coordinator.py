"""Coordinador de recolectores pasivos de contexto (Fase B).

ContextCollector crea la TimelineDB compartida, instancia los tres
recolectores (cambio de ventana, portapapeles, OCR de pantalla) y gestiona su
ciclo de vida. Además mantiene la base circular: cleanup_old_records() al
arranque y cada hora (timeline_retention_hours, default 72).

Robustez: start() NUNCA lanza — si un recolector falla al arrancar se loguea
y se sigue con los demás. Hilos daemon con errores atrapados en cada
recolector: el contexto es un extra, nunca debe tumbar la app.
"""

import logging
import threading
from typing import Optional

from trace_engine.storage.timeline_db import TimelineDB

logger = logging.getLogger(__name__)

_CLEANUP_INTERVAL_S = 3600  # limpieza de la base circular cada hora


class ContextCollector:
    """Arranca/para los recolectores pasivos y la limpieza periódica."""

    def __init__(self, config=None, db: Optional[TimelineDB] = None):
        self.config = config or {}
        self._owns_db = db is None
        self.db = db if db is not None else TimelineDB()
        self.retention_hours = float(self.config.get("timeline_retention_hours", 72))
        self._collectors = []
        self._cleanup_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._started = False

    # -------------------------------------------------------------- builders

    def _build_collectors(self) -> None:
        """Instancia los recolectores; el OCR y el meeting reciben la config."""
        from trace_engine.collectors.clipboard import ClipboardCollector
        from trace_engine.collectors.screen_ocr import ScreenOcrCollector
        from trace_engine.collectors.window_change import WindowChangeCollector

        self._collectors = [WindowChangeCollector(self.db, config=self.config)]
        if self.config.get("trace_clipboard_enabled", True):
            self._collectors.append(ClipboardCollector(self.db, config=self.config))
        if self.config.get("trace_screen_enabled", True):
            self._collectors.append(ScreenOcrCollector(self.db, config=self.config))

        # Audio de reuniones (fase C): solo si la detección está habilitada.
        if self.config.get("meeting_detection_enabled", True):
            from trace_engine.collectors.meeting_audio import MeetingAudioCollector

            self._collectors.append(MeetingAudioCollector(self.db, config=self.config))

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Arranca todo. No lanza: un recolector roto no frena a los demás."""
        if self._started:
            return
        self._started = True
        self._stop_event.clear()

        if not self._collectors:
            self._build_collectors()

        for collector in self._collectors:
            name = type(collector).__name__
            try:
                collector.start()
                logger.info("✅ [CONTEXT] Recolector %s arrancado", name)
            except Exception as e:
                logger.error("❌ [CONTEXT] No se pudo arrancar %s: %s", name, e,
                             exc_info=True)

        # Base circular: limpieza al arranque y luego cada hora.
        try:
            deleted = self.db.cleanup_old_records(self.retention_hours)
            logger.info("🧹 [CONTEXT] Limpieza inicial del timeline: %d registros", deleted)
        except Exception as e:
            logger.error("❌ [CONTEXT] Error en la limpieza inicial: %s", e, exc_info=True)
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="collector-cleanup")
        self._cleanup_thread.start()
        logger.info("✅ [CONTEXT] Recolección de contexto activa "
                    "(retención: %.0fh)", self.retention_hours)

    def stop(self) -> None:
        """Para los recolectores y el hilo de limpieza. Nunca lanza."""
        if not self._started:
            return
        self._started = False
        self._stop_event.set()
        for collector in self._collectors:
            name = type(collector).__name__
            try:
                collector.stop()
                logger.info("🛑 [CONTEXT] Recolector %s detenido", name)
            except Exception as e:
                logger.error("❌ [CONTEXT] Error deteniendo %s: %s", name, e, exc_info=True)
        if self._cleanup_thread:
            self._cleanup_thread.join(timeout=5)
            self._cleanup_thread = None
        if self._owns_db:
            try:
                self.db.close()
            except Exception as e:
                logger.warning("⚠️ [CONTEXT] Error cerrando la base del timeline: %s", e)

    # --------------------------------------------------------------- limpieza

    def _cleanup_loop(self) -> None:
        while not self._stop_event.wait(_CLEANUP_INTERVAL_S):
            try:
                self.db.cleanup_old_records(self.retention_hours)
            except Exception as e:
                logger.error("❌ [CONTEXT] Error en la limpieza periódica: %s", e,
                             exc_info=True)

    # ------------------------------------------------------------------- PTT

    def set_capture_paused(self, active: bool) -> None:
        """Propaga el estado del push-to-talk a los recolectores que lo usan
        (MeetingAudioCollector pausa el mic mientras el asistente graba)."""
        for collector in self._collectors:
            setter = getattr(collector, "set_capture_paused", None)
            if setter is not None:
                try:
                    setter(active)
                except Exception as e:
                    logger.error("❌ [CONTEXT] Error en set_ptt_active(%s) de %s: %s",
                                 active, type(collector).__name__, e, exc_info=True)

    def set_playback_active(self,active):
        for collector in self._collectors:
            setter=getattr(collector,'set_playback_active',None)
            if callable(setter):setter(active)
