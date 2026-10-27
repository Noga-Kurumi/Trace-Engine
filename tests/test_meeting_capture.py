"""Meeting lifecycle and provenance: real SQLite, synthetic PCM, no devices."""
from dataclasses import replace
from pathlib import Path
import queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import gc
import weakref
import numpy as np

from trace_engine.collectors.meeting_audio import MeetingAudioCollector,MeetingSegment,CaptureSession
from trace_engine.speech_activity import SpeechSegmenter,RATE
from trace_engine.storage.timeline_db import TimelineDB
from trace_engine.meeting_context import session_state


class MeetingCaptureTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.db=TimelineDB(str(Path(self.folder.name)/'timeline.db'));self.addCleanup(self.db.close)
        self.start=time.time()-30
        self.first=self.db.start_meeting_session('zoom.exe','Call A',started_at=self.start)

    def segment(self,**kw):
        defaults=dict(session_id=self.first,source='audio_out',sequence=1,started_at=self.start+1,
            ended_at=self.start+2,audio=np.full(RATE,.2,dtype=np.float32),capture_scope='synthetic')
        defaults.update(kw);return MeetingSegment(**defaults)

    def test_late_result_is_committed_to_closed_original_call_not_new_call(self):
        self.db.end_meeting_session(self.first,self.start+10)
        second=self.db.start_meeting_session('ms-teams.exe','Call B',started_at=self.start+11)
        self.assertTrue(self.db.record_meeting_segment(self.segment(),'Resultado original.'))
        row=self.db._conn.execute('SELECT session_id FROM meeting_segments').fetchone()
        self.assertEqual(row[0],self.first);self.assertNotEqual(row[0],second)
        event=self.db.get_by_time_range(0,time.time()+100)[0]
        self.assertEqual(event[1],self.start+1);self.assertEqual(event[3],'zoom.exe');self.assertEqual(event[4],'Call A')

    def test_segment_identity_is_immutable_and_idempotent(self):
        job=self.segment()
        with self.assertRaises(AttributeError):job.session_id=999
        self.assertTrue(self.db.record_meeting_segment(job,'Dato.'))
        self.assertTrue(self.db.record_meeting_segment(job,'Dato.'))
        self.assertEqual(self.db._conn.execute('SELECT COUNT(*) FROM timeline').fetchone()[0],1)

    def test_deleted_session_never_resurrects_pending_audio(self):
        job=self.segment();self.db.clear_all_records()
        self.assertFalse(self.db.record_meeting_segment(job,'No reaparecer.'))
        self.assertFalse(self.db.get_by_time_range(0,time.time()+100))

    def test_out_of_session_capture_interval_is_rejected(self):
        self.db.end_meeting_session(self.first,self.start+5)
        self.assertFalse(self.db.record_meeting_segment(self.segment(started_at=self.start+8,ended_at=self.start+9),'Fuera.'))

    def test_drain_status_waits_for_pending_work(self):
        self.db.update_meeting_state(self.first,pending_delta=1)
        self.db.update_meeting_state(self.first,status='draining')
        self.assertEqual(session_state(self.db._conn,self.first)['status'],'draining')
        self.db.record_meeting_segment(self.segment(),'Dato.')
        self.db.update_meeting_state(self.first,pending_delta=-1)
        self.assertEqual(session_state(self.db._conn,self.first)['status'],'finished')

    def test_backpressure_is_bounded_and_persists_a_gap(self):
        collector=MeetingAudioCollector(self.db,{'meeting_buffer_seconds':8})
        large=np.zeros(RATE*8,dtype=np.float32)
        self.assertTrue(collector._enqueue(self.segment(audio=large,ended_at=self.start+9)))
        self.assertFalse(collector._enqueue(self.segment(sequence=2)))
        state=session_state(self.db._conn,self.first)
        self.assertEqual(state['status'],'partial');self.assertEqual(state['gap_count'],1)
        self.assertEqual(collector._queued_samples,RATE*8)

    def test_stop_interrupts_waiting_for_shared_asr_without_persisting_late_text(self):
        entered=threading.Event()
        class Service:
            def submit(self,*a,**kw):entered.set();return queue.Queue()
        collector=MeetingAudioCollector(self.db,{'transcription_service':Service()})
        collector._enqueue(self.segment())
        worker=threading.Thread(target=collector._transcribe_loop);collector._transcribe_thread=worker;worker.start()
        self.assertTrue(entered.wait(2));then=time.monotonic();self.assertTrue(collector.stop())
        self.assertLess(time.monotonic()-then,.8)
        self.assertFalse(worker.is_alive());self.assertFalse(self.db.get_by_time_range(0,time.time()+100))

    def test_shared_service_priority_and_transcript_survive_speaker_backend_failure(self):
        class Service:
            priorities=[]
            def submit(self,*a,**kw):
                self.priorities.append(kw['priority']);result=queue.Queue()
                result.put(SimpleNamespace(text='Acuerdo confirmado.',error=None));return result
        class BrokenSpeaker:
            def assign(self,*a):raise RuntimeError('embedding unavailable')
        service=Service();collector=MeetingAudioCollector(self.db,{'transcription_service':service})
        collector._enqueue(self.segment(speaker_tracker=BrokenSpeaker()))
        worker=threading.Thread(target=collector._transcribe_loop);worker.start()
        deadline=time.monotonic()+2
        while collector._segments.unfinished_tasks and time.monotonic()<deadline:time.sleep(.01)
        collector._stop_event.set();worker.join(2)
        self.assertEqual(int(service.priorities[0]),10)
        self.assertEqual(self.db.get_by_time_range(0,time.time()+100)[0][5],'Acuerdo confirmado.')

    def test_pause_reasons_do_not_release_each_other(self):
        collector=MeetingAudioCollector(self.db,{})
        collector.set_capture_paused(True);collector.set_playback_active(True)
        collector.set_capture_paused(False);self.assertTrue(collector._capture_paused.is_set())
        collector.set_playback_active(False);self.assertFalse(collector._capture_paused.is_set())

    def test_capture_error_generation_is_recreated_on_retry(self):
        def fail(*a):raise OSError('device removed')
        collector=MeetingAudioCollector(self.db,{'transcription_service':object(),'meeting_capture_factory':fail})
        candidate={'active':True,'pid':123,'app_name':'zoom.exe','process_created':10,'capture_session_key':'x'}
        collector._apply_detection(candidate)
        for thread in collector._capture_threads:thread.join(2)
        first=collector._session.session_id
        collector._retry_after=0;collector._apply_detection(candidate);collector._apply_detection(candidate)
        self.assertNotEqual(collector._session.session_id,first)
        self.assertTrue(collector.stop())

    def test_speaker_model_load_does_not_block_opening_capture(self):
        factory=Mock(side_effect=RuntimeError('speaker model absent'))
        collector=MeetingAudioCollector(self.db,{'transcription_service':object(),
            'meeting_capture_factory':object(),'meeting_speaker_factory':factory})
        collector._start_capture=Mock()
        candidate={'pid':123,'app_name':'zoom.exe'}
        collector._begin_session(candidate,(123,1,'s'))
        factory.assert_not_called();collector._start_capture.assert_called_once()
        tracker=collector._session.speaker_tracker
        self.assertEqual(tracker.assign(np.ones(RATE*2,np.float32)),'remote_unknown')
        self.assertEqual(tracker.assign(np.ones(RATE*2,np.float32)),'remote_unknown')
        factory.assert_called_once();collector.stop()

    def test_idle_worker_releases_the_completed_jobs_speaker_model(self):
        class Tracker:
            def assign(self,audio):return 'speaker_1'
        class Service:
            def submit(self,*a,**kw):
                result=queue.Queue();result.put(SimpleNamespace(text='Dato.',error=None));return result
        tracker=Tracker();reference=weakref.ref(tracker)
        collector=MeetingAudioCollector(self.db,{'transcription_service':Service()})
        collector._enqueue(self.segment(speaker_tracker=tracker));del tracker
        worker=threading.Thread(target=collector._transcribe_loop)
        collector._transcribe_thread=worker;worker.start()
        deadline=time.monotonic()+2
        while collector._segments.unfinished_tasks and time.monotonic()<deadline:time.sleep(.01)
        gc.collect();self.assertIsNone(reference())
        self.assertTrue(worker.is_alive());self.assertTrue(collector.stop())


