"""Deterministic monitor/focus races and atomic provenance, no personal capture."""
import asyncio
from datetime import datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from trace_engine.collectors import screen_ocr as ocr
from trace_engine.retrieval import read_context
from trace_engine.storage.timeline_db import TimelineDB


class ObservationProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.first = ocr.Observation(101,'editor.exe','Trabajo',(-1920,0,0,1080),1780000000)
        self.other = ocr.Observation(202,'browser.exe','Otro',(0,0,1920,1080),1780000001)
        self.db = Mock()
        self.db.insert.return_value = True
        self.collector = ocr.ScreenOcrCollector(self.db, {})
        self.collector._capture_if_changed = Mock(return_value=(b'frame',True))

    def tick(self, observations, text='El mismo texto'):
        with patch.object(ocr,'foreground_observation',side_effect=observations), \
             patch.object(ocr,'_ocr_png_bytes',new=AsyncMock(return_value=text)) as recognize:
            self.collector._tick(self.loop)
            return recognize

    def test_metadata_and_capture_time_remain_from_original_window(self):
        self.tick([self.first]*3)
        call = self.db.insert.call_args
        self.assertEqual(call.args,('ocr','editor.exe','Trabajo','El mismo texto',self.first.captured_at))
        self.assertEqual(call.kwargs['observation']['window_id'],'101')
        self.assertEqual(call.kwargs['observation']['monitor']['left'],-1920)

    def test_focus_changed_during_capture_skips_recognition(self):
        recognize = self.tick([self.first,self.other])
        recognize.assert_not_called()
        self.db.insert.assert_not_called()
        self.assertIsNone(self.collector._last_hash)

    def test_focus_changed_during_ocr_never_misattributes_text(self):
        self.tick([self.first,self.first,self.other])
        self.db.insert.assert_not_called()
        self.assertIsNone(self.collector._last_hash)

    def test_excluded_window_is_not_captured(self):
        self.collector.excluded_apps = {'editor.exe'}
        self.tick([self.first])
        self.collector._capture_if_changed.assert_not_called()
        self.db.insert.assert_not_called()

    def test_same_text_in_two_windows_keeps_both_origins(self):
        self.tick([self.first]*3)
        self.tick([self.first]*3)
        self.tick([self.other]*3)
        self.assertEqual(self.db.insert.call_count,2)

    def test_hash_dedup_is_scoped_to_window_and_uses_its_monitor(self):
        collector = ocr.ScreenOcrCollector(None,{})
        screen = Mock()
        screen.grab.return_value = SimpleNamespace(size=(2,2),bgra=b'\x00\x00\xff\xff'*4)
        collector._sct = screen
        first = collector._capture_if_changed(self.first)
        duplicate = collector._capture_if_changed(self.first)
        second = collector._capture_if_changed(self.other)
        self.assertTrue(first[1])
        self.assertFalse(duplicate[1])
        self.assertTrue(second[1])
        self.assertEqual(screen.grab.call_args_list[0].args[0],self.first.monitor)

    def test_provenance_is_bounded_retrievable_and_deleted_with_event(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'timeline.db'
            db = TimelineDB(str(path))
            try:
                db.insert('ocr','editor.exe','Documento','Texto',1780000000,
                    observation={'processed_at':1780000002,'window_id':'101','monitor':self.first.monitor})
                response = read_context(path,time_window='intervalo',
                    start_time='2026-01-01',end_time='2027-01-01')
                event = response['events'][0]
                self.assertNotEqual(event['observed_at'],event['observation']['processed_at'])
                self.assertEqual(event['observation']['monitor']['left'],-1920)
                self.assertLessEqual(len(json.dumps(response,ensure_ascii=False,separators=(',',':')).encode()),2000)
                db.cleanup_old_records(0)
                self.assertEqual(db._conn.execute('SELECT COUNT(*) FROM timeline_observations').fetchone()[0],0)
            finally:
                db.close()

    def test_failed_provenance_insert_rolls_back_main_event(self):
        with tempfile.TemporaryDirectory() as directory:
            db = TimelineDB(str(Path(directory)/'timeline.db'))
            try:
                with self.assertRaises(KeyError):
                    db.insert('ocr','editor.exe','Trabajo','Texto',observation={})
                self.assertEqual(db._conn.execute('SELECT COUNT(*) FROM timeline').fetchone()[0],0)
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
