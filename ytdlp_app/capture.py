"""Bounded, explicit macOS audio capture. No device opens at import time."""
from __future__ import annotations

import platform
import sys
import threading
import time


MIC_PERMISSION = "Allow microphone access in System Settings → Privacy & Security → Microphone, then restart the app."
SYSTEM_PERMISSION = "Allow access in System Settings → Privacy & Security → Screen & System Audio Recording, then restart the app."


def _wait(event, cancel, stop=None, timeout=30):
    deadline = time.monotonic() + timeout
    while not event.wait(.05):
        if cancel.is_set() or (stop is not None and stop.is_set()):
            raise RuntimeError("Cancelled")
        if time.monotonic() >= deadline:
            raise RuntimeError("Audio capture timed out waiting for macOS. Check recording permissions and try again.")


def _listen(stop, cancel, progress, seconds, failure=lambda: None):
    started = time.monotonic()
    previous = -1
    while not stop.is_set() and not cancel.is_set():
        error = failure()
        if error:
            raise RuntimeError(str(error))
        elapsed = time.monotonic() - started
        if elapsed >= seconds:
            break
        remaining = max(0, int(seconds - elapsed))
        if remaining != previous:
            progress(f"● Listening… {remaining}s remaining")
            previous = remaining
        cancel.wait(.05)


def record(source, path, *, stop, cancel, progress, seconds=20):
    if sys.platform != "darwin":
        raise RuntimeError("Live audio capture currently requires macOS. You can still identify an audio file or YouTube link.")
    if cancel.is_set() or stop.is_set():
        raise RuntimeError("Cancelled")
    try:
        import AVFoundation as AV
        from Foundation import NSURL
    except ImportError as error:
        raise RuntimeError("Audio capture dependencies are missing. Run ./run.sh --update and restart.") from error
    if source == "Microphone":
        _microphone(AV, NSURL, path, stop, cancel, progress, seconds)
    elif source == "Computer audio":
        if int(platform.mac_ver()[0].split('.')[0]) < 13:
            raise RuntimeError("Computer audio capture requires macOS 13 or newer.")
        _system(AV, NSURL, path, stop, cancel, progress, seconds)
    else:
        raise ValueError("Unknown audio source")
    if cancel.is_set():
        raise RuntimeError("Cancelled")


def _microphone(AV, NSURL, path, stop, cancel, progress, seconds):
    # CLI Python builds may omit a usage string from their runtime bundle.
    # Supply it before AVFoundation requests access; packaged launchers carry
    # the same description in their on-disk Info.plist.
    from Foundation import NSBundle
    info = NSBundle.mainBundle().infoDictionary()
    if info is not None and not info.get("NSMicrophoneUsageDescription"):
        info["NSMicrophoneUsageDescription"] = "Identify a song from a short recording when you choose Listen."
    progress("Waiting for microphone permission…")
    status = AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeAudio)
    if status == AV.AVAuthorizationStatusNotDetermined:
        event = threading.Event()
        granted = []
        def permission(ok):
            granted.append(ok)
            event.set()
        AV.AVCaptureDevice.requestAccessForMediaType_completionHandler_(AV.AVMediaTypeAudio, permission)
        _wait(event, cancel, stop, timeout=120)
        if not granted[0]:
            raise RuntimeError(MIC_PERMISSION)
    elif status != AV.AVAuthorizationStatusAuthorized:
        raise RuntimeError(MIC_PERMISSION)
    if cancel.is_set() or stop.is_set():
        raise RuntimeError("Cancelled")
    settings = {AV.AVFormatIDKey: int.from_bytes(b'aac ', 'big'),
                AV.AVSampleRateKey: 44100., AV.AVNumberOfChannelsKey: 1,
                AV.AVEncoderBitRateKey: 128000}
    recorder, error = AV.AVAudioRecorder.alloc().initWithURL_settings_error_(
        NSURL.fileURLWithPath_(str(path)), settings, None)
    if error or recorder is None:
        raise RuntimeError("Could not open the microphone. " + MIC_PERMISSION)
    try:
        if not recorder.record():
            raise RuntimeError("Could not start the microphone. " + MIC_PERMISSION)
        _listen(stop, cancel, progress, seconds,
                failure=lambda: None if recorder.isRecording() else "The microphone stopped unexpectedly.")
    finally:
        recorder.stop()


# Lazily define the Objective-C delegate once. PyObjC class names are global.
_output_class = None


