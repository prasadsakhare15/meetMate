#!/usr/bin/env python3
"""Phase 5 verification: run one test WAV through BOTH segmentation modes
and compare them.

    chunk (before): real 30 s + 5 s-overlap chunk slicing -> real
        TranscriptionWorker (beam 5 / condition-on / dedup on overlap)
    vad   (after):  real 30 ms frames -> real Segmenter (Silero) ->
        real SegmentWorker (beam 1, priority queue, same-channel prompt)

Both modes are run TWICE:
  * production settings (chunk: beam 5 / condition-on / vad_filter;
    vad: beam 1 / prompt) -> the honest latency + RTF table
  * UNIFORM decode settings (beam 1, condition-off, vad_filter off,
    no prompt) on BOTH paths -> the word diff verdict, so a "lost"
    word means the segmenter dropped audio, not that beam 5 phrases
    things differently from beam 1

Prints, per mode:
  * per-stage latency (queue wait, whisper, send) mean/max
  * end-to-end transcript latency (question end -> text ready), split into
    the structural waiting time (chunk boundary / silence close) and the
    measured processing time
  * Whisper real-time factor
  * a word-level diff between the two transcripts (chunk = reference)
    under uniform decode settings

Exit code: 0 = PASS/WARN, 1 = FAIL (retention < 80% or a mode produced no
transcript).
"""

import argparse
import glob
import json
import os
import re
import shutil
import sys
import tempfile
import time
import wave
from difflib import SequenceMatcher
from pathlib import Path

# Must be set before the first TIMING event (read lazily on first use).
os.environ["TIMING_LOG"] = "1"

from dedup import normalize_text                      # noqa: E402
from segmenter import Segmenter, SegmenterConfig      # noqa: E402
from segment_worker import SegmentWorker              # noqa: E402
from timing_log import TIMING                         # noqa: E402
from transcriber import Transcriber                   # noqa: E402
from transcription_worker import TranscriptionWorker  # noqa: E402

CHUNK_DURATION = 30
OVERLAP = 5
FRAME_MS = 30
DEFAULT_LIMIT_S = 130


# ==============================================================
# AUDIO HELPERS
# ==============================================================

def load_wav(path):
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise SystemExit(f"{path}: only 16-bit PCM is supported")
        return (
            handle.getframerate(),
            handle.getnchannels(),
            handle.readframes(handle.getnframes()),
        )


def write_wav(path, pcm, rate, channels):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)


def load_source(paths, limit_s):
    """Concatenate same-params WAVs, trimmed to limit_s."""

    rate = channels = None
    chunks = []
    total = 0.0
    used = []

    for path in paths:
        r, c, pcm = load_wav(path)
        if rate is None:
            rate, channels = r, c
        elif (r, c) != (rate, channels):
            print(f"  (skipping {path}: params differ)")
            continue

        if total >= limit_s:
            break

        chunks.append(pcm)
        total += len(pcm) / (rate * channels * 2)
        used.append(str(path))

    pcm = b"".join(chunks)

    if limit_s:
        max_bytes = int(limit_s * rate * channels * 2)
        pcm = pcm[:max_bytes]

    return rate, channels, pcm, used, len(pcm) / (rate * channels * 2)


def iter_frames(pcm, rate, channels, frame_ms=FRAME_MS):
    """30 ms frames exactly like audio_capture emits in vad mode, except
    ts is the FILE position in seconds (offline run, no wall clock)."""

    samples = int(rate * frame_ms / 1000)
    step = samples * channels * 2

    for offset in range(0, len(pcm) - step + 1, step):
        yield {
            "channel": "YOU",
            "pcm": pcm[offset:offset + step],
            "channels": channels,
            "sample_rate": rate,
            "sample_width": 2,
            "duration": samples / rate,
            "ts": (offset + step) / (rate * channels * 2),
        }


# ==============================================================
# MODE RUNNERS
# ==============================================================

class CountingTranscriber:
    """Wraps a Transcriber to count completed transcribe() calls."""

    def __init__(self, inner):
        self.inner = inner
        self.completed = 0

    def transcribe(self, *args, **kwargs):
        try:
            return self.inner.transcribe(*args, **kwargs)
        finally:
            self.completed += 1


class UniformDecode:
    """Forces identical Whisper decode params on every call.

    The word diff must measure SEGMENTATION loss, not decoder settings —
    chunk production (beam 5, condition-on) versus segment production
    (beam 1, prompt) rephrases enough text to drown the signal. Params
    match the segment path exactly, minus the same-channel prompt.
    """

    def __init__(self, inner):
        self.inner = inner

    def transcribe(self, audio, *args, **kwargs):
        kwargs.update(
            beam_size=1,
            condition_on_previous_text=False,
            vad_filter=False,
            initial_prompt=None,
        )
        return self.inner.transcribe(audio, **kwargs)