class SpeechEndpointTests(unittest.TestCase):
    def test_short_final_word_is_not_discarded(self):
        segmenter=SpeechSegmenter(lambda frame:1.)
        audio=np.full(6400,.2,dtype=np.float32)
        self.assertEqual(segmenter.feed(audio,100),[])
        parts=segmenter.finish();self.assertEqual(len(parts),1)
        np.testing.assert_array_equal(parts[0].audio,audio)
        self.assertAlmostEqual(parts[0].ended_at,100.4)

    def test_silence_does_not_reach_asr(self):
        segmenter=SpeechSegmenter(lambda frame:0.)
        self.assertEqual(segmenter.feed(np.zeros(RATE),100),[]);self.assertEqual(segmenter.finish(),[])

    def test_speech_is_emitted_on_pause_without_waiting_eight_seconds(self):
        segmenter=SpeechSegmenter(lambda frame:float(np.max(frame)>.01))
        result=segmenter.feed(np.r_[np.full(4000,.2),np.zeros(6400)].astype(np.float32),100)
        self.assertEqual(len(result),1);self.assertLess(result[0].ended_at-100,1)

    def test_forced_long_segments_have_explicit_overlap_and_bounded_audio(self):
        segmenter=SpeechSegmenter(lambda frame:1.,max_seconds=1)
        parts=[]
        for n in range(4):parts+=segmenter.feed(np.full(RATE,.2,dtype=np.float32),100+n)
        parts+=segmenter.finish()
        self.assertGreater(len(parts),2)
        self.assertTrue(all(len(p.audio)<=2*RATE for p in parts))
        self.assertGreater(parts[1].overlap_seconds,0)

    def test_nearby_pause_prevents_a_boundary_only_extra_fragment(self):
        segmenter=SpeechSegmenter(lambda x:float(np.max(x)>.01),max_seconds=1)
        audio=np.r_[np.full(19200,.2),np.zeros(6400)].astype(np.float32)
        parts=segmenter.feed(audio[:16000],100)+segmenter.feed(audio[16000:],101)+segmenter.finish()
        self.assertEqual(len(parts),1)
        self.assertEqual(parts[0].overlap_seconds,0)
        self.assertGreater(len(parts[0].audio),RATE)
