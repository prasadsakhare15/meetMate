"""VAD segmentation for the local agent (Phase 2).

Turns the ~30 ms frames produced by audio_capture (vad mode) into
utterance-sized segments:

    frame -> mono 16 kHz -> 512-sample windows -> Silero speech
        probability -> per-channel state machine -> segment

State machine (per channel; all timings configurable via VAD_* env vars):

  * speech starts at the first frame whose probability >= threshold,
    seeded with the previous ~250 ms of audio (pre-roll padding);
  * the segment closes after ~600 ms of silence — and never before
    ~250 ms past the last speech frame (post padding);
  * at ~12 s of continuous speech the segmenter waits up to 2 s more,
    cuts at the lowest-energy point of that window and carries ~1 s of
    overlapping audio into the next segment (dedup.py remains the safety
    net for the duplicated words).

Each segment carries: channel (YOU/OTHERS), start timestamp, audio.
Raw audio never leaves the machine — segments are transcribed locally.

Model: the Silero v5 ONNX files already bundled with faster-whisper, run
by onnxruntime (also already installed as a faster-whisper dependency),
so this adds no new package and no download. Each channel keeps its own
decoder state, so interleaved YOU/OTHERS audio does not bleed across.
"""

import os
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Silero v5 input geometry (as used by faster_whisper.vad).
ANALYSIS_RATE = 16000     # analysis runs at 16 kHz mono
WINDOW_SAMPLES = 512      # 32 ms per VAD window
CONTEXT_SAMPLES = 64      # context prepended per window


# ==============================================================
# CONFIGURATION
# ==============================================================

@dataclass(frozen=True)
class SegmenterConfig:
    threshold: float = 0.5       # speech probability to start/continue
    silence_ms: float = 600.0    # silence that closes a segment
    pre_pad_ms: float = 250.0    # audio kept before detected speech start
    post_pad_ms: float = 250.0   # audio kept after the last speech frame
    max_segment_s: float = 12.0  # forced-cut length
    cut_lookahead_s: float = 2.0 # window searched for the cut point
    overlap_s: float = 1.0       # audio carried into the next segment

    @classmethod
    def from_env(cls):
        """Read VAD_* env vars. Invalid values abort with a clear error."""

        def number(name, default, cast, check=None):
            raw = os.getenv(name)
            if raw is None or not str(raw).strip():
                return default
            try:
                value = cast(str(raw).strip())
            except ValueError:
                raise SystemExit(
                    f"Invalid {name}={raw!r} — expected a number."
                )
            if check is not None and not check(value):
                raise SystemExit(f"Invalid {name}={raw!r} — out of range.")
            return value

        return cls(
            threshold=number(
                "VAD_THRESHOLD", 0.5, float,
                check=lambda v: 0.0 <= v <= 1.0,
            ),
            silence_ms=number(
                "VAD_SILENCE_MS", 600.0, float,
                check=lambda v: v > 0,
            ),
            pre_pad_ms=number(
                "VAD_PRE_PAD_MS", 250.0, float,
                check=lambda v: v >= 0,
            ),
            post_pad_ms=number(
                "VAD_POST_PAD_MS", 250.0, float,
                check=lambda v: v >= 0,
            ),
            max_segment_s=number(
                "VAD_MAX_SEGMENT_S", 12.0, float,
                check=lambda v: v > 0,
            ),
            cut_lookahead_s=number(
                "VAD_CUT_LOOKAHEAD_S", 2.0, float,
                check=lambda v: v > 0,
            ),
            overlap_s=number(
                "VAD_OVERLAP_S", 1.0, float,
                check=lambda v: v >= 0,
            ),
        )


# ==============================================================
# MODEL RESOLUTION + STREAMING SCORER
# ==============================================================

