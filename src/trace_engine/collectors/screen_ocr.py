"""Recolector pasivo: OCR de pantalla a intervalo (Windows.Media.Ocr).

Cada `ocr_interval_seconds` (config, default 12): captura el monitor
de la ventana activa con mss, hashea la imagen reducida en grises (blake2b — rápido y
barato) y, solo si la pantalla cambió, corre el OCR NATIVO de Windows
(paquete `winsdk`, winsdk.windows.media.ocr) en español si el idioma está
disponible (fallback al perfil del usuario). Inserta source='ocr' con
app_name/window_title de la ventana activa en ese momento.

La API winrt es async: el hilo del recolector tiene su propio event loop
(asyncio.new_event_loop) y cada OCR es un run_until_complete.

Objetivo de recursos: <1% CPU en idle — la captura+hash es barata y el OCR
solo corre cuando la pantalla cambió. winsdk se importa perezoso: si falta el
paquete, el recolector se desactiva con un log claro y el resto sigue.
"""

import asyncio
import hashlib
import io
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from trace_engine.collectors.window_change import get_window_info
from trace_engine.storage.timeline_db import TimelineDB

logger = logging.getLogger(__name__)

# A host may exclude its own overlays only for the captured frame. Standalone
# TRACE keeps its original behavior and never depends on a host GUI package.
from contextlib import nullcontext
_capture_scope = nullcontext
_excluded_window = lambda hwnd: False


def configure_capture_policy(capture_scope, excluded_window):
    global _capture_scope, _excluded_window
    _capture_scope, _excluded_window = capture_scope, excluded_window


def screen_capture_scope():
    return _capture_scope()


def is_excluded_window(hwnd):
    return bool(_excluded_window(hwnd))

_HASH_WIDTH = 320  # ancho del thumbnail en grises para el hash de cambio


@dataclass(frozen=True)
class Observation:
    hwnd: int
    app: str
    title: str
    bounds: tuple
    captured_at: float

    @property
    def identity(self):
        return self.hwnd, self.app, self.title, self.bounds

    @property
    def monitor(self):
        left, top, right, bottom = self.bounds
        return dict(left=left, top=top, width=right-left, height=bottom-top)


def foreground_observation():
    import win32api
    import win32gui
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        raise OSError('No foreground window to identify the capture')
    app, title = get_window_info(hwnd)
    bounds = win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd, 2))['Monitor']
    return Observation(hwnd, app, title, tuple(bounds), time.time())

# Motor OCR compartido (lazy; thread-safe para el recolector y api_brain).
_engine_lock = threading.Lock()
_engine_cached = None
_engine_failed = False


def get_ocr_engine():
    """OcrEngine de Windows: español si está disponible, si no el del perfil.

    Devuelve None (con log claro) si falta winsdk o no hay motor usable.
    """
    global _engine_cached, _engine_failed
    if _engine_cached is not None or _engine_failed:
        return _engine_cached
    with _engine_lock:
        if _engine_cached is not None or _engine_failed:
            return _engine_cached
        try:
            from winsdk.windows.globalization import Language
            from winsdk.windows.media.ocr import OcrEngine

            spanish = Language("es")
            if OcrEngine.is_language_supported(spanish):
                _engine_cached = OcrEngine.try_create_from_language(spanish)
                logger.info("✅ [OCR] Motor OCR de Windows en español")
            else:
                logger.warning("⚠️ [OCR] Español no disponible en Windows OCR; "
                               "se usa el idioma del perfil de usuario")
                _engine_cached = OcrEngine.try_create_from_user_profile_languages()
            if _engine_cached is None:
                logger.error("❌ [OCR] Windows no devolvió un motor OCR usable")
                _engine_failed = True
        except ImportError as e:
            logger.error("❌ [OCR] Falta el paquete 'winsdk' (OCR nativo): %s", e)
            _engine_failed = True
        except Exception as e:
            logger.error("❌ [OCR] Error inicializando Windows OCR: %s", e, exc_info=True)
            _engine_failed = True
    return _engine_cached


async def _ocr_png_bytes(engine, png_bytes: bytes) -> str:
    """Corre Windows.Media.Ocr sobre un PNG en memoria."""
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(png_bytes)
    await writer.store_async()
    stream.seek(0)

    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bitmap)
    return result.text or ""


def ocr_image(pil_image) -> str:
    """OCR nativo de Windows sobre una PIL.Image. Devuelve "" si falla.

    Optimizado: usa codificación BMP en memoria que decodifica ~3x más rápido
    en Windows.Media.Ocr (WinRT) que PNG.
    """
    engine = get_ocr_engine()
    if engine is None:
        return ""
    try:
        buffer = io.BytesIO()
        pil_image.save(buffer, format="BMP")
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                _ocr_png_bytes(engine, buffer.getvalue())) or ""
        finally:
            loop.close()
    except Exception as e:
        logger.error("❌ [OCR] Error procesando imagen: %s", e, exc_info=True)
        return ""


