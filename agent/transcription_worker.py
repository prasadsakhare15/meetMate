import queue
import threading
import time

from dedup import deduplicate_segments
from timing_log import TIMING


class TranscriptionWorker:

    def __init__(
        self,
        transcriber,
        chunk_duration,
        overlap,
        on_segments_ready,
    ):

        self.transcriber = transcriber
        self.chunk_duration = chunk_duration
        self.overlap = overlap
        self.on_segments_ready = on_segments_ready

        self.queue = queue.Queue()
        self.stop_event = threading.Event()
        self.thread = None

        self.previous_you_segments = []
        self.previous_others_segments = []

    def start(self):

        self.thread = threading.Thread(
            target=self._run,
            name="TranscriptionWorker",
            daemon=True,
        )
        self.thread.start()

    def submit(self, system_file, mic_file, chunk_number):

        self.queue.put({
            "system_file": system_file,
            "mic_file": mic_file,
            "chunk_number": chunk_number,
        })

    def stop(self, join_timeout=60.0):
        """Stop the worker.

        Waits for the in-flight chunk to finish (and be sent) so the
        caller can end the meeting afterwards; queued chunks are
        discarded. A 35 s window takes ~20 s at `small`-model RTF, which
        already exceeded the old 10 s join.
        """

        self.stop_event.set()
        self.queue.put(None)

        if self.thread:
            self.thread.join(timeout=join_timeout)

            if self.thread.is_alive():
                print(
                    f"[chunk-worker] still transcribing after "
                    f"{join_timeout:.0f}s — it may send after the "
                    f"meeting ended."
                )

    def _run(self):

        while not self.stop_event.is_set():

            item = self.queue.get()

            if item is None:
                break

            try:
                self._process_chunk(item)
            except Exception as e:
                print(f"\nTranscription error (chunk {item['chunk_number']}): {e}")
            finally:
                self.queue.task_done()

    def _process_chunk(self, item):

        system_file = item["system_file"]
        mic_file = item["mic_file"]
        chunk_number = item["chunk_number"]
        key = f"chunk-{chunk_number}"

        TIMING.event("segment_ready", key=key, chunk=chunk_number)

        print(f"\n========== CHUNK {chunk_number} (transcribing) ==========")

        print("Transcribing OTHERS (local)...")
        transcribe_started = time.monotonic()
        others_segments = self.transcriber.transcribe(system_file)
        others_seconds = time.monotonic() - transcribe_started

        print("Transcribing YOU (local)...")
        transcribe_started = time.monotonic()
        you_segments = self.transcriber.transcribe(mic_file)
        you_seconds = time.monotonic() - transcribe_started

        # Real-time factor: Whisper seconds per second of audio (< 1 is
        # faster than real time).
        audio_seconds = float(self.chunk_duration + self.overlap)

        TIMING.event(
            "transcript_ready",
            key=key,
            audio_s=round(audio_seconds, 3),
            whisper_s=round(others_seconds + you_seconds, 3),
            rtf_others=round(others_seconds / audio_seconds, 3),
            rtf_you=round(you_seconds / audio_seconds, 3),
            segs_others=len(others_segments),
            segs_you=len(you_segments),
        )

        chunk_start = (chunk_number - 1) * self.chunk_duration
        overlap_start = chunk_start
        overlap_end = chunk_start + self.overlap

        for segment in you_segments:
            segment["start"] += chunk_start
            segment["end"] += chunk_start

        for segment in others_segments:
            segment["start"] += chunk_start
            segment["end"] += chunk_start

        if chunk_number > 1:
            you_segments = deduplicate_segments(
                you_segments,
                self.previous_you_segments,
                overlap_start,
                overlap_end,
            )
            others_segments = deduplicate_segments(
                others_segments,
                self.previous_others_segments,
                overlap_start,
                overlap_end,
            )

        self.previous_you_segments = you_segments
        self.previous_others_segments = others_segments

        combined = []

        for segment in you_segments:
            combined.append({
                "speaker": "YOU",
                "start": segment["start"],
                "end": segment["end"],
                "text": segment["text"].strip(),
            })

        for segment in others_segments:
            combined.append({
                "speaker": "OTHERS",
                "start": segment["start"],
                "end": segment["end"],
                "text": segment["text"].strip(),
            })

        combined.sort(key=lambda x: x["start"])

        if combined:
            self.on_segments_ready(combined, chunk_number)
