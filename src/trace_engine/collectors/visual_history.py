"""Foreground visual history. Pixel capture and privacy rules are supplied by the host."""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
from io import BytesIO
import logging
import threading
import time

from trace_engine.collectors.window_change import WindowChangeCollector, get_window_info
from trace_engine.visual_archive import MAX_JPEG_BYTES, VisualArchive


logger = logging.getLogger(__name__)

_FOREGROUND = 0x0003
_NAME_CHANGE = 0x800C
_DESKTOP_SWITCH = 0x0020


@dataclass(frozen=True)
class CaptureJob:
    hwnd: int
    app: str
    title: str
    session_id: int
    revision: int


def jpeg_for_history(image):
    """Half-size by default, with a 720 px short-edge floor and no upscaling."""
    from PIL import Image

    scale = min(1, max(0.5, 720 / min(image.size)))
    size = tuple(max(1, round(edge * scale)) for edge in image.size)
    reduced = image.resize(size, Image.Resampling.LANCZOS, reducing_gap=2.0) if size != image.size else image
    try:
        for quality in (84, 75, 65):
            with BytesIO() as output:
                reduced.save(output, format='JPEG', quality=quality, subsampling=0)
                if output.tell() <= MAX_JPEG_BYTES:
                    return output.getvalue()
        return None
    finally:
        if reduced is not image:
            reduced.close()


def has_chromium_browser_controls(hwnd):
    """Detect a renamed Chromium browser without classifying Electron as one."""
    import win32gui

    if not hwnd or not win32gui.GetClassName(hwnd).startswith('Chrome_WidgetWin_'):
        return False
    import comtypes
    from trace_engine.collectors.uia_context import _get_uia

    comtypes.CoInitialize()
    try:
        automation = _get_uia()
        if automation is None:
            return True
        from comtypes.gen import UIAutomationClient as UIA

        automation = automation.QueryInterface(UIA.IUIAutomation2)
        automation.ConnectionTimeout = 200
        automation.TransactionTimeout = 200
        window = automation.ElementFromHandle(hwnd)
        toolbar = automation.CreatePropertyCondition(UIA.UIA_ClassNamePropertyId, 'ReloadButton')
        return bool(window.FindFirst(UIA.TreeScope_Descendants, toolbar))
    except (comtypes.COMError, OSError):
        return True
    finally:
        comtypes.CoUninitialize()


