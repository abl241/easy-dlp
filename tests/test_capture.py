import threading
import unittest
from unittest.mock import MagicMock, patch
from ytdlp_app import capture


class CaptureTests(unittest.TestCase):
    def test_cancel_before_opening_device(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(capture.sys, 'platform', 'darwin'):
            with self.assertRaisesRegex(RuntimeError, 'Cancelled'):
                capture.record('Microphone', '/tmp/not-created.m4a', stop=threading.Event(), cancel=cancel, progress=lambda _: None)

    def test_microphone_denial_does_not_open_recorder(self):
        av = MagicMock()
        av.AVAuthorizationStatusNotDetermined = 0
        av.AVAuthorizationStatusAuthorized = 3
        av.AVCaptureDevice.authorizationStatusForMediaType_.return_value = 2
        with self.assertRaisesRegex(RuntimeError, 'Microphone'):
            capture._microphone(av, MagicMock(), 'unused', threading.Event(), threading.Event(), lambda _: None, 20)
        av.AVAudioRecorder.alloc.assert_not_called()

    def test_microphone_is_stopped_when_capture_fails(self):
        av = MagicMock()
        av.AVAuthorizationStatusNotDetermined = 0
        av.AVAuthorizationStatusAuthorized = 3
        av.AVCaptureDevice.authorizationStatusForMediaType_.return_value = 3
        recorder = MagicMock()
        av.AVAudioRecorder.alloc.return_value.initWithURL_settings_error_.return_value = (recorder, None)
        with patch.object(capture, '_listen', side_effect=RuntimeError('device failed')):
            with self.assertRaisesRegex(RuntimeError, 'device failed'):
                capture._microphone(av, MagicMock(), 'unused', threading.Event(), threading.Event(), lambda _: None, 20)
        recorder.stop.assert_called_once()

    def test_stop_ends_recording_loop(self):
        stop = threading.Event()
        progress = MagicMock(side_effect=lambda _: stop.set())
        capture._listen(stop, threading.Event(), progress, 20)
        progress.assert_called_once()

    def test_wait_can_be_cancelled(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(RuntimeError, 'Cancelled'):
            capture._wait(threading.Event(), cancel)