def _model_paths():
    """Locate the Silero v5 encoder/decoder ONNX files.

    SILERO_VAD_PATH may point at a directory containing
    silero_encoder_v5.onnx / silero_decoder_v5.onnx, or directly at an
    encoder file (the decoder is derived by name). By default the files
    bundled with faster-whisper are used.
    """

    override = os.getenv("SILERO_VAD_PATH")

    if override:
        path = Path(override)
        if path.is_dir():
            encoder = path / "silero_encoder_v5.onnx"
            decoder = path / "silero_decoder_v5.onnx"
        else:
            encoder = path
            decoder = path.with_name(path.name.replace("encoder", "decoder"))

        if not encoder.exists():
            raise FileNotFoundError(f"SILERO_VAD_PATH encoder not found: {encoder}")
        if not decoder.exists():
            raise FileNotFoundError(f"SILERO_VAD_PATH decoder not found: {decoder}")
        return encoder, decoder

    import faster_whisper

    assets = Path(faster_whisper.__file__).resolve().parent / "assets"
    encoder = assets / "silero_encoder_v5.onnx"
    decoder = assets / "silero_decoder_v5.onnx"

    if not encoder.exists() or not decoder.exists():
        raise FileNotFoundError(
            "Silero VAD models not found in faster-whisper assets "
            f"({assets}). Set SILERO_VAD_PATH to their location."
        )

    return encoder, decoder


class SileroScorer:
    """Streaming speech-probability scorer over 512-sample windows.

    Wraps the encoder/decoder pair faster-whisper ships, keeping decoder
    state and the previous window tail across calls so consecutive
    windows are scored as one continuous stream (one instance per
    channel).
    """

    def __init__(self, encoder_path=None, decoder_path=None):

        import onnxruntime

        if encoder_path is None or decoder_path is None:
            encoder_path, decoder_path = _model_paths()

        # Match faster-whisper's session settings: no thread tuning,
        # quiet logs — the Whisper worker owns the CPU budget.
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 0
        opts.intra_op_num_threads = 0
        opts.log_severity_level = 4

        providers = ["CPUExecutionProvider"]

        self._encoder = onnxruntime.InferenceSession(
            str(encoder_path), providers=providers, sess_options=opts
        )
        self._decoder = onnxruntime.InferenceSession(
            str(decoder_path), providers=providers, sess_options=opts
        )

        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._prev_tail = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)

    def reset(self):
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._prev_tail = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)

    def score_window(self, samples):
        """Score exactly WINDOW_SAMPLES float32 samples at 16 kHz."""

        window = np.asarray(samples, dtype=np.float32).reshape(
            1, WINDOW_SAMPLES
        )

        # Context = previous window's tail (zeros on the first window),
        # matching faster_whisper.vad's one-shot behaviour.
        encoder_input = np.concatenate(
            [self._prev_tail, window], axis=1
        )  # (1, 576)

        encoded = self._encoder.run(None, {"input": encoder_input})[0]
        encoded = encoded.reshape(1, -1, 128)

        out, self._state = self._decoder.run(
            None,
            {"input": encoded[:, 0, :], "state": self._state},
        )

        self._prev_tail = window[:, -CONTEXT_SAMPLES:].copy()

        return float(np.asarray(out).reshape(-1)[0])


# ==============================================================
# AUDIO HELPERS
# ==============================================================

