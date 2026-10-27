"""STT compartido con cola de prioridades para voz interactiva y reuniones.

TRACE es dueño del modelo y de su ciclo de vida. Los consumidores solo
encolan audio; nunca crean otra instancia del modelo por su cuenta.

El consumidor puede inyectar un modelo nativo (Whisper o Nemotron en Lindsay).
Sin modelo inyectado, los siguientes adaptadores Whisper mantienen la API:

Backend disponibles (en orden de preferencia):
  1. whisper-cli.exe (Vulkan/GPU) — si se proporciona ``whisper_cli_exe``.
  2. pywhispercpp   (CPU)         — fallback cuando no hay binario.
"""

from __future__ import annotations

import logging
import os
import queue
import struct
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class TranscriptionPriority(IntEnum):
    """Prioridades disponibles; menor valor significa mayor prioridad."""

    INTERACTIVE = 0
    MEETING = 10


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    source: str
    duration_seconds: float = 0.0
    error: Optional[str] = None


@dataclass(order=True)
class _Request:
    priority: int
    sequence: int
    audio: Any = field(compare=False)
    source: str = field(compare=False)
    future: "queue.Queue[TranscriptionResult]" = field(compare=False)


# ---------------------------------------------------------------------------
# Helpers para escribir WAV desde numpy sin depender de scipy/soundfile
# ---------------------------------------------------------------------------

def _write_wav(path: str, samples: Any, sample_rate: int = 16000) -> None:
    """Escribe un WAV mono float32→int16 a *path*."""
    try:
        import numpy as np
        data = np.ascontiguousarray(samples, dtype=np.float32)
        # Clamp y convertir a int16 (PCM 16-bit)
        data = np.clip(data, -1.0, 1.0)
        pcm = (data * 32767).astype(np.int16)
        raw = pcm.tobytes()
    except ImportError:
        # Sin numpy, asumimos que ya es bytes PCM int16
        raw = bytes(samples) if not isinstance(samples, (bytes, bytearray)) else samples

    n_channels = 1
    sampwidth = 2  # int16 = 2 bytes
    n_frames = len(raw) // sampwidth
    data_size = len(raw)
    riff_size = 36 + data_size

    with open(path, "wb") as f:
        # RIFF header
        f.write(b"RIFF")
        f.write(struct.pack("<I", riff_size))
        f.write(b"WAVE")
        # fmt chunk
        f.write(b"fmt ")
        f.write(struct.pack("<I", 16))          # chunk size
        f.write(struct.pack("<H", 1))           # PCM
        f.write(struct.pack("<H", n_channels))
        f.write(struct.pack("<I", sample_rate))
        f.write(struct.pack("<I", sample_rate * n_channels * sampwidth))  # byte rate
        f.write(struct.pack("<H", n_channels * sampwidth))                # block align
        f.write(struct.pack("<H", sampwidth * 8))                         # bits per sample
        # data chunk
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(raw)


# ---------------------------------------------------------------------------
# Multiprocessing Worker (Aislamiento de pywhispercpp)
# ---------------------------------------------------------------------------

_mp_model = None

def _init_mp_worker(model_path: str, n_threads: int, language: str) -> None:
    """Inicializador del proceso hijo. Carga pywhispercpp y su ggml.dll aislado."""
    global _mp_model
    import logging
    log = logging.getLogger(__name__)
    try:
        from pywhispercpp.model import Model
        if not os.path.exists(model_path):
            raise FileNotFoundError(model_path)
        _mp_model = Model(
            model_path,
            n_threads=n_threads,
            language=language,
            print_realtime=False,
            print_progress=False,
            print_timestamps=False,
        )
        log.info("[TRACE-ASR] Worker pywhispercpp inicializado con éxito.")
    except Exception as exc:
        log.error("[TRACE-ASR] Error inicializando Worker pywhispercpp: %s", exc, exc_info=True)
        _mp_model = None


def _interactive_audio_context(sample_count: int) -> int:
    """Bound encoder padding, never the recording: 16 kHz, 50 encoder frames/s."""
    import math
    frames = max(256, math.ceil(sample_count / 320) + 64)
    # Zero preserves normal 30-second windows for long/multi-window recordings.
    return frames if frames < 1500 else 0


