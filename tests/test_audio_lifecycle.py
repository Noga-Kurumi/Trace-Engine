"""Resource ownership and cancellation regressions without loading Whisper."""
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from trace_engine import TraceConfig, TraceEngine
from trace_engine.transcription import TranscriptionService
from trace_engine.transcription.service import _transcribe_via_cli
from trace_engine.transcription.streaming import StreamingTranscriber


class AudioLifecycleTests(unittest.TestCase):
    def test_failed_shutdown_is_not_reported_as_success_on_second_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            transcriber = Mock()
            transcriber.close.return_value = False
            engine = TraceEngine(TraceConfig(db_path=str(Path(directory) / 'timeline.db')),
                                 transcriber=transcriber)
            self.assertFalse(engine.stop())
            self.assertFalse(engine.stop())
            transcriber.close.assert_called_once()
            with self.assertRaises(RuntimeError):
                engine.start()

    def test_pause_preserves_storage_and_shared_speech(self):
        with tempfile.TemporaryDirectory() as directory:
            transcriber = Mock()
            engine = TraceEngine(TraceConfig(db_path=str(Path(directory) / 'timeline.db')),
                                 transcriber=transcriber)
            engine.collector = Mock()
            engine.pause_collectors()
            transcriber.close.assert_not_called()
            self.assertTrue(engine.insert('ocr', 'screen after pause'))
            self.assertTrue(engine.stop())
            self.assertTrue(engine.stop())

    def test_injected_transcriber_does_not_create_unused_whisper_service(self):
        with tempfile.TemporaryDirectory() as directory:
            transcriber = Mock()
            with patch('trace_engine.transcription.TranscriptionService') as factory:
                engine = TraceEngine(TraceConfig(db_path=str(Path(directory) / 'timeline.db'),
                                                whisper_model_path='unused'), transcriber=transcriber)
                factory.assert_not_called()
                self.assertIs(engine.transcription_service, transcriber)
                self.assertTrue(engine.stop())

    def test_failed_component_does_not_prevent_other_resources_from_closing(self):
        with tempfile.TemporaryDirectory() as directory:
            transcriber = Mock()
            transcriber.close.side_effect = RuntimeError('shutdown failed')
            engine = TraceEngine(TraceConfig(db_path=str(Path(directory) / 'timeline.db')),
                                 transcriber=transcriber)
            stream = engine.streaming_transcriber = Mock()
            with patch.object(engine.db, 'close', wraps=engine.db.close) as close:
                self.assertFalse(engine.stop())
                stream.stop.assert_called_once()
                close.assert_called_once()
            self.assertFalse(engine.stop())

    def test_closed_service_resolves_new_requests(self):
        service = TranscriptionService('unused', model=Mock())
        self.assertTrue(service.close())
        self.assertIsNotNone(service.submit([], source='test').get(timeout=1).error)
        self.assertTrue(service.close())

    def test_cli_cancellation_kills_real_child_and_removes_temporary_wav(self):
        popen = subprocess.Popen
        children = []
        wavs = []
        def launch(*args, **kwargs):
            child = popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            children.append(child)
            return child
        stop = threading.Event()
        timer = threading.Timer(0.2, stop.set)
        timer.start()
        started = time.monotonic()
        try:
            with patch('trace_engine.transcription.service.subprocess.Popen', side_effect=launch), \
                 patch('trace_engine.transcription.service._write_wav', side_effect=lambda path, _: wavs.append(path)):
                with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                    _transcribe_via_cli(sys.executable, 'unused', [], stop_event=stop)
            self.assertLess(time.monotonic() - started, 2)
            self.assertTrue(children)
            self.assertTrue(all(child.poll() is not None for child in children))
            self.assertTrue(all(not Path(path).exists() for path in wavs))
        finally:
            timer.cancel()
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=2)

    def test_cli_failure_without_stdout_is_an_error(self):
        process = Mock(returncode=1)
        process.communicate.return_value = (b'', b'unexpected failure')
        process.poll.return_value = 1
        with patch('trace_engine.transcription.service.subprocess.Popen', return_value=process), \
             patch('trace_engine.transcription.service._write_wav'):
            with self.assertRaisesRegex(RuntimeError, 'failed'):
                _transcribe_via_cli(sys.executable, 'unused', [])

    def test_stream_stop_unblocks_writer_without_lifecycle_deadlock(self):
        entered, released = threading.Event(), threading.Event()
        class Pipe:
            def write(self, data):
                entered.set()
                if not released.wait(timeout=3):
                    raise TimeoutError('writer trapped')
            def flush(self):
                pass
            def close(self):
                released.set()
        process = Mock(stdin=Pipe(), stdout=None, stderr=None)
        process.poll.return_value = None
        process.terminate.side_effect = released.set
        streaming = StreamingTranscriber('unused', 'unused')
        streaming._process = process
        writer = threading.Thread(target=streaming.send, args=([0.0],))
        writer.start()
        try:
            self.assertTrue(entered.wait(timeout=2))
            streaming.stop()
            writer.join(timeout=2)
            self.assertFalse(writer.is_alive())
            process.terminate.assert_called_once()
            self.assertIsNone(streaming._process)
        finally:
            released.set()
            writer.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