def frame_to_16k_mono(frame):
    """One capture frame -> float32 mono at 16 kHz for VAD scoring.

    The stored segment audio is never resampled — only this analysis
    copy is.
    """

    samples = np.frombuffer(frame["pcm"], dtype=np.int16).astype(np.float32)
    samples /= 32768.0

    if frame["channels"] == 2:
        samples = samples.reshape(-1, 2).mean(axis=1)

    rate = int(frame["sample_rate"])

    if rate != ANALYSIS_RATE:
        if rate % ANALYSIS_RATE == 0:
            samples = samples[:: rate // ANALYSIS_RATE]
        else:
            n_out = max(1, round(len(samples) * ANALYSIS_RATE / rate))
            positions = np.arange(n_out) * (rate / ANALYSIS_RATE)
            samples = np.interp(
                positions, np.arange(len(samples)), samples
            ).astype(np.float32)

    return samples


def frame_rms(frame):
    """Energy of one frame (int16 full scale ≈ 32767)."""

    samples = np.frombuffer(frame["pcm"], dtype=np.int16).astype(np.float32)

    if samples.size == 0:
        return 0.0

    return float(np.sqrt(np.mean(samples * samples)))


# ==============================================================
# PER-CHANNEL STATE MACHINE
# ==============================================================

class ChannelSegmenter:
    """Frames in, finished segments out, for one channel (YOU/OTHERS)."""

    def __init__(self, channel, scorer=None, config=None):

        self.channel = channel
        self.config = config or SegmenterConfig()
        self.scorer = scorer if scorer is not None else SileroScorer()

        self.frames = []          # current segment's frames
        self.speech_active = False
        self.silence_ms = 0.0     # silence since the last speech frame
        self.preroll = deque()    # last pre_pad_ms of audio (any state)
        self.lookahead_from = None  # index where the cut window begins

        self._analysis = np.zeros(0, dtype=np.float32)
        self._last_prob = 0.0

    # ----------------------------------------------------------
    # SCORING
    # ----------------------------------------------------------

    def _score(self, frame):
        """Frame -> speech probability (rolling 512-sample windows)."""

        self._analysis = np.concatenate(
            [self._analysis, frame_to_16k_mono(frame)]
        )

        probs = []

        while len(self._analysis) >= WINDOW_SAMPLES:
            window = self._analysis[:WINDOW_SAMPLES]
            self._analysis = self._analysis[WINDOW_SAMPLES:]
            probs.append(self.scorer.score_window(window))

        if probs:
            self._last_prob = float(np.mean(probs))

        # Frames that do not complete a window inherit the last verdict
        # (windows are 32 ms, frames 30 ms — this keeps frame cadence).
        return self._last_prob

    # ----------------------------------------------------------
    # HELPERS
    # ----------------------------------------------------------

    def _buffer_ms(self):
        return sum(f["duration"] for f in self.frames) * 1000.0

    def _preroll_trim(self):
        while self.preroll and (
            sum(f["duration"] for f in self.preroll) * 1000.0
            > self.config.pre_pad_ms
        ):
            self.preroll.popleft()

    def _reset(self):
        self.frames = []
        self.speech_active = False
        self.silence_ms = 0.0
        self.lookahead_from = None

    def reset(self):
        """Full reset for a new capture session (state, pre-roll, analysis)."""
        self._reset()
        self.preroll.clear()
        self._analysis = np.zeros(0, dtype=np.float32)
        self._last_prob = 0.0
        self.scorer.reset()

    @staticmethod
    def _emit(frames):
        if not frames:
            return None

        start = frames[0]["ts"] - frames[0]["duration"]
        end = frames[-1]["ts"]

        return {
            "channel": frames[0]["channel"],
            "start": start,
            "end": end,
            "audio": b"".join(f["pcm"] for f in frames),
            "sample_rate": frames[0]["sample_rate"],
            "channels": frames[0]["channels"],
            "duration": end - start,
        }

    # ----------------------------------------------------------
    # MAIN ENTRY POINT
    # ----------------------------------------------------------

    def add_frame(self, frame):
        """Feed one frame; return the segments (0 or 1) it closed."""

        prob = self._score(frame)

        if not self.speech_active and prob >= self.config.threshold:
            # Speech begins: seed with the pre-roll (which does not yet
            # contain this frame) plus the frame itself.
            self.frames = list(self.preroll) + [frame]
            self.speech_active = True
            self.silence_ms = 0.0
            self.lookahead_from = None

        elif self.speech_active:
            self.frames.append(frame)

            if prob >= self.config.threshold:
                self.silence_ms = 0.0
            else:
                self.silence_ms += frame["duration"] * 1000.0

                # Close after the silence threshold — but always keep
                # at least post_pad_ms past the last speech frame.
                close_after = max(
                    self.config.silence_ms, self.config.post_pad_ms
                )

                if self.silence_ms >= close_after:
                    closed = self._emit(self.frames)
                    self._reset()
                    # Refresh the pre-roll with the tail just emitted so
                    # the next segment gets its own pre-pad.
                    self._preroll_push(frame)
                    return [closed] if closed else []

        # Pre-roll tracks the latest pre_pad_ms regardless of state, so
        # the next speech onset is padded (also on the start frame and
        # on the frame that closed a segment, pushed above).
        self._preroll_push(frame)

        # ---- forced cut bookkeeping ----
        if not self.speech_active:
            return []

        if (
            self.lookahead_from is None
            and self._buffer_ms() >= self.config.max_segment_s * 1000.0
        ):
            # Max length reached: the frames appended from here on are
            # "the following ~2 s" searched for the cut point.
            self.lookahead_from = len(self.frames)

        if self.lookahead_from is not None:
            window = self.frames[self.lookahead_from:]
            window_ms = sum(f["duration"] for f in window) * 1000.0

            if window_ms >= self.config.cut_lookahead_s * 1000.0:
                return self._forced_cut()

        return []

    def flush(self):
        """Emit the segment still open at end of input (recording EOF).

        Live capture never calls this — when the meeting ends the tail
        is dropped — but offline verification must account for every
        second of the source.
        """
        closed = self._emit(self.frames)
        self._reset()
        return [closed] if closed else []

    def _preroll_push(self, frame):
        self.preroll.append(frame)
        self._preroll_trim()

    # ----------------------------------------------------------
    # FORCED CUT (max segment length)
    # ----------------------------------------------------------

    def _forced_cut(self):
        """Cut at the lowest-energy point of the lookahead window and
        carry overlap_s of audio into the next segment."""

        window = self.frames[self.lookahead_from:]

        energies = [frame_rms(f) for f in window]
        local = min(range(len(window)), key=lambda i: energies[i])
        cut_idx = self.lookahead_from + local  # first frame of next segment

        head = self.frames[:cut_idx]

        # Overlap = the last overlap_s of the finished segment.
        keep_ms = 0.0
        overlap_start = len(head)

        for i in range(len(head) - 1, -1, -1):
            overlap_start = i
            keep_ms += head[i]["duration"] * 1000.0
            if keep_ms >= self.config.overlap_s * 1000.0:
                break

        segment = self._emit(head)

        # Next segment = overlap + everything after the cut point.
        self.frames = self.frames[overlap_start:]
        self.lookahead_from = None

        # If the remainder still exceeds the max (only possible with a
        # tiny max), the next frame re-arms the lookahead.

        return [segment] if segment else []


# ==============================================================
# MULTI-CHANNEL FACADE
# ==============================================================

class Segmenter:
    """Frames tagged YOU/OTHERS in, finished segments out.

    One ChannelSegmenter (with its own Silero decoder state) per
    channel, so interleaved mic/system audio never bleeds across.
    """

    def __init__(self, config=None, channels=("YOU", "OTHERS")):

        self.config = config if config is not None else SegmenterConfig.from_env()

        self._channels = {
            channel: ChannelSegmenter(channel, config=self.config)
            for channel in channels
        }

    @property
    def channels(self):
        return tuple(self._channels)

    def add_frame(self, frame):
        """Feed one frame; return the segments (0 or 1) it closed."""

        try:
            channel_segmenter = self._channels[frame["channel"]]
        except KeyError:
            raise ValueError(
                f"Unknown frame channel: {frame['channel']!r}"
            ) from None

        return channel_segmenter.add_frame(frame)

    def flush(self):
        """End of input: emit any still-open segment from every channel."""
        closed = []
        for channel_segmenter in self._channels.values():
            closed.extend(channel_segmenter.flush())
        return closed

    def reset(self):
        for channel_segmenter in self._channels.values():
            channel_segmenter.reset()
