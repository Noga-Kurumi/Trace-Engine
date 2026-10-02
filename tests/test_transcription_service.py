"""Queued interactive requests take priority without interrupting active work."""
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from trace_engine.transcription import TranscriptionPriority, TranscriptionService


class PriorityTests(unittest.TestCase):
    def test_interactive_request_precedes_queued_meeting(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        class Segment:
            def __init__(self, text):
                self.text = text
        class Model:
            def transcribe(self, audio):
                calls.append(audio)
                if audio == 'active':
                    entered.set()
                    if not release.wait(timeout=3):
                        raise TimeoutError('Fixture did not release active request')
                return [Segment(audio)]
        service = TranscriptionService('unused', model=Model())
        try:
            active = service.submit('active')
            self.assertTrue(entered.wait(timeout=2))
            meeting = service.submit('meeting', source='meeting', priority=TranscriptionPriority.MEETING)
            interactive = service.submit('dictado', priority=TranscriptionPriority.INTERACTIVE)
            release.set()
            self.assertEqual(active.get(timeout=2).text, 'active')
            self.assertEqual(interactive.get(timeout=2).text, 'dictado')
            self.assertEqual(meeting.get(timeout=2).text, 'meeting')
            self.assertEqual(calls, ['active', 'dictado', 'meeting'])
        finally:
            release.set()
            self.assertTrue(service.close())


if __name__ == '__main__':
    unittest.main()
