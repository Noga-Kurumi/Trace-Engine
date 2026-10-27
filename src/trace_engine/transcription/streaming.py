"""Control del streaming Whisper para transcripción parcial."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class StreamingTranscriber:
    """Dueño del proceso whisper-stream-pcm.exe y su ciclo de vida."""

    def __init__(self, executable: str, model_path: str, *, language: str = "es",
                 n_threads: int = 4):
        self.executable = executable
        self.model_path = model_path
        self.language = language
        self.n_threads = int(n_threads or 4)
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self, callback: Callable[[str], None]) -> bool:
        with self._lock:
            if self.running:
                return False
            if not os.path.exists(self.executable) or not os.path.exists(self.model_path):
                logger.error("[TRACE-ASR] Streaming sin binario o modelo")
                return False
            cmd = [self.executable, "-m", self.model_path, "-l", self.language,
                   "-t", str(self.n_threads), "-i", "-", "--format", "f32",
                   "--sample-rate", "16000", "--step", "750",
                   "--length", "4000", "--audio-ctx", "256",
                   "--max-tokens", "128", "--no-fallback"]
            try:
                creationflags = 0
                if os.name == "nt":
                    below_normal = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
                    creationflags = subprocess.CREATE_NO_WINDOW | below_normal
                self._process = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, creationflags=creationflags)
                self._reader = threading.Thread(
                    target=self._read_output, args=(self._process, callback),
                    daemon=True, name="trace-whisper-stream")
                self._reader.start()
                threading.Thread(target=self._drain_stderr,
                                 args=(self._process,), daemon=True,
                                 name="trace-whisper-stderr").start()
                return True
            except Exception as exc:
                logger.error("[TRACE-ASR] No se pudo iniciar streaming: %s", exc,
                             exc_info=True)
                self._process = None
                return False

    def send(self, audio) -> None:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None or process.stdin is None:
                return
        # Never hold the lifecycle lock during a potentially blocked pipe write.
        try:
            import numpy as np
            with self._write_lock:
                process.stdin.write(np.ascontiguousarray(audio, dtype=np.float32).tobytes())
                process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            logger.warning("Whisper streaming pipe closed")

    def stop(self) -> None:
        with self._lock:
            process, self._process = self._process, None
            if process is None:
                return
            try:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
            finally:
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except (OSError, ValueError):
                            logger.debug("Whisper stream pipe already closed")
            reader, self._reader = self._reader, None
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1)

    @staticmethod
    def _read_output(process, callback) -> None:
        """Decode transport frames, including CR repaints, without leaking controls."""
        import codecs
        import re
        if process.stdout is None:
            return
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        ansi = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
        marker = re.compile(r"### Transcription \d+ (?:END|START \| t0 = \d+ ms \| t1 = \d+ ms)")
        timestamp = re.compile(r"^\[\d{2}:\d{2}:\d{2}\.\d{3} --> \d{2}:\d{2}:\d{2}\.\d{3}\]\s*")
        def deliver(frame):
            text = ansi.sub("", frame).strip()
            if not text or marker.fullmatch(text):
                return
            text, count = timestamp.subn("", text, count=1)
            if not count and text.startswith("["):
                return
            if text:
                callback(text)
        read = getattr(process.stdout, 'read1', process.stdout.read)
        while True:
            try:
                raw = read(4096)
            except (OSError, ValueError):
                break
            if not raw:
                pending += decoder.decode(b"", final=True)
                if pending:
                    deliver(pending)
                break
            pending += decoder.decode(raw)
            frames = re.split(r"[\r\n]", pending)
            pending = frames.pop()
            for frame in frames:
                deliver(frame)

    @staticmethod
    def _drain_stderr(process) -> None:
        if process.stderr is not None:
            for _ in iter(process.stderr.readline, b""):
                pass