class ScreenOcrCollector:
    """Inserta en el timeline el texto visible en pantalla cuando cambia."""

    def __init__(self, db: TimelineDB, config=None):
        self.config = config or {}
        self.db = db
        self.interval = float(self.config.get("ocr_interval_seconds", 12))
        self.max_chars = int(self.config.get("ocr_max_chars", 4000))
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_hash: Optional[bytes] = None
        self._last_text = ""
        self._last_capture_identity = None
        self._last_text_identity = None
        self.excluded_apps = {str(x).lower() for x in self.config.get("trace_excluded_apps", [])}
        self._sct = None
        self._engine = None       # OcrEngine compartido (get_ocr_engine)

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="collector-screen-ocr")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None

    # ------------------------------------------------------------- hilo OCR

    def _run(self) -> None:
        self._engine = get_ocr_engine()
        if self._engine is None:
            return  # log ya emitido; el recolector queda inactivo
        logger.info("✅ [OCR] Recolector de pantalla activo (cada %.0fs)", self.interval)
        loop = asyncio.new_event_loop()
        import mss
        self._sct = mss.mss()
        try:
            while not self._stop_event.wait(self.interval):
                try:
                    self._tick(loop)
                except Exception as e:
                    logger.error("❌ [OCR] Error en el ciclo OCR: %s", e, exc_info=True)
        finally:
            loop.close()
            if self._sct is not None:
                self._sct.close()
                self._sct = None
            logger.info("🛑 [OCR] Recolector de pantalla detenido")

    # -------------------------------------------------------------- un tick

    def _tick(self, loop) -> None:
        try:
            observed = foreground_observation()
            if (not observed.app or is_excluded_window(observed.hwnd)
                    or observed.app.lower() in self.excluded_apps):
                return
            png_bytes, changed = self._capture_if_changed(observed)
        except Exception as e:
            logger.warning("⚠️ [OCR] Captura fallida, saltando frame: %s", e)
            return
        if not changed:
            return
        if foreground_observation().identity != observed.identity:
            self._last_hash = None
            return
        try:
            text = loop.run_until_complete(_ocr_png_bytes(self._engine, png_bytes))
        except Exception:
            self._last_hash = None
            raise
        if foreground_observation().identity != observed.identity or self._stop_event.is_set():
            self._last_hash = None
            return
        text = " ".join((text or "").split())
        if not text:
            return
        if len(text) > self.max_chars:
            text = text[:self.max_chars]
        # Cambios visuales menores pueden producir el mismo texto. No lo
        # vuelvas a almacenar si el OCR no aporta contexto nuevo.
        if text == self._last_text and observed.identity == self._last_text_identity:
            return
        if self.db.insert("ocr", observed.app, observed.title, text, observed.captured_at,
                observation={'processed_at':time.time(), 'window_id':str(observed.hwnd),
                             'monitor':observed.monitor}):
            self._last_text = text
            self._last_text_identity = observed.identity
            logger.debug("[OCR] Pantalla: %d chars (%s - %s)", len(text), observed.app, observed.title[:40])

    def _capture_if_changed(self, observation=None) -> tuple:
        """(png_bytes, changed): captura + hash del thumbnail en grises.

        El hash corre sobre la imagen reescalada a _HASH_WIDTH px en escala de
        grises: barato y suficiente para detectar cambios de contenido.
        """
        from PIL import Image

        if self._sct is None:
            import mss
            self._sct = mss.mss()
        monitor = observation.monitor if observation else self._sct.monitors[1]
        try:
            with screen_capture_scope():
                shot = self._sct.grab(monitor)
        except Exception as e:
            # BitBlt u otro error GDI: reinicializar contexto mss en el próximo tick.
            logger.warning("⚠️ [OCR] Error en grab (BitBlt?), reiniciando mss: %s", e)
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None
            return b"", False
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        thumb = img.convert("L")
        ratio = _HASH_WIDTH / float(thumb.width)
        thumb = thumb.resize((_HASH_WIDTH, max(1, int(thumb.height * ratio))),
                             Image.Resampling.BILINEAR)
        digest = hashlib.blake2b(thumb.tobytes(), digest_size=16).digest()
        identity = observation.identity if observation else None
        if digest == self._last_hash and identity == self._last_capture_identity:
            return b"", False
        self._last_hash = digest
        self._last_capture_identity = identity

        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return buffer.getvalue(), True