def _transcribe_mp_worker(audio: Any, source: str = "meeting") -> str:
    """Ejecuta la transcripción en el proceso hijo usando el modelo global."""
    global _mp_model
    if _mp_model is None:
        raise RuntimeError("El modelo pywhispercpp no se inicializó correctamente en el worker.")
    
    try:
        import numpy as np
        audio_data = np.ascontiguousarray(audio, dtype=np.float32)
    except ImportError:
        audio_data = audio

    context = _interactive_audio_context(len(audio_data)) if source == "interactive" else 0
    # Model overrides persist: explicitly restore full context for other sources.
    text = " ".join(
        segment.text for segment in _mp_model.transcribe(audio_data, audio_ctx=context)
        if getattr(segment, "text", "").strip()
    ).strip()
    return text


def _dummy_mp_worker() -> None:
    """Tarea vacía utilizada para forzar la inicialización temprana del pool."""
    if _mp_model is None:
        raise RuntimeError("Whisper model initialization failed")


# ---------------------------------------------------------------------------
# Transcripción vía whisper-cli.exe (Vulkan GPU)
# ---------------------------------------------------------------------------

def _transcribe_via_cli(
    cli_exe: str,
    model_path: str,
    audio: Any,
    *,
    language: str = "es",
    n_threads: int = 4,
    stop_event: Optional[threading.Event] = None,
) -> str:
    """Run batch Whisper with bounded execution and cooperative cancellation."""
    fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="trace_asr_")
    os.close(fd)
    process = None
    try:
        _write_wav(wav_path, audio)
        cmd = [cli_exe, "-m", os.path.abspath(model_path), "-f", wav_path,
               "-l", language, "-t", str(n_threads), "--no-timestamps",
               "--no-prints", "-bs", "1", "-bo", "1", "-fa"]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=os.path.dirname(os.path.abspath(cli_exe)), creationflags=creationflags,
        )
        deadline = time.monotonic() + 120
        while True:
            if stop_event is not None and stop_event.is_set():
                raise RuntimeError("Transcription cancelled by shutdown")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Whisper CLI timed out")
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        stderr_out = stderr.decode("utf-8", errors="replace")
        if process.returncode != 0 and not stdout.strip():
            raise RuntimeError(
                f"Whisper CLI failed (rc={process.returncode}): {stderr_out[:300]}"
            )
        try:
            result = stdout.decode("utf-8").strip()
        except UnicodeDecodeError:
            result = stdout.decode("cp1252", errors="replace").strip()
        if stderr_out:
            logger.debug("Whisper CLI stderr: %s", stderr_out[:200])
        return result
    finally:
        try:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=1)
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Servicio principal
# ---------------------------------------------------------------------------

