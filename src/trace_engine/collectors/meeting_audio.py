"""Recolector on-demand: audio de reuniones (Discord en canal de voz).

Fase C del timeline. Cuando la detección está activa, captura dos canales por
separado, los segmenta, filtra silencios y transcribe con whisper.cpp:

- audio_in  = voz del usuario (micrófono, 16kHz mono int16).
- audio_out = lo que dicen los demás (WASAPI Loopback del dispositivo de
  salida, estéreo→mono y resample a 16kHz por interpolación lineal — barato
  y suficiente para ASR; documentado, no es un resample de calidad musical).

Solo el TEXTO transcrito llega al timeline: el buffer de audio se descarta de
memoria tras transcribir (no se persiste audio en ningún lado).

DETECCIÓN (heurística ligera, documentada): Discord registra una sesión de
audio WASAPI siempre que corre, pero esa sesión solo pasa a State==Active
cuando hay audio de voz en curso (canal de voz). Entonces: "Discord en
llamada" ≡ existe proceso cuyo nombre empieza con alguna de
meeting_source_apps Y su sesión de audio está Active (pycaw). Limitación
conocida: Discord reproduciendo un video con sonido también activa la sesión
(falso positivo aceptable para MVP; el RMS gate descarta silencios igual).
Poll cada meeting_poll_seconds (default 5) en un hilo daemon: en idle cuesta
~0 CPU.

PUSH-TO-TALK: el micrófono puede estar ocupado por input_handler
(modules/input_handler.py, push-to-talk del asistente). Se prioriza el PTT:
set_ptt_active(True) pausa la captura de mic (se lee y descarta para no
desbordar el stream). El cableado input_handler → este método queda pendiente
(limitación conocida documentada; la API pública ya está).

WHISPER: instancia PROPIA y perezosa de pywhispercpp (NO se comparte la de
audio_core: colisiones de uso entre el asistente y el recolector). Coste: si
coinciden los dos usos, el modelo ggml está cargado 2 veces en RAM (~75MB con
tiny) — aceptable para MVP, documentado.

RECURSOS: idle (sin meeting) = solo el poll de detección (~0 CPU). En meeting:
2 streams de captura + una transcripción tiny por segmento de 8s en un hilo
dedicado (la captura nunca se bloquea por whisper).
"""

import logging
import queue
import threading
import time
from typing import List, Optional

import numpy as np

from trace_engine.storage.timeline_db import TimelineDB

FILTERED_MARKERS = frozenset({
    "[MÚSICA]", "[MUSIC]", "[BLANK_AUDIO]", "[SILENCIO]", "[SILENCE]",
    "[APPLAUSE]", "[APLAUSOS]", "[RISAS]", "[LAUGHTER]",
})

_FILTERED_UPPER = {m.upper() for m in FILTERED_MARKERS}

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000          # whisper trabaja a 16kHz mono
CHUNK_SECONDS = 0.5          # lectura por iteración por canal
MIN_FLUSH_SECONDS = 2.0      # al cerrar la meeting, segmentos más cortos se descartan
INT16_FULL_SCALE = 32768.0
ACTIVE_CONFIRM_POLLS = 2
INACTIVE_CONFIRM_POLLS = 3


# ---------------------------------------------------------------------------
# Lógica pura de audio (testeable sin hardware)
# ---------------------------------------------------------------------------

def int16_bytes_to_float32(pcm: bytes, channels: int = 1) -> np.ndarray:
    """PCM int16 interleaved → float32 mono normalizado [-1, 1].

    Con channels > 1 hace downmix por media de canales (estéreo→mono).
    """
    data = np.frombuffer(pcm, dtype=np.int16)
    if channels > 1:
        frames = len(data) // channels
        data = data[: frames * channels].reshape(frames, channels).mean(axis=1)
    return (data.astype(np.float32) / INT16_FULL_SCALE)


def rms(audio: np.ndarray) -> float:
    """RMS de un buffer float32. 0.0 si está vacío."""
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(audio ** 2)))


