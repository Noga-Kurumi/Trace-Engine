"""Regression tests for bounded, dated historical screen context."""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from trace_engine.collectors import uia_context
from trace_engine.storage.timeline_db import TimelineDB


class HistoricalScreenTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = TimelineDB(str(Path(self.directory.name) / 'timeline.db'))
        self.now = time.time()

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def context(self):
        with patch.object(uia_context, '_active_window_info', return_value='CurrentApp'), \
             patch.object(uia_context, 'get_uia_text', return_value=''), \
             patch.object(uia_context, '_ocr_screen_text', return_value=''), \
             patch('trace_engine.storage.timeline_db.TimelineDB', return_value=self.db), \
             patch.object(self.db, 'close') as close:
            result = uia_context.get_screen_context_text()
        close.assert_called_once()
        return result

    def test_newest_ocr_survives_more_than_ten_records_and_other_sources(self):
        for index in range(20):
            self.db.insert('ocr', 'RecordedApp', 'OldWindow', f'frame-{index}', self.now - 100 + index)
        for source in ('clipboard', 'audio_in', 'audio_out', 'app_change'):
            self.db.insert(source, 'OtherApp', 'OtherWindow', 'unrelated payload', self.now - 1)
        row = self.db.get_latest_screen_record(self.now - 300, self.now)
        self.assertEqual(row[-1], 'frame-19')
        result = self.context()
        self.assertIn('frame-19', result)
        self.assertNotIn('unrelated payload', result)
        self.assertIn('Ventana en foco: CurrentApp', result)
        self.assertIn('Registro histórico OCR', result)
        self.assertIn('RecordedApp', result)

    def test_non_screen_sources_are_never_screen_fallback(self):
        for source in ('clipboard', 'audio_in', 'audio_out', 'app_change'):
            self.db.insert(source, 'OtherApp', 'OtherWindow', 'unrelated payload', self.now - 1)
        self.assertIsNone(self.db.get_latest_screen_record(self.now - 3600, self.now))
        self.assertEqual(self.context(), 'Ventana en foco: CurrentApp')

    def test_hour_fallback_is_dated_and_expired_records_are_ignored(self):
        self.db.insert('ocr', 'RecordedApp', 'OldWindow', 'older screen', self.now - 600)
        self.db.insert('ocr', 'RecordedApp', 'OldWindow', 'expired screen', self.now - 4000)
        result = self.context()
        self.assertIn('older screen', result)
        self.assertNotIn('expired screen', result)
        self.assertIn('No confirma el contenido actual', result)

    def test_equal_timestamps_use_latest_insert(self):
        self.db.insert('ocr', 'App', '', 'first', self.now)
        self.db.insert('ocr', 'App', '', 'second', self.now)
        self.assertEqual(self.db.get_latest_screen_record(self.now, self.now)[-1], 'second')

    def test_query_failure_still_closes_database(self):
        with patch.object(self.db, 'get_latest_screen_record', side_effect=RuntimeError('query failed')):
            self.assertEqual(self.context(), 'Ventana en foco: CurrentApp')

    def test_live_ocr_does_not_open_history(self):
        with patch.object(uia_context, '_active_window_info', return_value='CurrentApp'), \
             patch.object(uia_context, 'get_uia_text', return_value=''), \
             patch.object(uia_context, '_ocr_screen_text', return_value='live OCR'), \
             patch('trace_engine.storage.timeline_db.TimelineDB') as database:
            result = uia_context.get_screen_context_text()
        database.assert_not_called()
        self.assertIn('live OCR', result)


if __name__ == '__main__':
    unittest.main()
