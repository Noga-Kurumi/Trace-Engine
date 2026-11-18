"""Exercise the public archive and deferred capture APIs without a desktop host."""
from contextlib import closing
import queue
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

import numpy as np

from trace_engine.call_archive import CallArchive
from trace_engine.call_index import CallIndex
from trace_engine.collectors.meeting_audio import MeetingAudioCollector
from trace_engine.storage.timeline_db import TimelineDB
from trace_engine.visual_archive import VisualArchive


def fixture_seal(payload):
    return b'fixture:' + payload[::-1]


def fixture_open(payload):
    if not payload.startswith(b'fixture:'):
        raise ValueError('Invalid test payload')
    return payload[8:][::-1]


class EncryptedArchiveTests(unittest.TestCase):
    def test_visual_round_trip_and_retention(self):
        with tempfile.TemporaryDirectory() as folder, closing(TimelineDB(Path(folder) / 'trace.db')) as db:
            archive = VisualArchive(db.db_path, seal=fixture_seal, decrypt=fixture_open)
            stamp = time.time()
            session = db.start_window_session('Editor.exe', 'Fixture', stamp - 1)
            image = b'\xff\xd8test fixture\xff\xd9'
            self.assertTrue(archive.record(session, stamp, image))
            db.end_window_session(session, stamp + 1)
            snapshot, = archive.select(stamp - 1, stamp + 1)
            self.assertEqual(snapshot.title, 'Fixture')
            self.assertEqual(archive.read(snapshot), image)
            self.assertEqual([p.read_bytes() for p in archive.root.iterdir()], [fixture_seal(image)])
            archive.prune(stamp + 2)
            self.assertEqual(archive.select(stamp - 1, stamp + 1), [])

    def test_meeting_sink_flushes_and_index_respects_host_admission(self):
        with tempfile.TemporaryDirectory() as folder, closing(TimelineDB(Path(folder) / 'trace.db')) as db:
            archive = CallArchive(db.db_path, seal=fixture_seal, decrypt=fixture_open, opus=lambda: None)
            archive.initialize()
            stamp = time.time()
            captured = queue.Queue()
            packet_processed = threading.Event()
            state = {'active': True, 'pid': 77, 'app_name': 'Conference.exe'}

            # Native packets are replaced by an explicitly supplied capture backend.
            class Stream:
                def __init__(self, source):
                    self.source = source
                    self.sent = False

                def read(self, timeout_ms):
                    if self.sent:
                        packet_processed.set()
                    if self.sent or self.source == 'audio_in':
                        time.sleep(.01)
                        return None
                    self.sent = True
                    return np.full(2048, .4, dtype=np.float32), 0, 0, 0

                def close(self):
                    pass

            def save(segment):
                if archive.record(segment):
                    captured.put(segment)

            collector = MeetingAudioCollector(db, config={
                'meeting_detector': lambda: state,
                'meeting_capture_factory': lambda source, session: Stream(source),
                'meeting_poll_seconds': .05,
            }, segment_sink=save, accept_app=lambda name: name == 'Conference.exe')
            collector.start()
            try:
                self.assertTrue(packet_processed.wait(2))
            finally:
                self.assertTrue(collector.stop())
            captured.get(timeout=1)
            clip, = archive.select(stamp - 1, time.time() + 1)[0]
            self.assertFalse(clip.indexed)

            calls = []

            def submit(audio, **options):
                calls.append(options)
                future = queue.Queue()
                future.put(SimpleNamespace(text='Laura confirmó 7834'))
                return future

            service = SimpleNamespace(submit=submit)
            admit = False
            index = CallIndex(archive, lambda: service, lambda _, background: admit,
                              recent_seconds=.1)
            self.assertFalse(index.index_one(now=time.time() + 1))
            self.assertEqual(calls, [])
            admit = True
            self.assertTrue(index.index_one(now=time.time() + 1))
            self.assertEqual(archive.transcript(clip), 'Laura confirmó 7834')
            self.assertEqual(archive.select(stamp - 1, time.time() + 1, topic='7834')[0][0].id, clip.id)
            self.assertEqual(len(calls), 1)