def resample_linear(audio: np.ndarray, src_rate: float, dst_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Remuestreo por interpolación lineal (barato; suficiente para ASR).

    El loopback WASAPI suele venir a 44.1/48kHz; whisper necesita 16kHz.
    Si src_rate == dst_rate devuelve el audio tal cual.
    """
    if audio.size == 0 or int(src_rate) == dst_rate:
        return audio
    duration = audio.size / float(src_rate)
    n_out = max(1, int(duration * dst_rate))
    x_old = np.linspace(0.0, duration, num=audio.size, endpoint=False)
    x_new = np.linspace(0.0, duration, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


# ---------------------------------------------------------------------------
# Recolector
# ---------------------------------------------------------------------------

class MeetingAudioCollector:
    """Captura y transcribe audio de meetings solo mientras hay una activa."""

    def __init__(self, db: TimelineDB, config=None):
        self.config = config or {}
        self.db = db
        self._external_transcriber = self.config.get("whisper_transcriber")
        self._transcription_service = self.config.get("transcription_service")
        self._metadata_provider = self.config.get("meeting_metadata_provider")
        self.poll_seconds = float(self.config.get("meeting_poll_seconds", 5))
        self.segment_seconds = float(self.config.get("meeting_segment_seconds", 8))
        self.rms_threshold = float(self.config.get("meeting_rms_threshold", 0.01))
        source_apps = self.config.get("meeting_source_apps", ["discord"])
        self.source_apps = [str(a).lower() for a in (source_apps or ["discord"])]

        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._capture_thread: Optional[threading.Thread] = None
        self._capture_stop = threading.Event()
        self._capture_paused = threading.Event()

        self._meeting_active = False
        self._active_polls = 0
        self._inactive_polls = 0
        self._meeting_session_id = None
        self._meeting_app = ""
        self._meeting_channel = ""
        self._meeting_participants = ""
        self._capture_disabled = False  # fallo de captura ya logueado esta sesión

        # Segmentos listos para transcribir: (source, audio_f32). Acotada: si
        # whisper va más lento que la captura, se descarta lo nuevo (mejor
        # perder un segmento que acumular RAM/lag).
        self._segments: "queue.Queue[tuple]" = queue.Queue(maxsize=8)
        self._transcribe_thread: Optional[threading.Thread] = None

        # Whisper propio y perezoso (ver docstring del módulo).
        self._model = None
        self._model_lock = threading.Lock()
        self._model_load_failed = False

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._stop_event.clear()
        self._transcribe_thread = threading.Thread(
            target=self._transcribe_loop, daemon=True, name="meeting-transcribe")
        self._transcribe_thread.start()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="meeting-detect")
        self._poll_thread.start()
        logger.info("✅ [MEETING] Detección de reuniones activa (poll cada %.0fs; apps: %s)",
                    self.poll_seconds, ", ".join(self.source_apps))

    def stop(self) -> None:
        self._stop_event.set()
        self._stop_capture()
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
            self._poll_thread = None
        if self._transcribe_thread:
            self._transcribe_thread.join(timeout=5)
            self._transcribe_thread = None
        self._close_meeting_session()
        logger.info("🛑 [MEETING] Recolector de reuniones detenido")

    def set_capture_paused(self, active: bool) -> None:
        """Pausa/reanuda la captura de mic mientras dura el push-to-talk."""
        if active:
            self._capture_paused.set()
        else:
            self._capture_paused.clear()

    # Alias de compatibilidad
    set_ptt_active = set_capture_paused

    # ------------------------------------------------------------- detección

    def _poll_loop(self) -> None:
        while not self._stop_event.wait(self.poll_seconds):
            try:
                state = self._detect_state()
            except Exception as e:
                logger.error("❌ [MEETING] Error en la detección: %s", e, exc_info=True)
                continue
            active = state["active"]
            if active:
                self._active_polls += 1
                self._inactive_polls = 0
            else:
                self._inactive_polls += 1
                self._active_polls = 0
            if active and not self._meeting_active and self._active_polls >= ACTIVE_CONFIRM_POLLS:
                self._meeting_active = True
                self._meeting_app = state["app_name"]
                self._meeting_channel = state["channel_name"]
                self._meeting_participants = state["participants"]
                self._meeting_session_id = self.db.start_meeting_session(
                    self._meeting_app, self._meeting_channel,
                    self._meeting_participants)
                logger.info("📞 [MEETING] Reunión detectada (canal de voz activo); "
                            "iniciando captura de audio")
                self._start_capture()
            elif (not active and self._meeting_active and
                  self._inactive_polls >= INACTIVE_CONFIRM_POLLS):
                self._meeting_active = False
                logger.info("📴 [MEETING] Reunión finalizada; deteniendo captura")
                self._stop_capture()
                self._close_meeting_session()

    def _detect_meeting(self) -> bool:
        """True si alguna app de meeting tiene una sesión de audio ACTIVA.

        Ver docstring del módulo para la heurística y sus limitaciones.
        """
        return self._detect_state()["active"]

    def _detect_state(self) -> dict:
        """Obtiene estado y metadatos confirmables por Windows.

        WASAPI/pycaw no exponen de forma fiable el canal ni los integrantes de
        Discord; esos campos permanecen vacíos hasta integrar una fuente
        explícita de Discord.
        """
        from pycaw.pycaw import AudioUtilities

        for session in AudioUtilities.GetAllSessions():
            if session.Process is None:
                continue
            name = session.Process.name().lower()
            if any(name.startswith(app) for app in self.source_apps):
                if getattr(session, "State", 0) != 1:
                    continue
                muted = False
                try:
                    volume = session.SimpleAudioVolume
                    muted = bool(volume.GetMute()) if volume is not None else False
                except Exception:
                    pass
                if muted:
                    return {"active": False, "app_name": name,
                            "channel_name": "", "participants": "", "deafened": True}
                metadata = self._meeting_metadata()
                return {"active": True, "app_name": name,
                        "channel_name": metadata["channel_name"],
                        "participants": metadata["participants"],
                        "deafened": False}
        return {"active": False, "app_name": "", "channel_name": "",
                "participants": "", "deafened": False}

    def _meeting_metadata(self) -> dict:
        """Obtiene metadatos opcionales sin acoplar TRACE a una app concreta."""
        if not callable(self._metadata_provider):
            return {"channel_name": "", "participants": ""}
        try:
            metadata = self._metadata_provider() or {}
            participants = metadata.get("participants", "")
            if isinstance(participants, (list, tuple, set)):
                participants = ", ".join(str(item) for item in participants)
            return {"channel_name": str(metadata.get("channel_name", "")),
                    "participants": str(participants or "")}
        except Exception as exc:
            logger.debug("[MEETING] Proveedor de metadatos no disponible: %s", exc)
        return {"channel_name": "", "participants": ""}

    def _close_meeting_session(self) -> None:
        if self._meeting_session_id is not None:
            self.db.end_meeting_session(self._meeting_session_id)
            self._meeting_session_id = None

    # --------------------------------------------------------------- captura

    def _start_capture(self) -> None:
        if self._capture_disabled:
            logger.warning("⚠️ [MEETING] Captura deshabilitada por error previo; "
                           "se reintenta en la próxima reunión")
            return
        self._capture_stop.clear()
        self._capture_thread = threading.Thread(
            target=self._capture_loop, daemon=True, name="meeting-capture")
        self._capture_thread.start()

    def _stop_capture(self) -> None:
        self._capture_stop.set()
        if self._capture_thread:
            self._capture_thread.join(timeout=5)
            self._capture_thread = None

    def _capture_loop(self) -> None:
        """Lee mic + loopback por chunks y arma segmentos por canal."""
        try:
            import pyaudiowpatch as pyaudio
        except ImportError as e:
            logger.error("❌ [MEETING] Falta PyAudioWPatch: %s. "
                         "Instalá requirements.txt para capturar audio de reuniones", e)
            self._capture_disabled = True
            return

        pa = None
        mic_stream = None
        loop_stream = None
        buffers = {"audio_in": [], "audio_out": []}
        loop_rate = None
        loop_channels = None

        try:
            pa = pyaudio.PyAudio()
            mic_stream = pa.open(format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE,
                                 input=True,
                                 frames_per_buffer=int(SAMPLE_RATE * CHUNK_SECONDS))
            try:
                loop_dev = pa.get_default_wasapi_loopback()
            except OSError as e:
                logger.warning("⚠️ [MEETING] Sin dispositivo loopback WASAPI (%s); "
                               "solo se captura el micrófono", e)
                loop_dev = None
            if loop_dev is not None:
                loop_rate = float(loop_dev["defaultSampleRate"])
                loop_channels = int(loop_dev["maxInputChannels"])
                loop_stream = pa.open(
                    format=pyaudio.paInt16,
                    channels=loop_channels,
                    rate=int(loop_rate),
                    input=True,
                    input_device_index=int(loop_dev["index"]),
                    frames_per_buffer=int(loop_rate * CHUNK_SECONDS))
                logger.info("🎧 [MEETING] Loopback: %s (%.0fHz, %dch)",
                            loop_dev["name"], loop_rate, loop_channels)
            logger.info("🎙️ [MEETING] Captura activa (mic 16kHz%s)",
                        " + loopback" if loop_stream else "")

            mic_frames = int(SAMPLE_RATE * CHUNK_SECONDS)
            loop_frames = int(loop_rate * CHUNK_SECONDS) if loop_rate else 0
            while not self._capture_stop.is_set():
                if mic_stream is not None:
                    pcm = mic_stream.read(mic_frames, exception_on_overflow=False)
                    if self._capture_paused.is_set():
                        # PTT tiene prioridad: el mic es del asistente ahora.
                        buffers["audio_in"] = []
                    else:
                        self._append_chunk(buffers, "audio_in", pcm, 1, SAMPLE_RATE)
                if loop_stream is not None:
                    pcm = loop_stream.read(loop_frames, exception_on_overflow=False)
                    self._append_chunk(buffers, "audio_out", pcm,
                                       loop_channels, loop_rate)
        except Exception as e:
            logger.error("❌ [MEETING] Error en la captura: %s", e, exc_info=True)
            self._capture_disabled = True
        finally:
            # Flush: lo que quedó en los buffers se evalúa como segmento final.
            for source, chunks in buffers.items():
                self._flush_segment(source, chunks, final=True)
            for stream in (mic_stream, loop_stream):
                if stream is not None:
                    try:
                        stream.stop_stream()
                        stream.close()
                    except Exception as e:
                        logger.warning("⚠️ [MEETING] Error cerrando stream: %s", e)
            if pa is not None:
                pa.terminate()
            logger.info("🛑 [MEETING] Captura detenida")

    def _append_chunk(self, buffers: dict, source: str, pcm: bytes,
                      channels: int, src_rate: float) -> None:
        """Acumula un chunk (float32 mono 16kHz) y corta segmento si toca."""
        audio = int16_bytes_to_float32(pcm, channels)
        audio = resample_linear(audio, src_rate)
        buffers[source].append(audio)
        total = sum(len(c) for c in buffers[source])
        if total >= int(self.segment_seconds * SAMPLE_RATE):
            self._flush_segment(source, buffers[source])

    def _flush_segment(self, source: str, chunks: List[np.ndarray],
                       final: bool = False) -> None:
        """RMS gate y encolado del segmento para transcribir (o descarte)."""
        if not chunks:
            return
        audio = np.concatenate(chunks)
        chunks.clear()
        seconds = len(audio) / SAMPLE_RATE
        if final and seconds < MIN_FLUSH_SECONDS:
            logger.debug("[MEETING] Segmento final muy corto (%.1fs), descartado", seconds)
            return
        level = rms(audio)
        if level < self.rms_threshold:
            logger.debug("[MEETING] Segmento %s descartado por silencio "
                         "(RMS %.4f < %.4f)", source, level, self.rms_threshold)
            return
        try:
            self._segments.put_nowait((source, audio, seconds))
        except queue.Full:
            logger.warning("⚠️ [MEETING] Whisper saturado; segmento %s de %.1fs "
                           "descartado (cola de transcripción llena)", source, seconds)

    # ----------------------------------------------------------- transcripción

    def _get_model(self):
        """Whisper propio, carga perezosa thread-safe. None si falla.

        NO compartir con audio_core (ver docstring del módulo).
        """
        if self._model is not None or self._model_load_failed:
            return self._model
        with self._model_lock:
            if self._model is not None or self._model_load_failed:
                return self._model
            try:
                from pywhispercpp.model import Model

                model_path = self.config.get("whisper_model_path")
                if not model_path:
                    raise ValueError("TRACE necesita config['whisper_model_path'] para reuniones")
                n_threads = int(self.config.get("whisper_threads", 4) or 4)
                self._model = Model(
                    model_path,
                    n_threads=n_threads,
                    language="es",
                    print_realtime=False,
                    print_progress=False,
                    print_timestamps=False,
                )
                logger.info("✅ [MEETING] Modelo whisper propio cargado: %s", model_path)
            except Exception as e:
                logger.error("❌ [MEETING] Error cargando whisper: %s", e, exc_info=True)
                self._model_load_failed = True
        return self._model

    def _transcribe_loop(self) -> None:
        """Consume segmentos, transcribe e inserta en el timeline (texto solo)."""
        while not self._stop_event.is_set():
            try:
                source, audio, seconds = self._segments.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                audio = np.ascontiguousarray(audio, dtype=np.float32)
                if self._transcription_service is not None:
                    from trace_engine.transcription import TranscriptionPriority
                    result_queue = self._transcription_service.submit(
                        audio, source="meeting",
                        priority=TranscriptionPriority.MEETING)
                    result = result_queue.get()
                    text = getattr(result, "text", "")
                elif self._external_transcriber is not None:
                    result = self._external_transcriber.transcribe(audio)
                    text = " ".join(
                        seg.text if hasattr(seg, "text") else str(seg)
                        for seg in result
                        if (seg.text if hasattr(seg, "text") else str(seg)).strip().upper()
                        not in _FILTERED_UPPER
                    ).strip()
                else:
                    model = self._get_model()
                    if model is None:
                        continue
                    result = model.transcribe(audio)
                    text = " ".join(
                        seg.text if hasattr(seg, "text") else str(seg)
                        for seg in result
                        if (seg.text if hasattr(seg, "text") else str(seg)).strip().upper()
                        not in _FILTERED_UPPER
                    ).strip()
                # El buffer se descarta acá: solo el texto sigue adelante.
                audio = None
                if not text or text.upper() in _FILTERED_UPPER:
                    continue
                canal = "mic" if source == "audio_in" else "loopback"
                if self.db.insert(source, "Discord", "voice channel", text):
                    logger.info("📝 [MEETING] %s transcrito (%.1fs): '%s'",
                                canal, seconds, text[:80])
            except Exception as e:
                logger.error("❌ [MEETING] Error transcribiendo segmento: %s", e,
                             exc_info=True)
            finally:
                self._segments.task_done()