def run_chunk_mode(source, rate, channels, transcriber, out_dir,
                   key_prefix=""):
    """Real chunk slicing + real TranscriptionWorker (dedup included)."""

    pcm, total = source
    window = CHUNK_DURATION + OVERLAP
    units = {}
    records = {}

    system_file = Path(out_dir) / "system.wav"
    write_wav(system_file, b"\x00" * (window * rate * channels * 2),
              rate, channels)

    def on_segments(combined, chunk_number):
        records[chunk_number] = [
            item for item in combined if item["speaker"] == "YOU"
        ]

    worker = TranscriptionWorker(
        transcriber=transcriber,
        chunk_duration=CHUNK_DURATION,
        overlap=OVERLAP,
        on_segments_ready=on_segments,
    )
    worker.start()

    number = 0
    while True:
        start = number * CHUNK_DURATION
        if start + window > total:
            break  # a full chunk needs 35 s buffered, same as live
        number += 1

        mic_file = Path(out_dir) / f"mic_{number:04d}.wav"
        end = start + window
        start_byte = int(start * rate * channels * 2)
        end_byte = int(end * rate * channels * 2)
        write_wav(mic_file, pcm[start_byte:end_byte], rate, channels)

        key = f"{key_prefix}chunk-{number}"
        units[key] = {"start": start, "avail": end, "mode": "chunk"}
        TIMING.event("audio_captured", key=key,
                     window_start=start, avail=end)

        # Submit -> wait for completion, so the queue wait reflects the
        # live steady state (chunks arrive 30 s apart; whisper is faster).
        worker.submit(str(system_file), str(mic_file), number)
        worker.queue.join()
        TIMING.event("cloud_sent", key=key,
                     lines=len(records.get(number, [])))

    worker.stop()
    return units, [records[n] for n in sorted(records)]


def run_vad_mode(pcm, rate, channels, transcriber, key_prefix=""):
    """Real Segmenter (Silero) + real SegmentWorker (priority queue)."""

    units = {}
    segments = []
    segmenter = Segmenter(config=SegmenterConfig())
    records = []

    worker = SegmentWorker(
        transcriber=transcriber,
        on_transcript=lambda items, key: records.append((key, items)),
    )
    worker.start()

    for frame in iter_frames(pcm, rate, channels):
        for segment in segmenter.add_frame(frame):
            segments.append(segment)

    # End of input: emit the still-open segment (live capture drops it
    # at meeting end; the verifier must account for every second).
    segments.extend(segmenter.flush())

    submitted = 0

    for index, segment in enumerate(segments, start=1):
        key = f"{key_prefix}seg-{index}"
        units[key] = {"start": segment["start"], "avail": segment["end"],
                      "mode": "vad"}
        TIMING.event("segment_ready", key=key,
                     channel=segment["channel"],
                     duration_s=round(segment["duration"], 3))

        before = worker_transcribe_completed(worker)
        before_records = len(records)
        worker.submit(segment, key)
        submitted += 1

        # Live-equivalent: whisper keeps up with segments arriving, so
        # each one is transcribed as soon as it is submitted.
        deadline = time.time() + 120
        while worker_transcribe_completed(worker) < before + 1:
            if time.time() > deadline:
                raise SystemExit(f"worker stuck waiting on {key}")
            time.sleep(0.02)

        TIMING.event("cloud_sent", key=key,
                     lines=len(records) - before_records)

    worker.stop()
    return units, records, len(segments)


def worker_transcribe_completed(worker):
    transcriber = getattr(worker, "transcriber", None)
    return getattr(transcriber, "completed", 0)


# ==============================================================
# METRICS
# ==============================================================

