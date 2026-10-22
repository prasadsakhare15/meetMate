"""Priority queue + single Whisper worker for VAD segments (Phase 3).

Finished segments from the segmenter go into a queue where OTHERS (the
remote speakers, whose questions trigger suggestions) are dequeued before
YOU, FIFO within each channel. Each segment is transcribed and handed to
the cloud the moment it is done — no waiting for a chunk boundary.

CPU tuning (plan): beam_size=1, condition_on_previous_text=False, explicit
cpu_threads (set on the Transcriber constructor), and the Whisper prompt is
the previous segment's text from the SAME channel.

dedup.py stays only as a safety net: it runs when a segment overlaps its
channel's previous segment (a forced-cut overlap, or a very tight restart
that overlaps trailing silence).
"""

import threading
import time

from dedup import deduplicate_segments
from segmenter import frame_to_16k_mono
from timing_log import TIMING

# Backlog bound: if Whisper falls this far behind, make room by dropping
# the oldest YOU segment first (questions win twice — at intake and at
# dequeue). Pathological full-queue cases drop the oldest question, or an
# incoming YOU when only questions are pending.
MAX_PENDING_SEGMENTS = 100

# Whisper keeps ~228 prompt tokens; segments are <= ~14 s of speech, so
# this only guards against pathological output.
MAX_PROMPT_CHARS = 600


class _SegmentQueue:
    """FIFO within a channel; OTHERS dequeued before YOU. Stop discards."""

    def __init__(self):
        self._cond = threading.Condition()
        self._others = []
        self._you = []
        self._stopped = False

    @property
    def size(self):
        with self._cond:
            return len(self._others) + len(self._you)

    def put(self, segment):
        with self._cond:
            if self._stopped:
                return False
            if segment["channel"] == "OTHERS":
                self._others.append(segment)
            else:
                self._you.append(segment)
            self._cond.notify()
            return True

    def get(self):
        """Block for the highest-priority segment; None once stopped
        (pending backlog is discarded — the meeting is over)."""
        with self._cond:
            while not self._stopped and not self._others and not self._you:
                self._cond.wait()

            if self._stopped:
                return None

            if self._others:
                return self._others.pop(0)

            return self._you.pop(0)

    def drop_oldest(self, channel):
        """Evict one queued segment of that channel; False if none."""
        with self._cond:
            queue = self._others if channel == "OTHERS" else self._you
            if queue:
                queue.pop(0)
                return True
            return False

    def stop(self):
        with self._cond:
            self._stopped = True
            self._others.clear()  # discard the backlog
            self._you.clear()
            self._cond.notify_all()


