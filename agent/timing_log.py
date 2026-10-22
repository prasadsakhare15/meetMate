"""Timestamped stage logging for end-to-end latency measurement (Phase 0).

Each pipeline stage logs exactly one event, in this canonical order:

    audio_captured -> segment_ready -> transcript_ready
                    -> cloud_sent -> llm_received -> ui_shown

Every event is printed to the console as ``[+  ss.sss] stage ...`` and
appended as one JSON line to ``agent/timings/timings-<start>.jsonl`` so
before/after runs can be compared. On shutdown a
``summary-<start>.json`` with per-stage deltas and Whisper real-time
factors is written to the same directory.

Configuration (read lazily on the first event, so .env values loaded by
main.py are already visible):

    TIMING_LOG=1|0      enable/disable (default 1)
    TIMING_LOG_DIR=...  output directory (default agent/timings/)
"""

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

# Canonical pipeline stages, in order. The summary reports elapsed time
# between each consecutive pair.
STAGE_ORDER = (
    "audio_captured",
    "segment_ready",
    "transcript_ready",
    "cloud_sent",
    "llm_received",
    "ui_shown",
)


def _env_enabled():
    value = os.getenv("TIMING_LOG", "1")
    return str(value).strip().lower() not in ("0", "false", "no", "off")


def _stats(values, digits=3):
    if not values:
        return None
    return {
        "n": len(values),
        "mean": round(sum(values) / len(values), digits),
        "min": round(min(values), digits),
        "max": round(max(values), digits),
    }


class TimingLog:
    """Thread-safe stage logger. Use the module-level ``TIMING`` instance."""

    def __init__(self):
        self._lock = threading.Lock()
        self._events = []
        self._file = None
        self._path = None
        self._enabled = None  # decided on first event
        self._started = time.time()

    # ===============================================================
    # RECORDING
    # ===============================================================

    def _ensure_open(self):
        """Apply config and open the JSONL file (lazy: caller holds the lock)."""

        if self._enabled is not None:
            return self._enabled

        self._enabled = _env_enabled()

        if self._enabled:
            directory = Path(
                os.getenv("TIMING_LOG_DIR")
                or (Path(__file__).resolve().parent / "timings")
            )
            directory.mkdir(parents=True, exist_ok=True)

            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self._path = directory / f"timings-{stamp}.jsonl"
            self._file = open(self._path, "a", encoding="utf-8")

        return self._enabled

    def event(self, stage, key=None, **fields):
        """Record one pipeline stage event. Safe to call from any thread."""

        with self._lock:

            if not self._ensure_open():
                return

            now = time.time()

            record = {
                "stage": stage,
                "ts_ms": int(now * 1000),          # epoch: cross-process compare
                "t_ms": int((now - self._started) * 1000),  # since agent start
                "iso": datetime.fromtimestamp(now).isoformat(
                    timespec="milliseconds"
                ),
            }

            if key is not None:
                record["key"] = key

            record.update(fields)

            self._events.append(record)
            self._file.write(json.dumps(record) + "\n")
            self._file.flush()

            print(self._format(record), flush=True)

    @staticmethod
    def _format(record):

        line = f"[+{record['t_ms'] / 1000:8.3f}s] {record['stage']:<16}"

        if record.get("key"):
            line += f" {record['key']}"

        for name, value in record.items():
            if name in ("stage", "ts_ms", "t_ms", "iso", "key"):
                continue
            line += f" {name}={value}"

        return line

    # ===============================================================
    # SUMMARY / SHUTDOWN
    # ===============================================================

    def close(self):
        """Write the summary file and stop logging. Safe to call twice."""

        with self._lock:

            if self._file is None:
                return

            summary = self._summarize()

            summary_path = self._path.with_name(
                self._path.name
                .replace("timings-", "summary-")
                .replace(".jsonl", ".json")
            )
            summary_path.write_text(
                json.dumps(summary, indent=2),
                encoding="utf-8",
            )

            self._file.close()
            self._file = None
            self._enabled = False

            self._print_summary(summary, summary_path)

    def _summarize(self):

        stage_index = {stage: i for i, stage in enumerate(STAGE_ORDER)}

        counts = {}
        deltas = {}          # "stage_a->stage_b" -> [ms, ...]
        rtf_others = []
        rtf_you = []
        keyed = {}           # key -> [records in arrival order]

        # Nearest preceding events for the suggestion chain (those events
        # carry no chunk key).
        prev_sent_ms = None
        prev_llm_ms = None

        def add_delta(label, ms):
            deltas.setdefault(label, []).append(round(ms, 3))

        for record in sorted(self._events, key=lambda r: r["ts_ms"]):

            stage = record["stage"]
            counts[stage] = counts.get(stage, 0) + 1

            if stage == "transcript_ready":
                if "rtf_others" in record:
                    rtf_others.append(record["rtf_others"])
                if "rtf_you" in record:
                    rtf_you.append(record["rtf_you"])

            # Suggestion chain: pair with the nearest preceding event of
            # the previous stage, whatever its key.
            if stage == "cloud_sent":
                prev_sent_ms = record["ts_ms"]
            elif stage == "llm_received":
                if prev_sent_ms is not None:
                    add_delta(
                        "cloud_sent->llm_received",
                        record["ts_ms"] - prev_sent_ms,
                    )
                prev_llm_ms = record["ts_ms"]
            elif stage == "ui_shown" and prev_llm_ms is not None:
                add_delta(
                    "llm_received->ui_shown",
                    record["ts_ms"] - prev_llm_ms,
                )

            key = record.get("key")

            if key is None:
                continue

            group = keyed.setdefault(key, [])

            if group:
                previous = group[-1]
                if stage_index.get(stage) == stage_index.get(previous["stage"], -99) + 1:
                    add_delta(
                        f"{previous['stage']}->{stage}",
                        record["ts_ms"] - previous["ts_ms"],
                    )

            group.append(record)

        return {
            "start_iso": datetime.fromtimestamp(self._started).isoformat(
                timespec="seconds"
            ),
            "end_iso": datetime.now().isoformat(timespec="seconds"),
            "events": len(self._events),
            "counts": counts,
            "deltas": {
                label: _stats(values)
                for label, values in sorted(deltas.items())
            },
            "rtf_others": _stats(rtf_others),
            "rtf_you": _stats(rtf_you),
        }

    @staticmethod
    def _print_summary(summary, summary_path):

        print("\n" + "=" * 70)
        print("TIMING SUMMARY")
        print("=" * 70)

        for stage in STAGE_ORDER:
            print(f"  {stage:<18} n={summary['counts'].get(stage, 0)}")

        print()

        for label, stats in summary["deltas"].items():
            if not stats:
                continue
            print(
                f"  {label:<34} mean={stats['mean']:>9.1f} ms  "
                f"min={stats['min']:>9.1f}  max={stats['max']:>9.1f}  "
                f"n={stats['n']}"
            )

        for channel, stats in (
            ("others", summary["rtf_others"]),
            ("you", summary["rtf_you"]),
        ):
            if stats:
                print(
                    f"  RTF {channel:<30} mean={stats['mean']:.3f}  "
                    f"min={stats['min']:.3f}  max={stats['max']:.3f}  "
                    f"n={stats['n']}"
                )

        print("=" * 70)
        print(f"Saved: {summary_path}")
        print("=" * 70 + "\n")


# Shared instance used by the agent.
TIMING = TimingLog()