def load_events():
    timing_dir = Path(os.getenv("TIMING_LOG_DIR")) if os.getenv(
        "TIMING_LOG_DIR") else (Path(__file__).resolve().parent / "timings")
    files = sorted(timing_dir.glob("timings-*.jsonl"),
                   key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit(f"no timing file found in {timing_dir}")
    with open(files[-1], encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()], files[-1]


def by_key(events):
    grouped = {}
    for event in events:
        grouped.setdefault(event.get("key"), {})[event["stage"]] = event
    return grouped


def stage_metrics(units, grouped):
    """Per-unit stage deltas + end-to-end latency stats."""

    rows = []
    for key, unit in units.items():
        stages = grouped.get(key, {})
        if "transcript_ready" not in stages or "cloud_sent" not in stages:
            print(f"  (skipping {key}: incomplete events)")
            continue

        ready = stages["segment_ready"]["t_ms"] if "segment_ready" in stages \
            else stages["audio_captured"]["t_ms"]
        whisper = stages["transcript_ready"]["t_ms"]
        sent = stages["cloud_sent"]["t_ms"]

        queue_wait = (ready - stages["audio_captured"]["t_ms"]) \
            if "audio_captured" in stages and unit["mode"] == "chunk" else 0
        whisper_ms = whisper - ready
        send_ms = sent - whisper
        processing = sent - ready

        start, end, avail = unit["start"], unit["avail"], unit["avail"]
        duration = max(unit["avail"] - unit["start"], 0.001)
        struct = {
            "best": (avail - end) * 1000,
            "mean": (avail - (start + end) / 2) * 1000,
            "worst": (avail - start) * 1000,
        }

        rows.append({
            "key": key, "duration": duration,
            "queue_ms": queue_wait, "whisper_ms": whisper_ms,
            "send_ms": send_ms, "processing_ms": processing,
            "struct_ms": struct,
            "rtf": stages["transcript_ready"].get("rtf"),
            "whisper_s": stages["transcript_ready"].get("whisper_s"),
            "audio_s": stages["transcript_ready"].get("audio_s"),
            "latency_ms": {k: v + processing for k, v in struct.items()},
        })

    return rows


def weighted(rows, path):
    total = sum(row["duration"] for row in rows)
    if not total:
        return 0.0
    return sum(path(row) * row["duration"] for row in rows) / total


def mean(values):
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def print_mode(name, rows, note):
    print(f"\n--- {name} ---")
    print(f"units: {len(rows)} | {note}")
    if not rows:
        return
    print(f"  queue wait   mean {mean(r['queue_ms'] for r in rows):8.1f} ms")
    print(f"  whisper      mean {mean(r['whisper_ms'] for r in rows):8.1f} ms "
          f"max {max(r['whisper_ms'] for r in rows):8.1f} ms")
    print(f"  send         mean {mean(r['send_ms'] for r in rows):8.3f} ms")
    rtf_total = (sum(r["whisper_s"] or 0 for r in rows)
                 / max(sum(r["audio_s"] or 0 for r in rows), 1e-9))
    print(f"  RTF (overall) {rtf_total:.3f}")
    lat = lambda which: weighted(rows, lambda r: r["latency_ms"][which])
    print(f"  transcript latency (structural + processing):")
    print(f"    best  {lat('best'):9.0f} ms | "
          f"mean {lat('mean'):9.0f} ms | worst {lat('worst'):9.0f} ms")
    return rtf_total


def word_list(records):
    words = []
    for items in records:
        for item in items:
            text = re.sub(r"[^a-z0-9' ]", " ", item["text"].lower())
            words.extend(w for w in text.split() if w)
    return words


def word_spans(records):
    """(words, times) — times[i] = (start, end) of the item word i came
    from, so a diff index can be mapped back to the audio timeline."""
    words = []
    times = []
    for items in records:
        for item in items:
            text = re.sub(r"[^a-z0-9' ]", " ", item["text"].lower())
            for word in text.split():
                if word:
                    words.append(word)
                    times.append((item["start"], item["end"]))
    return words, times


def spans_cover(spans, t0, t1, pad=0.0):
    """True if any [start, end) span overlaps [t0-pad, t1+pad]."""
    return any(s - pad <= t1 and t0 <= e + pad for s, e in spans)


def classify_lost(chunk_times, only_chunk, vad_spans, pad=0.0):
    """Split lost-word indices by cause.

    Returns (inside, outside): words whose audio a VAD segment already
    covered (decode/context divergence) and words the segmenter never
    cut (a real coverage gap).
    """
    inside, outside = [], []
    for index in only_chunk:
        t0, t1 = chunk_times[index]
        target = inside if spans_cover(vad_spans, t0, t1, pad) else outside
        target.append((index, t0, t1))
    return inside, outside


def diff_words(chunk_words, vad_words):
    matcher = SequenceMatcher(None, chunk_words, vad_words, autojunk=False)
    common = sum(
        b2 - b1 for tag, a1, a2, b1, b2 in matcher.get_opcodes()
        if tag == "equal"
    )
    only_chunk = [
        i for tag, i1, i2, _, _ in matcher.get_opcodes()
        if tag in ("delete", "replace") for i in range(i1, i2)
    ]
    only_vad = [
        i for tag, _, _, i1, i2 in matcher.get_opcodes()
        if tag in ("insert", "replace") for i in range(i1, i2)
    ]
    retention = common / len(chunk_words) if chunk_words else 0.0
    return common, only_chunk, only_vad, retention


# ==============================================================
# MAIN
# ==============================================================

def main():
    default_sources = sorted(
        glob.glob(str(Path(__file__).resolve().parent / "recordings"
                      / "system_*.wav")))[:2] or sorted(
        glob.glob(str(Path(__file__).resolve().parent / "recordings"
                      / "mic_*.wav")))[:2] or [
        str(Path(__file__).resolve().parent / "recordings"
            / "phase2_speech.wav")]

    parser = argparse.ArgumentParser(
        description="Run a test WAV through both pipeline modes.")
    parser.add_argument("sources", nargs="*", default=None,
                        help="input WAV(s); default: first system (OTHERS) "
                             "recordings, which contain meeting speech")
    parser.add_argument("--model", default=os.getenv("WHISPER_MODEL",
                                                     "base.en"),
                        help="Whisper model (fast default: base.en)")
    parser.add_argument("--limit", type=float, default=DEFAULT_LIMIT_S,
                        help="max seconds of source audio (default 130)")
    args = parser.parse_args()

    sources = args.sources or default_sources
    rate, channels, pcm, used, total = load_source(sources, args.limit)
    print("=" * 70)
    print("Meeting Assistant pipeline verification (Phase 5)")
    print("=" * 70)
    print(f"source : {', '.join(os.path.basename(u) for u in used)} "
          f"({total:.1f}s, {rate} Hz, {channels} ch)")
    print(f"model  : {args.model} (int8)")

    threads = max(1, (os.cpu_count() or 2) - 2)
    base = Transcriber(model_size=args.model, device="cpu",
                       compute_type="int8", cpu_threads=threads)

    # Production wrappers: each mode uses its real decode settings.
    transcriber = CountingTranscriber(base)
    # Uniform wrappers: identical decode settings for a fair word diff.
    uniform = CountingTranscriber(UniformDecode(base))

    out_dir = tempfile.mkdtemp(prefix="verify_chunk_")
    try:
        print("\n[1/6] chunk mode, production settings ...", flush=True)
        chunk_units, chunk_records = run_chunk_mode(
            (pcm, total), rate, channels, transcriber, out_dir)

        print("[2/6] vad mode, production settings ...", flush=True)
        vad_units, vad_records, n_segments = run_vad_mode(
            pcm, rate, channels, transcriber)

        # Snapshot production timings BEFORE the uniform passes:
        # TranscriptionWorker hardcodes timing keys "chunk-N" and by_key
        # keeps the last event per stage, so pass 3 would corrupt pass 1.
        events, timing_file = load_events()

        print("[3/6] chunk mode, uniform decode (for word diff) ...",
              flush=True)
        _, chunk_uniform_records = run_chunk_mode(
            (pcm, total), rate, channels, uniform, out_dir,
            key_prefix="u-")

        print("[4/6] vad mode, uniform decode (for word diff) ...",
              flush=True)
        _, vad_uniform_records, _ = run_vad_mode(
            pcm, rate, channels, uniform, key_prefix="u-")
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)

    print("[5/6] collecting timings ...", flush=True)
    TIMING.close()
    grouped = by_key(events)

    print("[6/6] reporting\n", flush=True)
    chunk_rows = stage_metrics(chunk_units, grouped)
    vad_rows = stage_metrics(vad_units, grouped)

    rtf_chunk = print_mode("CHUNK MODE (before)", chunk_rows,
                           "30 s + 5 s overlap, beam 5, dedup")
    rtf_vad = print_mode(f"VAD MODE (after)", vad_rows,
                         f"{n_segments} segments, beam 1, priority queue")

    # ---- audio coverage (how much of the source each mode transcribed) ----
    def coverage(units):
        """Union of [start, avail) intervals in seconds (chunk windows
        overlap, so summing would double-count)."""
        spans = sorted((u["start"], u["avail"]) for u in units.values())
        total_s = 0.0
        cur_start, cur_end = spans[0] if spans else (0.0, 0.0)
        for start, end in spans[1:]:
            if start <= cur_end:
                cur_end = max(cur_end, end)
            else:
                total_s += cur_end - cur_start
                cur_start, cur_end = start, end
        if spans:
            total_s += cur_end - cur_start
        return total_s

    print(f"\naudio coverage of {total:.1f}s source: "
          f"chunk {coverage(chunk_units):.1f}s | "
          f"vad {coverage(vad_units):.1f}s "
          f"(vad counts segment spans; gaps = silence)")

    # ---- word-level diff (uniform decode on BOTH sides) ----
    chunk_words, chunk_times = word_spans(chunk_uniform_records)
    vad_words = word_list([items for _, items in vad_uniform_records])
    print("\n--- WORD DIFF (chunk vs vad, uniform decode, YOU channel) ---")
    if not chunk_words or not vad_words:
        print(f"FAIL: chunk words={len(chunk_words)} vad words={len(vad_words)}")
        if not chunk_words and not vad_words:
            print("       source appears silent — pass a WAV with speech "
                  "(see --help)")
        return 1

    common, only_chunk, only_vad, retention = diff_words(chunk_words,
                                                         vad_words)
    lost_words = [chunk_words[i] for i in only_chunk]
    extra_words = [vad_words[i] for i in only_vad]
    print(f"common {common} | only-chunk {len(lost_words)} | "
          f"only-vad {len(extra_words)} | retention {retention:.1%}")
    if lost_words:
        print(f"  lost by vad   : {' '.join(lost_words[:20])}")
    if extra_words:
        print(f"  extra in vad  : {' '.join(extra_words[:20])}")

    # Cause of each lost word: did a VAD segment cover its audio?
    #   inside  -> the segmenter heard it; decode/context divergence
    #   outside -> the segmenter never cut that audio (real gap)
    vad_spans = sorted((u["start"], u["avail"])
                       for u in vad_units.values())

    inside, outside = classify_lost(chunk_times, only_chunk, vad_spans)
    gap_indices = {index for index, _, _ in outside}

    if only_chunk:
        print(f"  lost-cause    : {len(inside)} inside segment spans "
              f"(decode divergence) | {len(outside)} outside "
              f"(real coverage gap)")
        print("  lost detail   : " + " ".join(
            f"{chunk_words[i]}@{chunk_times[i][0]:.1f}s"
            + (" [GAP]" if i in gap_indices else "")
            for i in only_chunk))
        for index, t0, t1 in outside[:10]:
            print(f"      uncovered: {chunk_words[index]!r} "
                  f"at {t0:.1f}-{t1:.1f}s")

    # Real-loss retention: words the segmenter's audio never contained.
    coverage_loss = (len(outside) / len(chunk_words)
                     if chunk_words else 0.0)
    retention_adj = 1.0 - coverage_loss
    print(f"  coverage retention (lost words outside segment spans): "
          f"{retention_adj:.1%}")

    verdict = ("PASS" if retention >= 0.95
               else "WARN" if retention >= 0.80 else "FAIL")
    print(f"VERDICT: {verdict} (retention vs chunk transcript; "
          f"cause breakdown above separates segmentation gaps from "
          f"decode divergence)")

    # ---- before/after summary (raw material for the README table) ----
    def lat(rows, which):
        return weighted(rows, lambda r: r["latency_ms"][which])

    def struct(rows):
        return weighted(rows, lambda r: r["struct_ms"]["mean"])

    def proc(rows):
        return weighted(rows, lambda r: r["processing_ms"])

    print("\n" + "=" * 70)
    print("BEFORE/AFTER — question end -> text ready at cloud")
    print("=" * 70)
    print(f"{'':34}{'chunk (before)':>16}{'vad (after)':>14}")
    print(f"{'structural wait (mean)':34}{struct(chunk_rows)/1000:15.1f}s"
          f"{struct(vad_rows)/1000:13.1f}s")
    print(f"{'+ whisper/send (mean)':34}"
          f"{proc(chunk_rows)/1000:15.1f}s"
          f"{proc(vad_rows)/1000:13.1f}s")
    print(f"{'= transcript latency (mean)':34}"
          f"{lat(chunk_rows,'mean')/1000:15.1f}s"
          f"{lat(vad_rows,'mean')/1000:13.1f}s")
    print(f"{'transcript latency (worst)':34}"
          f"{lat(chunk_rows,'worst')/1000:15.1f}s"
          f"{lat(vad_rows,'worst')/1000:13.1f}s")
    print(f"{'RTF':34}{rtf_chunk:15.3f}{rtf_vad:13.3f}")
    print(f"timing events: {timing_file}")
    print("=" * 70)

    return 0 if verdict != "FAIL" else 1


if __name__ == "__main__":
    sys.exit(main())
