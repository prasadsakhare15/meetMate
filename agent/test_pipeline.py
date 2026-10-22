"""Tests for the pieces added after Phase 3 (flush, drain, breakers).

Self-contained asserts — no test framework needed:

    cd agent
    venv\\Scripts\\activate
    python test_pipeline.py        # exit 0 = all green, 1 = a check failed

The `test_*` functions are also collectable by pytest if it is ever
installed (it is pinned in ../requirements-dev.txt but not in the venv).
Nothing here loads Whisper or the Silero ONNX models: a scripted stub
scorer stands in, so the whole file runs in about a second.
"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("TIMING_LOG", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

from main import MeetingAgent  # noqa: E402
from segment_worker import SegmentWorker  # noqa: E402
from segmenter import (  # noqa: E402
    ChannelSegmenter,
    Segmenter,
    SegmenterConfig,
)
from transcription_worker import TranscriptionWorker  # noqa: E402
from verify_pipeline import (  # noqa: E402
    UniformDecode,
    classify_lost,
    spans_cover,
)

FRAME_MS = 30
RATE = 44100
CHANNELS = 2


# ==============================================================
# HELPERS
# ==============================================================

def make_frame(channel="YOU", ts=0.0, amplitude=4000, duration=0.03):
    samples = int(RATE * duration)
    pcm = (np.ones(samples * CHANNELS, dtype=np.int16) * amplitude).tobytes()
    return {
        "channel": channel,
        "pcm": pcm,
        "channels": CHANNELS,
        "sample_rate": RATE,
        "sample_width": 2,
        "duration": duration,
        "ts": ts,
    }


class ScriptedScorer:
    """Stub Silero: score_window() walks a fixed 0/1 script."""

    def __init__(self, script):
        self.script = list(script)
        self.index = 0

    def score_window(self, samples):
        value = (self.script[self.index]
                 if self.index < len(self.script) else 0.0)
        self.index += 1
        return float(value)

    def reset(self):
        self.index = 0


def speech_frames(count, channel="YOU", start_ts=0.0, amplitude=4000):
    duration = FRAME_MS / 1000
    return [
        make_frame(channel, start_ts + duration * (i + 1), amplitude)
        for i in range(count)
    ]


def wait_for(predicate, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class FakeTranscriber:
    """Records calls and returns one line per call; `delay` simulates
    a slow model so drain/stop timing is observable."""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.calls = 0

    def transcribe(self, audio, *args, **kwargs):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return [{"start": 0.0, "end": 1.0, "text": "hello there"}]


# ==============================================================
# SEGMENTER FLUSH
# ==============================================================

def test_flush_without_frames_is_empty():
    """No audio buffered -> no phantom segment."""
    segmenter = Segmenter(config=SegmenterConfig.from_env())
    assert segmenter.flush() == [], "flush must not invent a segment"


def test_flush_emits_open_segment_and_resets():
    channel_segmenter = ChannelSegmenter(
        "YOU", config=SegmenterConfig.from_env())
    channel_segmenter.frames = speech_frames(50)

    emitted = channel_segmenter.flush()

    assert len(emitted) == 1, f"expected 1 segment, got {len(emitted)}"
    segment = emitted[0]
    assert segment["channel"] == "YOU"
    assert abs(segment["duration"] - 50 * FRAME_MS / 1000) < 1e-6, (
        f"unexpected duration {segment['duration']}")
    assert channel_segmenter.frames == [], "frames must be cleared"
    assert channel_segmenter.flush() == [], "second flush must be empty"


def test_flush_end_to_end_both_channels():
    """Real Segmenter + stub scorer: speech that never gets the closing
    silence is still emitted, per channel, at end of input."""
    config = SegmenterConfig(threshold=0.5, silence_ms=600.0, pre_pad_ms=250.0,
                             post_pad_ms=250.0, max_segment_s=12.0,
                             cut_lookahead_s=2.0, overlap_s=1.0)
    segmenter = Segmenter(config=config)

    # Two channels, enough windows to open a segment on each (512-sample
    # windows = 32 ms, frames are 30 ms). The stub scorer replaces the
    # Silero model so the test needs no ONNX load.
    for channel, ts in (("YOU", 0.0), ("OTHERS", 100.0)):
        segmenter._channels[channel].scorer = ScriptedScorer([1.0] * 400)

        for frame in speech_frames(40, channel=channel, start_ts=ts):
            segmenter.add_frame(frame)

    flushed = segmenter.flush()
    channels = {segment["channel"] for segment in flushed}

    assert len(flushed) == 2, f"expected one segment per channel, got {flushed}"
    assert channels == {"YOU", "OTHERS"}, f"channels {channels}"
    for segment in flushed:
        assert segment["audio"], "flushed segment must carry audio"
        assert segment["duration"] > 0
    assert segmenter.flush() == [], "nothing left after flushing"


# ==============================================================
# WORKER DRAIN / STOP
# ==============================================================

def test_segment_worker_drain_waits_for_pending():
    transcriber = FakeTranscriber(delay=0.05)
    sent = []
    worker = SegmentWorker(transcriber=transcriber,
                           on_transcript=lambda items, key: sent.append(key))
    worker.start()

    for index in range(3):
        worker.submit({"channel": "YOU", "start": float(index),
                       "end": float(index) + 1, "audio": b"\x00" * 2,
                       "channels": 1, "sample_rate": RATE,
                       "duration": 1.0}, f"seg-{index}")

    assert worker.drain(timeout=10.0), "drain timed out"
    assert worker.busy is False, "worker should be idle after drain"
    assert len(sent) == 3, f"expected 3 transcripts, got {len(sent)}"

    worker.stop()
    assert worker._thread is not None and not worker._thread.is_alive(), \
        "stop() must join the worker thread"


def test_segment_worker_stop_waits_for_in_flight():
    """The segment being transcribed when stop() is called must finish
    (and be sent) before stop() returns — otherwise it lands after
    END_MEETING. Segments still *queued* are discarded by design."""
    transcriber = FakeTranscriber(delay=0.3)
    sent = []
    worker = SegmentWorker(transcriber=transcriber,
                           on_transcript=lambda items, key: sent.append(key))
    worker.start()

    worker.submit({"channel": "YOU", "start": 0.0, "end": 1.0,
                   "audio": b"\x00" * 2, "channels": 1,
                   "sample_rate": RATE, "duration": 1.0}, "seg-inflight")

    # Call stop() only once the segment is genuinely mid-transcription.
    assert wait_for(lambda: transcriber.calls >= 1), "never started"
    worker.stop(join_timeout=10.0)

    assert sent == ["seg-inflight"], (
        f"stop() returned before the in-flight transcript was sent: {sent}")
    assert not worker._thread.is_alive(), "thread outlived stop()"

    # Nothing may arrive after stop() has returned.
    before = len(sent)
    time.sleep(0.5)
    assert len(sent) == before, f"late send after stop(): {sent}"


def test_transcription_worker_stop_waits_for_in_flight():
    """Same guarantee on the chunk path (35 s window at small-model RTF
    is slower than the old 10 s join)."""
    transcriber = FakeTranscriber(delay=0.3)
    records = []
    worker = TranscriptionWorker(transcriber=transcriber, chunk_duration=30,
                                 overlap=5,
                                 on_segments_ready=lambda combined, n:
                                 records.append(n))
    worker.start()

    worker.submit("system.wav", "mic.wav", 1)

    assert wait_for(lambda: transcriber.calls == 2), "chunk never started"
    worker.stop(join_timeout=10.0)

    assert records == [1], (
        f"stop() returned before the chunk transcript was delivered: "
        f"{records}")


# ==============================================================
# END OF MEETING (main.stop_capture)
# ==============================================================

class FakeSegmenter:
    def __init__(self, segments):
        self.segments = segments
        self.flushed = 0

    def flush(self):
        self.flushed += 1
        return self.segments


class FakeCloudClient:
    def __init__(self):
        self.events = []
        self.meeting_id = "test-meeting"

    def send_segments(self, segments, key=None):
        self.events.append(("send", key))

    def send_help_me(self):
        self.events.append(("help_me", None))

    def end_meeting(self):
        self.events.append(("end_meeting", None))


class FakeUIServer:
    def __init__(self):
        self.pending = []

    def send(self, message):
        pass


def _agent_for_stop(segmenter, worker, cloud):
    agent = object.__new__(MeetingAgent)
    agent.capture = type("C", (), {
        "capture_started": True,
        "stop": lambda self: cloud.events.append(("capture_stop", None)),
    })()
    agent.segmenter = segmenter
    agent.worker = worker
    agent.cloud_client = cloud
    agent.ui_server = FakeUIServer()
    agent.authenticated = True
    agent._vad_segment_number = 0
    agent._meeting_epoch = 10.0  # tail segment starts at 12.5 s -> 2.5 s
    agent._vad_errors = 0
    return agent


# ==============================================================
# HELP ME FLUSH & DRAIN
# ==============================================================

def test_help_me_flushes_and_drains_before_cloud_send():
    """Clicking HELP ME must cut the open segment, transcribe it, and
    send the transcript before forwarding HELP_ME to cloud."""
    import main as main_module

    cloud = FakeCloudClient()
    sent_items = []
    worker = SegmentWorker(
        transcriber=FakeTranscriber(delay=0.05),
        on_transcript=lambda items, key: (
            cloud.send_segments(items, key=key), sent_items.extend(items)),
    )
    worker.start()

    segment = {"channel": "OTHERS", "start": 12.5, "end": 13.5,
               "duration": 1.0, "audio": b"\x00" * 2, "channels": 1,
               "sample_rate": RATE}
    segmenter = FakeSegmenter([segment])
    agent = _agent_for_stop(segmenter, worker, cloud)

    previous_mode = main_module.SEGMENTATION_MODE
    main_module.SEGMENTATION_MODE = "vad"
    try:
        agent._flush_and_request_help()
    finally:
        main_module.SEGMENTATION_MODE = previous_mode

    assert segmenter.flushed == 1, "segmenter.flush() was not called"
    assert ("send", "seg-1") in cloud.events, f"tail segment never sent: {cloud.events}"
    assert ("help_me", None) in cloud.events, f"help_me was not sent: {cloud.events}"
    assert cloud.events.index(("send", "seg-1")) < cloud.events.index(("help_me", None)), (
        f"transcribed segment must arrive before help_me: {cloud.events}"
    )


# ==============================================================
# END OF MEETING (main.stop_capture)
# ==============================================================


def test_stop_capture_flushes_tail_before_end_meeting():
    """The open segment at stop must be transcribed and sent while the
    meeting is still live."""
    import main as main_module

    cloud = FakeCloudClient()
    sent_items = []
    worker = SegmentWorker(
        transcriber=FakeTranscriber(delay=0.05),
        on_transcript=lambda items, key: (
            cloud.send_segments(items, key=key), sent_items.extend(items)),
    )
    worker.start()

    segment = {"channel": "YOU", "start": 12.5, "end": 13.5,
               "duration": 1.0, "audio": b"\x00" * 2, "channels": 1,
               "sample_rate": RATE}
    segmenter = FakeSegmenter([segment])
    agent = _agent_for_stop(segmenter, worker, cloud)

    previous_mode = main_module.SEGMENTATION_MODE
    main_module.SEGMENTATION_MODE = "vad"
    try:
        agent.stop_capture()
    finally:
        main_module.SEGMENTATION_MODE = previous_mode

    assert segmenter.flushed == 1, "segmenter.flush() was not called"
    assert cloud.events.index(("capture_stop", None)) == 0, cloud.events
    assert ("send", "seg-1") in cloud.events, (
        f"tail segment never sent: {cloud.events}")
    assert cloud.events.index(("send", "seg-1")) < \
        cloud.events.index(("end_meeting", None)), (
        f"tail segment sent after END_MEETING: {cloud.events}")
    assert agent.segmenter is None and agent.worker is None

    # Meeting-relative timestamps must survive the flush path.
    assert sent_items, "no transcript items"
    assert abs(sent_items[0]["start"] - 2.5) < 1e-6, (
        f"timestamp not meeting-relative: {sent_items[0]}")


def test_stop_capture_chunk_mode_unchanged():
    """The chunk path must not gain a flush or a drain."""
    import main as main_module

    class FakeChunkWorker:
        def __init__(self):
            self.stopped = False
            self.drained = False

        def stop(self):
            self.stopped = True

        def drain(self, timeout=None):
            self.drained = True
            return True

    cloud = FakeCloudClient()
    segmenter = FakeSegmenter([{"channel": "YOU"}])
    agent = _agent_for_stop(segmenter, None, cloud)
    worker = FakeChunkWorker()
    agent.worker = worker

    previous_mode = main_module.SEGMENTATION_MODE
    main_module.SEGMENTATION_MODE = "chunk"
    try:
        agent.stop_capture()
    finally:
        main_module.SEGMENTATION_MODE = previous_mode

    assert worker.stopped, "chunk worker was not stopped"
    assert not worker.drained, "chunk mode must not drain"
    assert segmenter.flushed == 0, "chunk mode must not flush a segmenter"
    assert cloud.events == [("capture_stop", None), ("end_meeting", None)], \
        cloud.events


# ==============================================================
# VAD CIRCUIT BREAKER (main._process_vad_frames)
# ==============================================================

class ExplodingSegmenter:
    def __init__(self):
        self.added = 0

    def add_frame(self, frame):
        self.added += 1
        raise RuntimeError("onnx exploded")


class FlakySegmenter:
    def __init__(self, fail_first):
        self.remaining = fail_first

    def add_frame(self, frame):
        if self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError("onnx exploded")
        return []


def _agent_with(segmenter, frames):
    """A MeetingAgent with only the attributes _process_vad_frames uses
    (constructing the real one would open UI/cloud/capture)."""
    agent = object.__new__(MeetingAgent)
    agent.segmenter = segmenter
    agent._vad_errors = 0
    agent._vad_segment_number = 0
    agent.worker = None

    class FakeCapture:
        capture_started = True
        frames_dropped = 0

        def drain_frames(self, max_items=None):
            return frames

    agent.capture = FakeCapture()
    agent._vad_counts = {"YOU": 0, "OTHERS": 0}
    agent._vad_report_start = time.monotonic()
    agent.errors = []
    agent._send_error = agent.errors.append
    agent._send_status = lambda text: None
    return agent


def test_vad_breaker_disables_after_five_errors():
    frames = [make_frame(ts=0.03 * (i + 1)) for i in range(10)]
    agent = _agent_with(ExplodingSegmenter(), frames)

    for _ in range(5):
        agent._process_vad_frames()

    assert agent.segmenter is None, "VAD should be disabled after 5 errors"
    assert len(agent.errors) == 1, f"expected 1 UI error, got {agent.errors}"
    assert "VAD disabled" in agent.errors[0], agent.errors[0]

    # Disabled: frames are counted but never fed again.
    before = agent.segmenter
    agent._process_vad_frames()
    assert agent.segmenter is None and before is None


def test_vad_error_counter_resets_on_success():
    frames = [make_frame(ts=0.03 * (i + 1)) for i in range(10)]
    agent = _agent_with(FlakySegmenter(fail_first=4), frames)

    for _ in range(4):
        agent._process_vad_frames()
        assert agent._vad_errors > 0

    agent._process_vad_frames()  # succeeds -> counter resets
    assert agent._vad_errors == 0, agent._vad_errors

    for _ in range(4):
        agent._process_vad_frames()

    assert agent.segmenter is not None, (
        "counter reset failed: VAD disabled after only 4 later errors")
    assert agent.errors == [], f"unexpected UI errors {agent.errors}"


# ==============================================================
# VERIFIER HELPERS
# ==============================================================

def test_uniform_decode_overrides_every_param():
    seen = {}

    class Inner:
        def transcribe(self, audio, **kwargs):
            seen.update(kwargs)
            return []

    UniformDecode(Inner()).transcribe(
        "audio", beam_size=5, condition_on_previous_text=True,
        initial_prompt="previous text", vad_filter=True)

    assert seen == {
        "beam_size": 1,
        "condition_on_previous_text": False,
        "vad_filter": False,
        "initial_prompt": None,
    }, seen


def test_spans_cover_and_classify_lost():
    spans = [(0.0, 5.0), (10.0, 15.0)]

    assert spans_cover(spans, 1.0, 2.0), "word inside a span"
    assert not spans_cover(spans, 6.0, 7.0), "word in a gap"
    assert not spans_cover(spans, 20.0, 21.0), "word after all spans"
    assert spans_cover(spans, 4.9, 5.1), "span edge overlap"
    assert spans_cover(spans, 6.0, 7.0, pad=1.0), "pad widens the span"

    # Word 0 sits inside a span, word 1 in a gap.
    times = [(1.0, 2.0), (6.0, 7.0)]
    inside, outside = classify_lost(times, [0, 1], spans)

    assert [i for i, _, _ in inside] == [0], inside
    assert [i for i, _, _ in outside] == [1], outside


# ==============================================================
# RUNNER
# ==============================================================

def main():
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]

    failures = 0

    for name, test in tests:
        try:
            test()
        except Exception as e:  # noqa: BLE001 - report and continue
            failures += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
        else:
            print(f"ok   {name}")

    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())