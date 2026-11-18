"""Deferred transcription and indexing for encrypted call clips."""
import logging
import queue
import sqlite3
import threading
import time

from trace_engine.collectors.meeting_capture import FILTERED_MARKERS
from trace_engine.transcription import TranscriptionPriority


logger = logging.getLogger(__name__)
RECENT_SECONDS = 15 * 60
QUERY_SECONDS = 15
QUERY_CLIPS = 8


class CallIndex:
    """Index old clips in background and coalesce transcription for explicit queries.

    service() resolves the current ASR provider. admit(provider, background)
    is evaluated before decoding and submission, so the host owns admission.
    """

    def __init__(self, archive, service, admit, *, excluded=lambda: (), retention=lambda: 72,
                 recent_seconds=RECENT_SECONDS, job_timeout=QUERY_SECONDS):
        self.archive, self.service, self.admit = archive, service, admit
        self.excluded, self.retention = excluded, retention
        self.recent_seconds, self.job_timeout = recent_seconds, job_timeout
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._worker = None
        self._inflight = None

    def start(self):
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, name='trace-call-index', daemon=True)
        self._worker.start()

    def stop(self):
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=1)

    def _finish(self):
        if self._inflight is None:
            return
        clip, future = self._inflight
        try:
            result = future.get_nowait()
        except queue.Empty:
            return
        self._inflight = None
        if getattr(result, 'error', None):
            self.archive.defer_retry(clip, 60)
            return
        text = result.text.strip()
        if text.upper() in FILTERED_MARKERS:
            text = ''
        self.archive.save_transcript(clip, text)

    def transcribe(self, clip, *, deadline, background=False, checkpoint=lambda: None):
        service = self.service()
        submitted = False
        while time.monotonic() < deadline and not (background and self._stop.is_set()):
            checkpoint()
            with self._lock:
                self._finish()
                existing = self.archive.transcript(clip)
                if existing is not None:
                    return existing
                if self._inflight is None:
                    if submitted or service is None or not self.admit(service, background):
                        return None
                    if background and getattr(service, 'pending_count', 0):
                        return None
                    audio = self.archive.read_audio(clip)
                    if audio is None or not self.admit(service, background):
                        return None
                    priority = TranscriptionPriority.MEETING if background else int(TranscriptionPriority.INTERACTIVE) + 1
                    self._inflight = (clip, service.submit(audio, source='meeting', priority=priority))
                    submitted = True
                elif self._inflight[0].id == clip.id:
                    submitted = True
            delay = min(.025, max(0, deadline - time.monotonic()))
            if background:
                self._stop.wait(delay)
            else:
                time.sleep(delay)
        return None

    def index_one(self, *, now=None):
        with self._lock:
            self._finish()
        service = self.service()
        if self._stop.is_set() or service is None or not self.admit(service, True):
            return False
        current = time.time() if now is None else now
        clips, _ = self.archive.select(current - self.retention() * 3600, current - self.recent_seconds,
                                      excluded=self.excluded(), pending=True, oldest=True, limit=1)
        if not clips or clips[0].end > current - self.recent_seconds:
            return False
        return self.transcribe(clips[0], deadline=time.monotonic() + self.job_timeout, background=True) is not None

    def _run(self):
        while not self._stop.wait(2):
            try:
                self.index_one()
            except (OSError, sqlite3.Error, RuntimeError, ValueError):
                logger.warning('Could not update TRACE call index', exc_info=True)