def _get_output_class():
    global _output_class
    if _output_class is not None:
        return _output_class
    import objc
    import ScreenCaptureKit as SC
    import CoreMedia as CM
    from Foundation import NSObject

    class EasyDLPAudioOutput(NSObject, protocols=[objc.protocolNamed("SCStreamOutput"), objc.protocolNamed("SCStreamDelegate")]):
        def stream_didOutputSampleBuffer_ofType_(self, stream, sample, kind):
            if kind != SC.SCStreamOutputTypeAudio or not CM.CMSampleBufferDataIsReady(sample):
                return
            with self.lock:
                if self.closed:
                    return
                try:
                    if not self.started:
                        if not self.writer.startWriting():
                            self.failure = "Could not start writing captured audio."
                            return
                        self.writer.startSessionAtSourceTime_(CM.CMSampleBufferGetPresentationTimeStamp(sample))
                        self.started = True
                    if self.input.isReadyForMoreMediaData():
                        if not self.input.appendSampleBuffer_(sample):
                            self.failure = "Could not save captured audio."
                except Exception as error:
                    self.failure = str(error)

        def stream_didStopWithError_(self, stream, error):
            self.failure = "Computer audio capture stopped. " + str(error.localizedDescription())

    _output_class = EasyDLPAudioOutput
    return _output_class


def _system(AV, NSURL, path, stop, cancel, progress, seconds):
    try:
        import ScreenCaptureKit as SC
    except ImportError as error:
        raise RuntimeError("System audio capture dependencies are missing. Run ./run.sh --update.") from error
    progress("Waiting for computer-audio permission…")
    ready = threading.Event()
    content_result = []
    def content_done(content, error):
        content_result.extend((content, error))
        ready.set()
    SC.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(True, True, content_done)
    _wait(ready, cancel, stop, timeout=120)
    content, error = content_result
    if error or content is None:
        raise RuntimeError(SYSTEM_PERMISSION)
    if not content.displays():
        raise RuntimeError("No display is available for computer-audio capture.")
    if cancel.is_set() or stop.is_set():
        raise RuntimeError("Cancelled")
    filter_ = SC.SCContentFilter.alloc().initWithDisplay_excludingWindows_(content.displays()[0], [])
    config = SC.SCStreamConfiguration.alloc().init()
    config.setCapturesAudio_(True)
    config.setExcludesCurrentProcessAudio_(True)
    config.setSampleRate_(48000)
    config.setChannelCount_(2)
    config.setWidth_(2)
    config.setHeight_(2)
    # Only attach an audio output: no screen frames are received or saved.
    writer, error = AV.AVAssetWriter.alloc().initWithURL_fileType_error_(NSURL.fileURLWithPath_(str(path)), AV.AVFileTypeAppleM4A, None)
    if error:
        raise RuntimeError("Could not create a temporary audio recording.")
    settings = {AV.AVFormatIDKey: int.from_bytes(b'aac ', 'big'), AV.AVSampleRateKey: 48000.,
                AV.AVNumberOfChannelsKey: 2, AV.AVEncoderBitRateKey: 128000}
    audio_input = AV.AVAssetWriterInput.assetWriterInputWithMediaType_outputSettings_(AV.AVMediaTypeAudio, settings)
    audio_input.setExpectsMediaDataInRealTime_(True)
    if not writer.canAddInput_(audio_input):
        raise RuntimeError("The computer-audio format is not supported.")
    writer.addInput_(audio_input)
    output = _get_output_class().alloc().init()
    output.writer, output.input = writer, audio_input
    output.lock = threading.Lock()
    output.started, output.closed, output.failure = False, False, None
    stream = SC.SCStream.alloc().initWithFilter_configuration_delegate_(filter_, config, output)
    ok, error = stream.addStreamOutput_type_sampleHandlerQueue_error_(output, SC.SCStreamOutputTypeAudio, None, None)
    if not ok:
        raise RuntimeError("Could not initialize computer audio. " + SYSTEM_PERMISSION)
    started = threading.Event()
    start_errors = []
    def start_done(error):
        start_errors.append(error)
        if cancel.is_set() or stop.is_set():
            stream.stopCaptureWithCompletionHandler_(lambda ignored: None)
        started.set()
    stream.startCaptureWithCompletionHandler_(start_done)
    try:
        _wait(started, cancel, stop)
        if start_errors[0]:
            raise RuntimeError("Could not start computer audio. " + SYSTEM_PERMISSION)
        _listen(stop, cancel, progress, seconds, failure=lambda: output.failure)
    finally:
        stopped = threading.Event()
        stream.stopCaptureWithCompletionHandler_(lambda error: stopped.set())
        stopped.wait(10)
        with output.lock:
            output.closed = True
            if output.started and not cancel.is_set() and not output.failure:
                audio_input.markAsFinished()
                finished = threading.Event()
                writer.finishWritingWithCompletionHandler_(lambda: finished.set())
            else:
                writer.cancelWriting()
                finished = None
        if finished is not None and not finished.wait(10):
            writer.cancelWriting()
            raise RuntimeError("Timed out finishing the computer-audio recording.")
    if not output.started:
        raise RuntimeError("No computer audio was captured. Play a song and try again; some apps block audio capture.")
    if writer.status() != AV.AVAssetWriterStatusCompleted and not cancel.is_set():
        raise RuntimeError("Could not finish the computer-audio recording.")
