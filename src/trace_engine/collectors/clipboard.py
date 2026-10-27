"""Recolector pasivo: portapapeles (AddClipboardFormatListener).

Ventana message-only registrada como listener de WM_CLIPBOARDUPDATE en un
hilo con message loop propio. En cada aviso lee CF_UNICODETEXT (se ignora lo
que no sea texto o esté vacío) e inserta source='clipboard'. Se deduplica el
contenido idéntico consecutivo con un hash blake2b (copiar lo mismo dos
veces seguidas no aporta contexto).
"""

import ctypes
import hashlib
import logging
import threading
import re
from typing import Optional

from trace_engine.storage.timeline_db import TimelineDB

logger = logging.getLogger(__name__)

WM_CLIPBOARDUPDATE = 0x031D
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
CF_UNICODETEXT = 13
_CLASS_NAME = "TimelineClipboardListener"


class ClipboardCollector:
    """Inserta en el timeline cada texto que cae al portapapeles."""

    def __init__(self, db: TimelineDB, config=None):
        self.db = db
        self.config = config or {}
        self.max_chars = int(self.config.get("clipboard_max_chars", 10000))
        self.excluded_apps = {str(x).lower() for x in self.config.get("trace_excluded_apps", [])}
        self.redact_sensitive = bool(self.config.get("clipboard_redact_sensitive", True))
        self._thread: Optional[threading.Thread] = None
        self._hwnd = None
        self._stop_event = threading.Event()
        self._class_name = f"{_CLASS_NAME}_{id(self):x}"
        self._last_hash: Optional[bytes] = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="collector-clipboard")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._hwnd:
            try:
                import win32gui
                win32gui.PostMessage(self._hwnd, WM_CLOSE, 0, 0)
            except Exception as error:
                logger.debug("Clipboard close already in progress: %s", error)
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warning("Clipboard listener still stopping")
                return
        self._thread = None
        self._hwnd = None

    # --------------------------------------------------------- hilo listener

    def _run(self) -> None:
        import win32con
        import win32gui
        from ctypes import wintypes
        if self._stop_event.is_set():
            return
        user32 = ctypes.windll.user32
        user32.AddClipboardFormatListener.argtypes = [wintypes.HWND]
        user32.RemoveClipboardFormatListener.argtypes = [wintypes.HWND]
        hinst = win32gui.GetModuleHandle(None)
        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = self._wnd_proc
        wc.lpszClassName = self._class_name
        wc.hInstance = hinst
        try:
            class_atom = win32gui.RegisterClass(wc)
        except Exception:
            logger.exception("Clipboard window class registration failed")
            return
        try:
            self._hwnd = win32gui.CreateWindowEx(
                0, class_atom, self._class_name, 0, 0, 0, 0, 0,
                win32con.HWND_MESSAGE, 0, hinst, None)
            if self._stop_event.is_set():
                return
            if not user32.AddClipboardFormatListener(self._hwnd):
                logger.error("AddClipboardFormatListener failed")
                return
            logger.info("Clipboard listener active")
            if not self._stop_event.is_set():
                win32gui.PumpMessages()
        except Exception:
            logger.exception("Clipboard listener failed")
        finally:
            hwnd = self._hwnd
            try:
                if hwnd and win32gui.IsWindow(hwnd):
                    win32gui.DestroyWindow(hwnd)
            finally:
                self._hwnd = None
                try:
                    win32gui.UnregisterClass(self._class_name, hinst)
                except Exception:
                    logger.exception("Clipboard window class cleanup failed")
            logger.info("Clipboard listener stopped")

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        import win32gui

        if msg == WM_CLIPBOARDUPDATE:
            try:
                self._on_clipboard_update()
            except Exception as e:
                logger.error("❌ [CLIPBOARD] Error leyendo portapapeles: %s", e, exc_info=True)
            return 0
        if msg == WM_CLOSE:
            win32gui.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            ctypes.windll.user32.RemoveClipboardFormatListener(hwnd)
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _on_clipboard_update(self) -> None:
        import win32clipboard

        text = None
        try:
            win32clipboard.OpenClipboard()
            if win32clipboard.IsClipboardFormatAvailable(CF_UNICODETEXT):
                data = win32clipboard.GetClipboardData(CF_UNICODETEXT)
                if isinstance(data, str) and data.strip():
                    text = data.strip()
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass

        if not text:
            return
        if len(text) > self.max_chars:
            text = text[:self.max_chars]
        try:
            from trace_engine.collectors.window_change import get_foreground_window_info
            app_name, title = get_foreground_window_info()
        except Exception:
            app_name, title = "", ""
        if app_name.lower() in self.excluded_apps:
            return
        if self.redact_sensitive and re.search(
                r"(?i)(api[_-]?key|secret|password|token|authorization)\s*[:=]",
                text):
            logger.info("[CLIPBOARD] Contenido sensible omitido")
            return
        digest = hashlib.blake2b(text.encode("utf-8", "ignore"), digest_size=16).digest()
        if digest == self._last_hash:
            return  # mismo contenido copiado dos veces seguidas
        self._last_hash = digest

        if self.db.insert("clipboard", app_name, title, text):
            logger.debug("[CLIPBOARD] Copiado: %d chars", len(text))