class SegmentWorker:
    """Single thread: segment in, transcript to the cloud out."""

    def __init__(self, transcriber, on_transcript):

        self.transcriber = transcriber
        self.on_transcript = on_transcript  # callback(items, key)

        self._queue = _SegmentQueue()
        self._thread = None

        # Tracks the segment currently being transcribed, so the agent
        # can wait for it at end of meeting (drain) instead of letting
        # it send after END_MEETING.
        self._activity = threading.Condition()
        self._inflight = 0

        # Per-channel state (transcripts never bleed across channels).
        self.previous_text = {"YOU": "", "OTHERS": ""}
        self.previous_end = {"YOU": float("-inf"), "OTHERS": float("-inf")}
        self.previous_items = {"YOU": [], "OTHERS": []}

        self.dropped = 0

    # ----------------------------------------------------------
    # LIFECYCLE
    # ----------------------------------------------------------

    def start(self):
        self._thread = threading.Thread(
            target=self._run,
            name="SegmentWorker",
            daemon=True,
        )
        self._thread.start()

    def stop(self, join_timeout=60.0):
        """Stop the worker.

        The in-flight segment is allowed to finish and is still sent, so
        the caller may end the meeting afterwards; the pending backlog is
        discarded because the meeting is over. A join that expires is
        reported — the thread is a daemon and would otherwise send late.
        """

        self._queue.stop()

        if self._thread:
            self._thread.join(timeout=join_timeout)

            if self._thread.is_alive():
                print(
                    f"[worker] still transcribing after {join_timeout:.0f}s "
                    f"— it may send after the meeting ended."
                )

    @property
    def busy(self):
        """True while a segment is queued or being transcribed."""
        with self._activity:
            return bool(self._inflight or self._queue.size)

    def drain(self, timeout=15.0):
        """Wait until queued and in-flight segments are finished.

        Used at end of meeting so a just-submitted tail segment still
        reaches the cloud before END_MEETING. True when the worker went
        idle, False on timeout.
        """

        deadline = time.monotonic() + timeout

        with self._activity:
            while (self._inflight or self._queue.size) \
                    and time.monotonic() < deadline:
                self._activity.wait(0.05)

            return not (self._inflight or self._queue.size)

    @property
    def pending(self):
        return self._queue.size

    def submit(self, segment, key):
        """Enqueue a finished segment; returns False if it was dropped."""

        if self._queue.size >= MAX_PENDING_SEGMENTS:
            # Make room: evict the oldest YOU; only if none exist does
            # the newcomer lose (incoming YOU) or the oldest question
            # (incoming OTHERS — keep the newest question).
            if segment["channel"] == "YOU":
                if not self._queue.drop_oldest("YOU"):
                    self.dropped += 1
                    return False
            elif not (
                self._queue.drop_oldest("YOU")
                or self._queue.drop_oldest("OTHERS")
            ):
                self.dropped += 1
                return False

            self.dropped += 1

        return self._queue.put(dict(segment, key=key))

    # ----------------------------------------------------------
    # WORKER LOOP
    # ----------------------------------------------------------

    def _run(self):

        while True:

            segment = self._queue.get()

            if segment is None:
                break

            try:
                with self._activity:
                    self._inflight += 1

                self._process(segment)
            except Exception as e:
                print(f"Transcription error ({segment.get('key')}): {e}")
            finally:
                with self._activity:
                    self._inflight -= 1
                    self._activity.notify_all()

    def _process(self, segment):

        key = segment["key"]
        channel = segment["channel"]
        audio_s = float(segment["duration"])

        analysis = frame_to_16k_mono({
            "pcm": segment["audio"],
            "channels": segment["channels"],
            "sample_rate": segment["sample_rate"],
        })

        prompt = self.previous_text[channel] or None

        started = time.monotonic()

        raw = self.transcriber.transcribe(
            analysis,
            beam_size=1,
            condition_on_previous_text=False,
            initial_prompt=prompt,
            vad_filter=False,  # the segmenter already cut the silence
        )

        elapsed = time.monotonic() - started
        rtf = elapsed / max(audio_s, 1e-6)

        TIMING.event(
            "transcript_ready",
            key=key,
            channel=channel,
            audio_s=round(audio_s, 3),
            whisper_s=round(elapsed, 3),
            rtf=round(rtf, 3),
            lines=len(raw),
        )
        print(
            f"[whisper] {key} {channel} {audio_s:.2f}s -> "
            f"{elapsed:.2f}s (rtf {rtf:.2f})"
        )

        # Meeting-relative seconds (the server renders mm:ss from start).
        base = segment["start"]

        items = []

        for entry in raw:
            text = entry["text"].strip()
            if not text:
                continue
            items.append({
                "speaker": channel,
                "start": round(base + entry["start"], 3),
                "end": round(base + entry["end"], 3),
                "text": text,
            })

        # Safety net only: overlaps exist when a forced cut (or a tight
        # restart over trailing silence) repeated words.
        if items and segment["start"] < self.previous_end[channel]:
            items = deduplicate_segments(
                items,
                self.previous_items[channel],
                overlap_start=segment["start"],
                overlap_end=self.previous_end[channel],
            )

        self.previous_end[channel] = segment["end"]
        self.previous_items[channel] = items

        joined = " ".join(item["text"] for item in items)
        self.previous_text[channel] = joined[:MAX_PROMPT_CHARS]

        if items:
            self.on_transcript(items, key)
