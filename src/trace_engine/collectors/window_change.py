"""Recolector pasivo: cambios de ventana activa (SetWinEventHook).

Hook global de EVENT_SYSTEM_FOREGROUND con ctypes (pywin32 no envuelve
SetWinEventHook). El hook exige un message loop de Win32 en su propio hilo
(win32gui.PumpMessages); al parar se postea WM_QUIT al hilo y se desengancha
con UnhookWinEvent antes de salir del loop.

En cada cambio inserta source='app_change' con el nombre del proceso
(psutil) y el título de la ventana (GetWindowText). Se deduplican eventos
consecutivos idénticos (misma app + mismo título), que Win32 repite mucho.
"""

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Optional

from trace_engine.storage.timeline_db import TimelineDB

logger = logging.getLogger(__name__)

EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
WM_QUIT = 0x0012

# Firma del callback WinEventProc.
_WIN_EVENT_PROC = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,  # hWinEventHook
    wintypes.DWORD,   # event
    wintypes.HWND,    # hwnd
    wintypes.LONG,    # idObject
    wintypes.LONG,    # idChild
    wintypes.DWORD,   # idEventThread
    wintypes.DWORD,   # dwmsEventTime
)


def get_foreground_window_info() -> tuple:
    """(app_name, window_title) de la ventana en primer plano.

    Compartido con screen_ocr (que etiqueta sus capturas con la ventana
    activa del momento). Errores hacia afuera para que el llamador loguee.
    """
    import win32gui
    return get_window_info(win32gui.GetForegroundWindow())


def get_window_info(hwnd) -> tuple:
    """Read metadata for the SAME handle supplied by a capture snapshot."""
    import win32gui
    import win32process
    title = win32gui.GetWindowText(hwnd) or "" if hwnd else ""
    app_name = ""
    try:
        import psutil

        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        app_name = psutil.Process(pid).name()
    except Exception as e:
        logger.warning("⚠️ [WINDOWS] Sin nombre de proceso para hwnd %s: %s", hwnd, e)
    return app_name, title


class WindowChangeCollector:
    """Inserta en el timeline cada cambio de ventana en primer plano."""

    def __init__(self, db: TimelineDB, config=None):
        self.db = db
        self.config = config or {}
        self.excluded_apps = {str(x).lower() for x in self.config.get("trace_excluded_apps", [])}
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._hook = None
        self._callback = None  # referencia viva: si el GC la recoge, el hook crashea
        self._last_event: Optional[tuple] = None  # (app_name, window_title)
        self._session_id: Optional[int] = None
        self._user32 = ctypes.windll.user32

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="collector-window-change")
        self._thread.start()

    def stop(self) -> None:
        if self._thread_id is not None:
            # WM_QUIT saca al hilo del PumpMessages; ahí se hace UnhookWinEvent.
            self._user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=5)
        if self._session_id is not None:
            self.db.end_window_session(self._session_id)
            self._session_id = None
        self._thread = None
        self._thread_id = None

    # ------------------------------------------------------------ hilo hook

    def _run(self) -> None:
        import win32gui  # pywin32 (message loop + GetWindowText)

        self._thread_id = threading.get_native_id()
        self._callback = _WIN_EVENT_PROC(self._on_foreground)
        self._hook = self._user32.SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND,
            None, self._callback, 0, 0, WINEVENT_OUTOFCONTEXT)
        if not self._hook:
            logger.error("❌ [WINDOWS] SetWinEventHook falló; recolector de ventanas inactivo")
            return
        logger.info("✅ [WINDOWS] Hook de cambio de ventana activo")
        try:
            win32gui.PumpMessages()  # retorna al recibir WM_QUIT
        except Exception as e:
            logger.error("❌ [WINDOWS] Error en el message loop: %s", e, exc_info=True)
        finally:
            if self._hook:
                self._user32.UnhookWinEvent(self._hook)
                self._hook = None
            logger.info("🛑 [WINDOWS] Hook de cambio de ventana detenido")

    def _on_foreground(self, hook, event, hwnd, id_object, id_child,
                       event_thread, event_time) -> None:
        # Solo cambios de ventana raíz (idObject OBJID_WINDOW = 0).
        if id_object != 0 or not hwnd:
            return
        try:
            self._record_foreground(hwnd)
        except Exception as e:
            logger.error("❌ [WINDOWS] Error registrando ventana: %s", e, exc_info=True)

    def _record_foreground(self, hwnd) -> None:
        import win32gui

        title = win32gui.GetWindowText(hwnd) or ""
        app_name = ""
        try:
            import win32process
            import psutil

            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            app_name = psutil.Process(pid).name()
        except Exception as e:
            logger.warning("⚠️ [WINDOWS] Sin nombre de proceso para hwnd %s: %s", hwnd, e)

        event_key = (app_name, title)
        if app_name.lower() in self.excluded_apps:
            return
        if event_key == self._last_event:
            return  # el mismo foco repetido no aporta contexto
        if self._session_id is not None:
            self.db.end_window_session(self._session_id)
        self._session_id = self.db.start_window_session(app_name, title)
        self._last_event = event_key

        if self.db.insert("app_change", app_name, title, title or app_name):
            logger.debug("[WINDOWS] Foco: %s - %s", app_name, title[:60])