class VisualWindowCollector(WindowChangeCollector):
    """Record focus on its WinEvent thread and keep only one pending pixel job."""

    def __init__(self, db, *, mode, config, grab_region, ocr=None, archive=None,
                 excluded=lambda app: False, ignore_window=lambda hwnd: False,
                 skip_app=lambda app: False, skip_window=lambda hwnd: False,
                 screenshot_interval=5.0):
        if mode not in ('ocr', 'screenshot') or (mode == 'ocr' and ocr is None):
            raise ValueError('Visual history requires screenshot mode or an OCR reader')
        if screenshot_interval <= 0:
            raise ValueError('Visual capture interval must be positive')
        super().__init__(db, config=config)
        self.mode = mode
        self.archive = archive if archive is not None else VisualArchive(db.db_path)
        self.grab_region, self.ocr = grab_region, ocr
        self.excluded, self.ignore_window = excluded, ignore_window
        self.skip_app, self.skip_window = skip_app, skip_window
        self.screenshot_interval = screenshot_interval
        self._halt = threading.Event()
        self._capture_changed = threading.Condition()
        self._pending = None
        self._periodic = None
        self._last_frame = None
        self._revision = 0
        self._capture_thread = None

    def start(self):
        if self._capture_thread is not None and self._capture_thread.is_alive():
            return
        self._halt.clear()
        self._last_event = None
        self._last_frame = None
        self._capture_thread = threading.Thread(target=self._collect_pixels, name='trace-visual', daemon=True)
        self._capture_thread.start()
        super().start()

    def stop(self):
        self._halt.set()
        with self._capture_changed:
            self._pending = self._periodic = None
            self._capture_changed.notify()
        super().stop()
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=3)
            if not self._capture_thread.is_alive():
                self._capture_thread = None

    def _run(self):
        import win32gui

        self._thread_id = threading.get_native_id()
        api = ctypes.WinDLL('user32')
        callback_type = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                           wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
        api.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, callback_type,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
        api.SetWinEventHook.restype = wintypes.HANDLE
        api.UnhookWinEvent.argtypes = [wintypes.HANDLE]
        self._callback = callback_type(self._on_foreground)
        hooks = []
        try:
            for event in (_FOREGROUND, _NAME_CHANGE, _DESKTOP_SWITCH):
                hook = api.SetWinEventHook(event, event, None, self._callback, 0, 0, 0)
                if not hook:
                    logger.warning('TRACE could not register window event %s', event)
                else:
                    hooks.append(hook)
            if hooks and not self._halt.is_set():
                self._record_foreground(win32gui.GetForegroundWindow())
                win32gui.PumpMessages()
        except Exception:
            logger.exception('TRACE window observation stopped')
        finally:
            for hook in hooks:
                api.UnhookWinEvent(hook)

    def _on_foreground(self, hook, event, hwnd, id_object, id_child, event_thread, event_time):
        import win32gui

        if self._halt.is_set() or id_object or id_child:
            return
        try:
            foreground = win32gui.GetForegroundWindow()
            if event != _DESKTOP_SWITCH and (hwnd or 0) != foreground:
                return
            self._record_foreground(foreground)
        except Exception:
            logger.debug('TRACE focus observation expired', exc_info=True)

    def _record_foreground(self, hwnd):
        # The shell/empty foreground is a distinct desktop observation.
        app, title = get_window_info(hwnd) if hwnd else ('explorer.exe', 'Desktop')
        key = hwnd, app, title
        if key == self._last_event:
            return
        self._last_event = key
        with self._capture_changed:
            self._revision += 1
            self._pending = self._periodic = None
            self._capture_changed.notify()
        if self._session_id:
            self.db.end_window_session(self._session_id)
            self._session_id = None
        if not app or self.ignore_window(hwnd) or self.excluded(app):
            return
        stamp = time.time()
        self._session_id = self.db.start_window_session(app, title, stamp)
        self.db.insert('app_change', app, title, title or app, stamp)
        if self.skip_app(app) or not self._session_id:
            return
        with self._capture_changed:
            job = CaptureJob(hwnd, app, title, self._session_id, self._revision)
            self._pending = job
            if self.mode == 'screenshot' and hwnd:
                self._periodic = (time.monotonic() + self.screenshot_interval, job)
            self._capture_changed.notify()

    def _current(self, job):
        import win32gui

        return (not self._halt.is_set() and job.revision == self._revision
                and win32gui.GetForegroundWindow() == job.hwnd
                and (not job.hwnd or win32gui.GetWindowText(job.hwnd) == job.title))

    def _next_capture(self):
        with self._capture_changed:
            while not self._halt.is_set():
                if self._pending is not None:
                    job, self._pending = self._pending, None
                    return job, False
                if self._periodic is None:
                    self._capture_changed.wait()
                    continue
                deadline, job = self._periodic
                now = time.monotonic()
                if now < deadline:
                    self._capture_changed.wait(deadline - now)
                    continue
                self._periodic = (now + self.screenshot_interval, job)
                return job, True
            return None

    def _collect_pixels(self):
        from mss import mss

        try:
            with mss() as screen:
                while (capture := self._next_capture()) is not None:
                    job, periodic = capture
                    if not periodic and self._halt.wait(.22):
                        break
                    try:
                        if self._current(job):
                            self._store_capture(job, screen, window_only=periodic)
                    except Exception:
                        logger.debug('TRACE visual observation skipped', exc_info=True)
        except Exception:
            logger.exception('TRACE could not start the visual capture worker')

    def _store_capture(self, job, screen, *, window_only=False):
        started = time.perf_counter()
        if self.skip_window(job.hwnd):
            return
        image, captured_at, monitor = self.grab_region(job, self.mode, screen, self.excluded,
                                                      window_only=window_only)
        with image:
            if self.mode == 'screenshot':
                frame = (job.revision, image.size, hashlib.sha256(image.tobytes()).digest())
                if frame == self._last_frame:
                    return
                jpeg = jpeg_for_history(image)
                if jpeg and self._current(job) and self.archive.record(job.session_id, captured_at, jpeg):
                    self._last_frame = frame
            else:
                from PIL import Image

                image.thumbnail((2048, 2048), Image.Resampling.LANCZOS, reducing_gap=2.0)
                text = self.ocr(image, language=self.config.get('whisper_language'))
                if text and self._current(job):
                    self.db.insert('ocr', job.app, job.title, text[:self.config.get('ocr_max_chars', 4000)],
                                   captured_at, observation={'processed_at': time.time(),
                                       'window_id': str(job.hwnd), 'monitor': monitor})
        logger.debug('TRACE %s capture finished in %.1f ms', self.mode, (time.perf_counter() - started) * 1000)
