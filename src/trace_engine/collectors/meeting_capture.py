"""Bounded meeting capture with immutable provenance and the shared STT service."""
from dataclasses import dataclass,field,replace
import logging
import queue
import threading
import time
import numpy as np
from trace_engine.speech_activity import SpeechSegmenter,RATE

logger=logging.getLogger(__name__)
SAMPLE_RATE=RATE
INT16_FULL_SCALE=32768.
FILTERED_MARKERS=frozenset({'[MÚSICA]','[MUSIC]','[BLANK_AUDIO]','[SILENCIO]','[SILENCE]',
                          '[APPLAUSE]','[APLAUSOS]','[RISAS]','[LAUGHTER]'})


def int16_bytes_to_float32(pcm,channels=1):
    data=np.frombuffer(pcm,dtype=np.int16)
    if channels>1:data=data[:len(data)//channels*channels].reshape(-1,channels).mean(axis=1)
    return data.astype(np.float32)/INT16_FULL_SCALE


def rms(audio):return float(np.sqrt(np.mean(audio**2))) if audio.size else 0.


def resample_linear(audio,src_rate,dst_rate=RATE):
    if not len(audio) or int(src_rate)==dst_rate:return audio
    return np.interp(np.arange(int(len(audio)*dst_rate/src_rate))*src_rate/dst_rate,
                     np.arange(len(audio)),audio).astype(np.float32)


@dataclass(frozen=True)
class MeetingSegment:
    session_id:int
    source:str
    sequence:int
    started_at:float
    ended_at:float
    audio:np.ndarray=field(compare=False,repr=False)
    capture_scope:str='application_tree'
    speaker_id:str='remote_unknown'
    overlap_seconds:float=0
    speaker_tracker:object=field(default=None,compare=False,repr=False)


@dataclass(frozen=True)
class CaptureSession:
    session_id:int
    app_name:str
    pid:int
    key:tuple
    started_at:float
    microphone_device:str=''
    stop:threading.Event=field(default_factory=threading.Event,compare=False)
    sequence:dict=field(default_factory=dict,compare=False)
    vad:object=field(default=None,compare=False,repr=False)
    speaker_tracker:object=field(default=None,compare=False,repr=False)


class _DeferredSpeakers:
    """Opening capture must never wait for a speaker model to load."""
    def __init__(self,factory):
        self.factory=factory;self.tracker=None;self.failed=False

    def assign(self,audio):
        if self.failed:return 'remote_unknown'
        if self.tracker is None:
            try:self.tracker=self.factory()
            except Exception:
                self.failed=True
                logger.exception('Anonymous speaker model unavailable')
                return 'remote_unknown'
        return self.tracker.assign(audio)


class MeetingAudioCollector:
    def __init__(self,db,config=None):
        self.db=db;self.config=config or {}
        self._transcription_service=self.config.get('transcription_service')
        self._detector=self.config.get('meeting_detector')
        self._capture_factory=self.config.get('meeting_capture_factory')
        self._vad_factory=self.config.get('meeting_vad_factory')
        self._speaker_factory=self.config.get('meeting_speaker_factory')
        self.poll_seconds=max(.2,float(self.config.get('meeting_poll_seconds',1)))
        self.segment_seconds=max(1,min(12,float(self.config.get('meeting_segment_seconds',12))))
        self.silence_seconds=max(.25,min(1.,float(self.config.get('meeting_silence_seconds',.6))))
        self.rms_threshold=float(self.config.get('meeting_rms_threshold',.003))
        self.source_apps=list(self.config.get('meeting_source_apps',()) or ())
        self._stop_event=threading.Event();self._capture_paused=threading.Event()
        self._pause_reasons=set();self._pause_lock=threading.Lock()
        self._lifecycle=threading.RLock();self._queue_lock=threading.Lock()
        self._segments=queue.Queue(maxsize=32);self._queued_samples=0
        self._max_samples=int(max(8,min(120,float(self.config.get('meeting_buffer_seconds',30))))*RATE)
        self._poll_thread=None;self._transcribe_thread=None;self._capture_threads=[]
        self._session=None;self._meeting_active=False;self._capture_disabled=False
        self._last_active=0.;self._retry_after=0.
        self._prepared_vad=None

    def _state(self,session_id,**values):
        try:return self.db.update_meeting_state(session_id,**values)
        except Exception:
            logger.debug('Meeting diagnostics unavailable during shutdown',exc_info=True)
            return False

    def start(self):
        if self._poll_thread and self._poll_thread.is_alive():return
        self._stop_event.clear()
        self._transcribe_thread=threading.Thread(target=self._transcribe_loop,name='meeting-asr',daemon=True)
        self._poll_thread=threading.Thread(target=self._poll_loop,name='meeting-detect',daemon=True)
        self._transcribe_thread.start();self._poll_thread.start()

    def stop(self):
        self._stop_event.set()
        with self._lifecycle:self._end_session()
        for thread in (self._poll_thread,self._transcribe_thread):
            if thread and thread is not threading.current_thread():thread.join(timeout=3.5)
        stopped=all(not t or not t.is_alive() for t in
                    (self._poll_thread,self._transcribe_thread,*self._capture_threads))
        if stopped:self._prepared_vad=None
        return stopped

    def set_capture_paused(self,active,reason='push_to_talk'):
        with self._pause_lock:
            if active:self._pause_reasons.add(reason)
            else:self._pause_reasons.discard(reason)
            self._capture_paused.set() if self._pause_reasons else self._capture_paused.clear()

    set_ptt_active=set_capture_paused

    def set_playback_active(self,active):self.set_capture_paused(active,'assistant_playback')

    def _detect_state(self):
        return self._detector() if callable(self._detector) else {'active':False,'reason':'meeting_adapter_unavailable'}

    def _detect_meeting(self):return bool(self._detect_state().get('active'))

    def _poll_loop(self):
        # Prepare the tiny detector on this background thread, before calls
        # arrive, instead of blocking capture at the start of every meeting.
        if self._vad_factory:
            try:self._prepared_vad=self._vad_factory()
            except Exception:logger.exception('Meeting VAD preparation failed')
        while not self._stop_event.is_set():
            try:
                candidate=self._detect_state()
                with self._lifecycle:
                    if self._stop_event.is_set():break
                    self._apply_detection(candidate)
            except Exception:logger.exception('Meeting detection failed')
            self._stop_event.wait(self.poll_seconds)

    def _apply_detection(self,candidate):
        if candidate.get('active'):
            self.set_capture_paused(not candidate.get('microphone_active',True),'call_microphone_inactive')
            key=(candidate.get('pid'),candidate.get('process_created'),candidate.get('capture_session_key'))
            self._last_active=time.monotonic()
            if self._session and self._session.key!=key:self._end_session()
            if self._session is None and time.monotonic()>=self._retry_after:self._begin_session(candidate,key)
        elif self._session and time.monotonic()-self._last_active>=float(self.config.get('meeting_end_grace_seconds',5)):
            self._end_session()
        if self._session and self._capture_disabled and time.monotonic()>=self._retry_after:self._end_session()

    def _begin_session(self,candidate,key):
        if not self._transcription_service or not self._capture_factory or any(t.is_alive() for t in self._capture_threads):return
        pid=candidate.get('pid')
        if type(pid) is not int or pid<=0 or not candidate.get('app_name'):return
        start=time.time()
        identifier=self.db.start_meeting_session(candidate['app_name'],candidate.get('channel_name',''),
            candidate.get('participants',''),started_at=start)
        if not identifier:return
        vad=self._prepared_vad;tracker=None
        if self._vad_factory and vad is None:
            try:vad=self._vad_factory()
            except Exception:
                logger.exception('Meeting VAD unavailable; using explicit energy fallback')
                self._state(identifier,reason='vad_unavailable')
        if self._speaker_factory:
            tracker=_DeferredSpeakers(self._speaker_factory)
        self._session=CaptureSession(identifier,candidate['app_name'],pid,key,start,
            candidate.get('microphone_device',''),vad=vad,speaker_tracker=tracker)
        self._meeting_active=True;self._capture_disabled=False
        self._state(identifier,status='recording');self._start_capture()

    def _start_capture(self):
        if not self._session:return
        self._capture_disabled=False
        self._capture_threads=[threading.Thread(target=self._capture_source,args=(source,self._session),
            name='meeting-'+source,daemon=True) for source in ('audio_in','audio_out')]
        for thread in self._capture_threads:thread.start()

    def _end_session(self):
        session,self._session=self._session,None
        if not session:return
        session.stop.set()
        for thread in self._capture_threads:
            if thread is not threading.current_thread():thread.join(timeout=3.5)
        self._capture_threads=[t for t in self._capture_threads if t.is_alive()]
        self._meeting_active=False
        if self._capture_threads:self._state(session.session_id,reason='capture_stop_timeout')
        self.db.end_meeting_session(session.session_id,time.time())
        self._state(session.session_id,status='draining')

    def _capture_source(self,source,session):
        stream=None;last_position=None;segmenter=None;paused=False
        try:
            stream=self._capture_factory(source,session)
            detector=session.vad.stream() if session.vad else lambda x:float(rms(x)>=self.rms_threshold)
            segmenter=self._segmenter(detector)
            wall_offset=time.time()-time.perf_counter()
            while not session.stop.is_set() and not self._stop_event.is_set():
                packet=stream.read(100)
                if packet is None:continue
                audio,position,qpc,flags=packet
                begin=wall_offset+qpc/1e7 if qpc and not flags&4 else time.time()-len(audio)/RATE
                begin=max(session.started_at,begin)
                if last_position is not None and (flags&1 or position!=last_position):
                    for chunk in segmenter.finish():self._queue_chunk(session,source,chunk)
                    self._state(session.session_id,reason='capture_discontinuity',dropped_seconds=max(0,(position-last_position)/RATE))
                    detector=session.vad.stream() if session.vad else lambda x:float(rms(x)>=self.rms_threshold)
                    segmenter=self._segmenter(detector)
                last_position=position+len(audio)
                if source=='audio_in' and self._capture_paused.is_set():
                    if not paused:
                        for chunk in segmenter.finish():self._queue_chunk(session,source,chunk)
                        with self._pause_lock:
                            reserved=bool(self._pause_reasons-{'call_microphone_inactive'})
                        if reserved:self._state(session.session_id,reason='microphone_reserved_for_assistant')
                        paused=True
                    segmenter=self._segmenter(detector)
                    continue
                if paused:
                    detector=session.vad.stream() if session.vad else lambda x:float(rms(x)>=self.rms_threshold)
                    segmenter=self._segmenter(detector)
                paused=False
                for chunk in segmenter.feed(audio,begin):self._queue_chunk(session,source,chunk)
        except Exception:
            logger.exception('Isolated meeting capture failed: %s',source)
            self._state(session.session_id,reason='capture_unavailable')
            self._capture_disabled=True;self._retry_after=time.monotonic()+5
        finally:
            if segmenter:
                for chunk in segmenter.finish():self._queue_chunk(session,source,chunk)
            if stream:
                try:stream.close()
                except Exception:logger.exception('Meeting capture close failed')

    def _segmenter(self,detector):
        return SpeechSegmenter(detector,max_seconds=self.segment_seconds,silence_seconds=self.silence_seconds)

    def _queue_chunk(self,session,source,chunk):
        sequence=session.sequence.get(source,0)+1;session.sequence[source]=sequence
        return self._enqueue(MeetingSegment(session.session_id,source,sequence,chunk.started_at,chunk.ended_at,chunk.audio,
            capture_scope='microphone' if source=='audio_in' else 'application_tree',
            speaker_id='microphone' if source=='audio_in' else 'remote_unknown',
            overlap_seconds=chunk.overlap_seconds,speaker_tracker=session.speaker_tracker))

    def _enqueue(self,segment):
        size=len(segment.audio)
        with self._queue_lock:
            if self._segments.full() or self._queued_samples+size>self._max_samples:
                self._state(segment.session_id,reason='backpressure_limit',dropped_seconds=size/RATE);return False
            self._queued_samples+=size
            self._state(segment.session_id,pending_delta=1)
            self._segments.put_nowait(segment)
        return True

    def _transcribe_loop(self):
        from trace_engine.transcription import TranscriptionPriority
        while not self._stop_event.is_set() or not self._segments.empty():
            try:segment=self._segments.get(timeout=.1)
            except queue.Empty:continue
            with self._queue_lock:self._queued_samples-=len(segment.audio)
            try:
                if self._stop_event.is_set():raise InterruptedError('Meeting collection stopped')
                future=self._transcription_service.submit(segment.audio,source='meeting',priority=TranscriptionPriority.MEETING)
                deadline=time.monotonic()+float(self.config.get('meeting_asr_timeout_seconds',120))
                while True:
                    if self._stop_event.is_set():raise InterruptedError('Meeting collection stopped')
                    try:result=future.get(timeout=.1);break
                    except queue.Empty:
                        if time.monotonic()>=deadline:raise TimeoutError('Meeting ASR deadline exceeded')
                if getattr(result,'error',None):raise RuntimeError('Meeting ASR failed')
                text=getattr(result,'text','')
                if not isinstance(text,str) or not text.strip() or text.strip().upper() in FILTERED_MARKERS:
                    self._state(segment.session_id,reason='asr_no_text',dropped_seconds=len(segment.audio)/RATE);continue
                if segment.source=='audio_out' and segment.speaker_tracker:
                    try:segment=replace(segment,speaker_id=segment.speaker_tracker.assign(segment.audio))
                    except Exception:logger.debug('Speaker ID unavailable; preserving transcript',exc_info=True)
                if not self.db.record_meeting_segment(segment,text):
                    logger.info('Discarded meeting result whose session was deleted or excluded')
            except Exception as error:
                reason='collection_stopped' if isinstance(error,InterruptedError) else 'asr_timeout' if isinstance(error,TimeoutError) else 'asr_error'
                self._state(segment.session_id,reason=reason,dropped_seconds=len(segment.audio)/RATE)
                logger.debug('Meeting segment not committed: %s',reason)
            finally:
                self._state(segment.session_id,pending_delta=-1)
                self._segments.task_done()
                del segment  # release PCM/model references while the queue is idle