class TranscriptionService:
    """Una única instancia Whisper atendida por un worker priorizado.

    Si se proporciona *whisper_cli_exe*, la transcripción se realiza mediante
    el binario nativo compilado con soporte Vulkan (GPU).  En caso contrario,
    se utiliza pywhispercpp como fallback (CPU).
    """

    def __init__(
        self,
        model_path: str,
        *,
        n_threads: int = 4,
        language: str = "es",
        model: Any = None,
        whisper_cli_exe: Optional[str] = None,
    ):
        self.model_path = model_path
        self.n_threads = int(n_threads or 4)
        self.language = language
        self.whisper_cli_exe = whisper_cli_exe
        self.model = model

        self._queue: "queue.PriorityQueue[_Request]" = queue.PriorityQueue()
        self._sequence = 0
        self._sequence_lock = threading.Lock()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, daemon=True,
                                        name="trace-transcription")
        
        self._pool = None
        if self.model is not None:
            logger.info("[TRACE-ASR] Backend: modelo in-memory inyectado")
        elif not self.whisper_cli_exe:
            # Backend CPU (pywhispercpp aislado en proceso)
            import multiprocessing as mp
            import concurrent.futures
            # Usamos spawn para aislar el entorno (sin librerías previamente cargadas)
            ctx = mp.get_context("spawn")
            self._pool = concurrent.futures.ProcessPoolExecutor(
                max_workers=1,
                mp_context=ctx,
                initializer=_init_mp_worker,
                initargs=(self.model_path, self.n_threads, self.language)
            )
            # Forzar inicialización temprana enviando una tarea vacía (warm-up)
            self._warmup_future = self._pool.submit(_dummy_mp_worker)
            logger.info("[TRACE-ASR] Backend: pywhispercpp (CPU) en proceso aislado (warm-up encolado)")
        else:
            logger.info(
                "[TRACE-ASR] Backend: whisper-cli (Vulkan GPU warm) → %s",
                self.whisper_cli_exe,
            )

        self._worker.start()

    def wait_for_warmup(self, timeout: Optional[float] = None) -> bool:
        """Bloquea hasta que el proceso aislado haya inicializado Whisper."""
        if hasattr(self, "_warmup_future") and self._warmup_future is not None:
            try:
                self._warmup_future.result(timeout=timeout)
                return True
            except Exception:
                return False
        return True

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def submit(
        self,
        audio: Any,
        *,
        source: str = "meeting",
        priority: TranscriptionPriority = TranscriptionPriority.MEETING,
    ) -> "queue.Queue[TranscriptionResult]":
        """Encola audio y devuelve una cola con exactamente un resultado."""
        result_queue: "queue.Queue[TranscriptionResult]" = queue.Queue(maxsize=1)
        with self._sequence_lock:
            if self._stop.is_set():
                result_queue.put(TranscriptionResult("", source, error="Transcription service is closed"))
                return result_queue
            sequence = self._sequence
            self._sequence += 1
            self._queue.put(_Request(int(priority), sequence, audio, source,
                                     result_queue))
        return result_queue

    def transcribe(
        self,
        audio: Any,
        *,
        source: str = "interactive",
        priority: TranscriptionPriority = TranscriptionPriority.INTERACTIVE,
    ) -> TranscriptionResult:
        """API bloqueante para consumidores que necesitan el texto."""
        result_queue = self.submit(audio, source=source, priority=priority)
        try:
            return result_queue.get(timeout=120)
        except queue.Empty:
            return TranscriptionResult("", source, error="Transcription timed out")

    # ------------------------------------------------------------------
    # Worker interno
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                request = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                started = time.perf_counter()

                if self.model is not None:
                    # ── Backend directo (modelo inyectado / tests) ────
                    segments = self.model.transcribe(request.audio)
                    text = " ".join(
                        segment.text for segment in segments
                        if getattr(segment, "text", "").strip()
                    ).strip()
                    result = TranscriptionResult(
                        text, request.source,
                        duration_seconds=time.perf_counter() - started,
                    )
                elif self.whisper_cli_exe:
                    # ── Backend GPU (Vulkan) ──────────────────────────
                    try:
                        text = _transcribe_via_cli(
                            self.whisper_cli_exe,
                            self.model_path,
                            request.audio,
                            language=self.language,
                            n_threads=self.n_threads,
                            stop_event=self._stop,
                        )
                        elapsed = time.perf_counter() - started
                        logger.info(
                            "[TRACE-ASR] Transcripción GPU completada en %.2fs: %r",
                            elapsed, text[:80],
                        )
                        result = TranscriptionResult(
                            text, request.source,
                            duration_seconds=elapsed,
                        )
                    except Exception as exc:
                        logger.error(
                            "[TRACE-ASR] Error en backend CLI: %s", exc,
                            exc_info=True,
                        )
                        result = TranscriptionResult(
                            "", request.source, error=str(exc))
                else:
                    # ── Backend CPU (pywhispercpp aislado) ────────────
                    future = self._pool.submit(_transcribe_mp_worker, request.audio, request.source)
                    while True:
                        if self._stop.is_set():
                            future.cancel()
                            raise RuntimeError("Transcription cancelled by shutdown")
                        try:
                            text = future.result(timeout=0.1)
                            break
                        except TimeoutError:
                            continue
                    result = TranscriptionResult(
                        text, request.source,
                        duration_seconds=time.perf_counter() - started)

                request.future.put(result)
            except Exception as exc:
                logger.error("[TRACE-ASR] Error transcribiendo %s: %s",
                             request.source, exc, exc_info=True)
                request.future.put(TranscriptionResult(
                    "", request.source, error=str(exc)))
            finally:
                self._queue.task_done()

    def close(self) -> bool:
        with self._sequence_lock:
            self._stop.set()
            while True:
                try:
                    request = self._queue.get_nowait()
                except queue.Empty:
                    break
                request.future.put(TranscriptionResult("", request.source, error="Transcription service closed"))
                self._queue.task_done()
        if self._pool is not None:
            pool, self._pool = self._pool, None
            # Python 3.14 owns termination; retain bounded support for 3.11.
            terminate = getattr(pool, "terminate_workers", None)
            if callable(terminate):
                terminate()
            else:
                processes = list((getattr(pool, "_processes", None) or {}).values())
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                pool.shutdown(wait=False, cancel_futures=True)
                for process in processes:
                    process.join(timeout=1)
        if self._worker.is_alive():
            self._worker.join(timeout=3)
        return not self._worker.is_alive()

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()
