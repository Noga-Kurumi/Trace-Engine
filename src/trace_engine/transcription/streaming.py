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
                   "--sample-rate", "16000", "--vad", "--step", "200",
                   "--length", "2000"]
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
        if process.stdout is None:
            return
        for raw in iter(process.stdout.readline, b""):
            text = raw.decode("utf-8", errors="ignore").strip()
            # Whisper stream outputs lines like: [00:00:00.000 --> 00:00:01.000] Hello
            if text.startswith("[") and "-->" in text and "]" in text:
                text = text.split("]", 1)[1].strip()
            elif text.startswith("["):
                # Ignorar otras líneas de depuración que empiezan con [
                continue
                
            if text:
                callback(text)

    @staticmethod
    def _drain_stderr(process) -> None:
        if process.stderr is not None:
            for _ in iter(process.stderr.readline, b""):
                pass
