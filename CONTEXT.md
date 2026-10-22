# Meeting Assistant — Coding Context

> Working context for coding sessions. **Update this file at the end of every
> phase** (status, decisions, verification results). Last updated: after Phase 8 (HELP ME Cut & Drain).

## Project shape & hard rules

- Three parts: `agent/` (local Python: capture, VAD, Whisper, WS bridges),
  `desktop/` (Electron floating UI), `server/` (FastAPI WS: context, LLM via Groq).
- **Privacy rule (never change):** raw audio never leaves the machine; only
  transcribed text goes to the cloud.
- **WebSocket message format: unchanged so far** — keep it that way; if a phase
  must change it, state exactly what changed.
- Constraints: CPU-only, Windows/WASAPI primary, Faster-Whisper `int8`,
  keep old chunk path working behind `SEGMENTATION_MODE=chunk|vad` (default
  `chunk`) for before/after comparison, small focused changes only, no new
  dependencies without saying why.
- Session rules: read relevant files first, present a short plan before coding,
  give exact commands + expected output after each phase, **stop after each
  phase and wait for "next phase"**, stop and ask if code conflicts with the plan.

## Phase progress

| Phase | Status | Landed |
|---|---|---|
| 0 Timing logs | ✅ done | `agent/timing_log.py` + stage events in main/worker/cloud_client; server `[timing]` prints; renderer `[timing] ui_shown`; output in `agent/timings/` (gitignored) |
| 1 Small frames | ✅ done | `audio_capture.py` `mode=chunk\|vad`, 30 ms frames (YOU=mic, OTHERS=loopback) → bounded `frame_queue` (1000, drop-oldest); `SEGMENTATION_MODE` in `main.py` |
| 2 VAD segmenter | ✅ done | `agent/segmenter.py` (Silero via faster-whisper's bundled ONNX + onnxruntime, **zero new deps**); wired in `_process_vad_frames` |
| 3 Queue + Whisper worker | ✅ done | `agent/segment_worker.py` (OTHERS-priority queue, tuned single worker, same-channel prompt, dedup safety net); per-segment cloud send; meeting-relative timestamps; `WHISPER_CPU_THREADS` |
| 4 Faster server triggers | ✅ done | `session.py`: ~1 s merge window (`_auto_suggest_after_wait` task, extend-cap), cooldown override for clearly-new questions, HELP ME cancels pending auto; `meeting_registry.py`: persisted `last_auto_question`; pre-existing `__aexit__` bug fixed |
| 5 Verify + README how-it-works | ✅ done | `agent/verify_pipeline.py` (both modes on real audio, per-stage latency/RTF/word-diff verdict); `segmenter.flush()` for end-of-input; README "How it works" + measured before/after table |
| 6 End-of-meeting correctness | ✅ done | `stop_capture` flushes the open segment and drains the worker before `END_MEETING`; `SegmentWorker.drain()`/`busy` + in-flight tracking; both workers join long enough to finish the in-flight transcript; `agent/test_pipeline.py` (12 asserts, no new deps) |
| 7 Password Mgmt & Docker | ✅ done | `server/users.py` (`update_password`), `server/main.py` (`/auth/update-password`), `agent/auth_client.py` (`update_password`), Electron UI change password modal/controls; `server/Dockerfile` + `.dockerignore` |
| 8 HELP ME Cut & Drain | ✅ done | `agent/main.py`: `_flush_and_request_help` cuts active speech segment on `HELP_ME`, drains Whisper queue (up to 3s), sends transcript before cloud `HELP_ME` request; `agent/test_pipeline.py` (13 asserts, 13/13 passing) |

## Current pipeline

**chunk (default, "before" path):** 30 s + 5 s overlap WAVs →
`TranscriptionWorker` (beam_size=5, vad_filter, condition_on_previous_text=True —
**kept byte-identical as the comparison baseline**) → dedup on overlap →
`SEGMENTS` to cloud → server auto-suggests on OTHERS question behind a **45 s
cooldown** (`AUTO_SUGGEST_COOLDOWN` in `server/session.py`).

**vad (now fully working):** 30 ms frames → `Segmenter` (250 ms pre-roll, close
after 600 ms silence / ≥250 ms post-pad, 12 s max → 2 s lookahead → lowest-RMS
cut → 1 s overlap carry) → `_on_vad_segment` (logs `segment_ready`, converts to
meeting-relative time, submits) → `SegmentWorker` priority queue (OTHERS first)
→ single Whisper thread (beam 1, no condition, same-channel `initial_prompt`,
vad_filter off, `WHISPER_CPU_THREADS` = cores−2) → `on_vad_transcript` →
`send_segments(items, key=seg-N)` **immediately** per segment → server
auto-suggests as before. Phase-0 chain per segment:
`segment_ready → transcript_ready → cloud_sent`.

## Key decisions (with why)

- **Silero v5, not webrtcvad:** faster-whisper 1.1.0 already ships
  `faster_whisper/assets/silero_{encoder,decoder}_v5.onnx` and `onnxruntime`
  (1.29) is already installed → no new package, no download. Scorer proven
  **bit-identical** (max diff 0.00000) to faster-whisper's own model call.
- **Streaming scorer keeps per-channel decoder state** (state + prev-window
  64-sample context), replicating `faster_whisper.vad.SileroVADModel.__call__`.
- **Analysis vs storage:** only the VAD analysis copy is resampled to 16 kHz
  mono (`frame_to_16k_mono`, reused by the worker); segment audio stays
  native-rate PCM.
- **Meeting-relative timestamps (Phase 3):** the server renders `mm:ss` via
  `seconds // 60` (`MeetingContext._format_time`, `TranscriptStore`), so epochs
  would show as `[29333333:47]`. `_meeting_epoch` is reset in `start_capture`
  and subtracted in `_on_vad_segment` (chunks already send capture-relative).
- **Chunk transcription params untouched** (beam 5 / condition True) so the
  before/after comparison stays honest; `cpu_threads` is model-wide and only
  affects speed. `Transcriber.transcribe()` now takes keyword params whose
  *defaults equal the old behaviour* — chunk call sites unchanged.
- **Queue policy (Phase 3):** OTHERS dequeued before YOU, FIFO within channel;
  bound 100 — overflow evicts oldest YOU (then oldest OTHERS); an incoming YOU
  loses when only questions are pending. `stop()` discards the backlog (meeting
  is over) and clears memory.
- **Worker stopped before `END_MEETING`** in `stop_capture` so an in-flight
  segment still lands inside the meeting (join timeout 10 s; daemon thread).
- **Config via env, fail-loud:** invalid `SEGMENTATION_MODE`/`VAD_*` abort with
  a clear `SystemExit`.
- Segmenter loads in `start_capture` (vad) *before* streams open; fresh
  segmenter+worker per capture session; 5 consecutive VAD errors → VAD disabled
  + UI error instead of crashing the agent.
- `cloud_client.send_segments(segments, key=None)` — internal param for timing
  correlation only; wire format untouched.
- **HELP ME flushes the segmenter (Phase 8):** before, `HELP_ME` just forwarded
  to cloud — the LLM only saw transcripts already sent, missing whatever was
  still in the open VAD buffer. Now `_flush_and_request_help` drains pending
  frames, calls `segmenter.flush()`, submits the cut segment, and
  `worker.drain(timeout=3.0)` before `send_help_me()`. The segmenter lock
  (`_segmenter_lock`) serialises this with `_process_vad_frames` and
  `stop_capture`.
- **Drain timeout of 3 s on HELP ME** (vs 15 s on stop): HELP ME should feel
  instant; if the worker is still busy after 3 s the request goes through
  anyway with whatever context the cloud has — the user does not wait forever.
  `stop_capture` keeps 15 s because correctness (no dropped tail) matters more
  than latency at meeting-end.

## Environment variables (agent/.env or shell)

`CLOUD_WS_URL`, `WHISPER_MODEL` (default `small`; measured segment-path RTF on
this 12-core machine: **`base.en` 0.149, `distil-small.en` 0.279, `base` 0.184,
`small` 0.583** — `base.en` recommended for English, all four verified with
4/4 keyword hits; `base.en`/`distil-small.en` download to the HF cache on first
use), `WHISPER_DEVICE=cpu`,
`WHISPER_COMPUTE=int8`, `WHISPER_CPU_THREADS` (default cores−2 → 10 here),
`SEGMENTATION_MODE=chunk|vad`, `TIMING_LOG=1`, `TIMING_LOG_DIR`,
server: `AUTO_SUGGEST_COOLDOWN=45`, `AUTO_SUGGEST_WAIT_S=1`,
`AUTO_SUGGEST_MAX_WAIT_S=3`, `AUTO_SUGGEST_MIN_INTERVAL_S=5` (all read in
`MeetingSession.__init__` — dotenv runs after module import),
`VAD_THRESHOLD=0.5`, `VAD_SILENCE_MS=600`, `VAD_PRE_PAD_MS=250`,
`VAD_POST_PAD_MS=250`, `VAD_MAX_SEGMENT_S=12`, `VAD_CUT_LOOKAHEAD_S=2`,
`VAD_OVERLAP_S=1`, `SILERO_VAD_PATH` (optional override).

## File map (agent/)

- `main.py` — `MeetingAgent`; mode flag; `_process_vad_frames` /
  `_on_vad_segment` (epoch→relative, `worker.submit(segment, key)`);
  `on_vad_transcript` → cloud; per-mode worker creation in `start_capture`;
  `_meeting_epoch`; `_flush_and_request_help` (Phase 8: cut + drain + help_me
  in a daemon thread); `_segmenter_lock` guards segmenter across main loop,
  stop_capture, and help_me threads.
- `segment_worker.py` — `_SegmentQueue` + `SegmentWorker` (Phase 3): tuning
  kwargs, same-channel `previous_text` prompt chain, dedup safety net only on
  `segment.start < previous_end[channel]`, `transcript_ready` timing event.
- `segmenter.py` — `SegmenterConfig` / `SileroScorer` / `ChannelSegmenter` /
  `Segmenter` facade (incl. Phase 5 `flush()`: emit the still-open segment at
  end-of-input) / `frame_to_16k_mono` / `frame_rms`.
- `verify_pipeline.py` — Phase 5 verifier: 4 passes (production pair for
  latency, uniform-decode pair for the word diff), per-stage latency, RTF,
  word-level diff with a lost-word cause breakdown, PASS/WARN/FAIL exit code.
- `audio_capture.py` — WASAPI dual capture; chunk path + vad frame emission.
- `transcriber.py` — `cpu_threads` ctor param; `transcribe(audio, beam_size=5,
  vad_filter=True, condition_on_previous_text=True, initial_prompt=None)`
  (defaults = old chunk behaviour; accepts file path or 16 kHz float32 array).
- `transcription_worker.py` — chunk worker (untouched behaviour).
- `dedup.py` — chunk overlap dedup + Phase 3 forced-cut safety net.
- `cloud_client.py` — WS + offline buffer; `send_segments(key=)` logs `cloud_sent`.
- `auth_client.py` — HTTP client handling `/auth/login`, `/auth/register`, and `/auth/update-password` with the cloud server.
- `timing_log.py` — `TIMING.event(stage, key=..., **fields)`; summary at close.
- Server: `main.py` (FastAPI routes: `/auth/*`, `/ws`), `users.py` (SQLite/Postgres user repository with PBKDF2-HMAC password hashing, JWT issue, `update_password`), `session.py` (`_handle_segments` → `_should_auto_suggest` gate →
  `_queue_auto_suggest` → `_auto_suggest_after_wait` task →
  `_generate_and_send_suggestion`; `_handle_help_me` cancels pending auto;
  `_similar_question` helper; `[timing]` prints), `meeting_registry.py`
  (persisted `last_auto_suggest` + `last_auto_question`),
  `conversation_processor.py` (`find_questions`), `meeting_context.py`
  (5-min window + summaries, `_format_time` — why timestamps are relative),
  `llm_client.py` (Groq; `LLMClient()` raises without `GROQ_API_KEY` — tests
  set a dummy and swap in a fake), `Dockerfile` (containerized server setup).

## Phase 5 — how the pipeline is verified

`agent/verify_pipeline.py` is the one-shot proof that the new path is faster
without losing speech. It runs **four passes** over one recording
(`agent/recordings/system_*.wav` — real meeting speech, not the near-silent
mic captures):

| Pass | Mode | Decode settings | Purpose |
|---|---|---|---|
| 1 | chunk | production (beam 5 / condition-on / vad_filter) | latency baseline |
| 2 | vad | production (beam 1 / same-channel prompt) | latency after |
| 3 | chunk | uniform (beam 1, no condition, no prompt) | word-diff reference |
| 4 | vad | uniform (same) | word-diff candidate |

Both modes run the **real** slicing, segmenter, worker and dedup code; only
Whisper decode settings are swapped, via `UniformDecode`. The word diff uses
uniform settings on *both* sides on purpose — comparing chunk-beam-5 against
segment-beam-1 conflates segmentation loss with decoder phrasing and produced a
misleading 79.9% "FAIL" in an earlier run.

Measured (130 s source, `base.en`, int8, 12 cores):

- chunk: structural wait 17.5 s + 3.9 s processing = **21.4 s mean**, 38.9 s worst, RTF 0.111
- vad: 26 segments, structural wait 3.6 s + 0.9 s processing = **4.5 s mean**, 8.2 s worst, RTF 0.182
- word diff: 92.2% retention (WARN) — but **all 20 differing words fall inside
  segment spans** (decode divergence: `et cetera`↔`etc`, `tie`↔`thai`),
  0 words outside, i.e. no coverage gap. Coverage retention 100%.
- VAD's higher RTF is fine: it decodes only speech, and per-window decode time
  drops 3.9 s → 0.9 s, so total latency still improves ~5x.

Thresholds are unchanged and honest: PASS ≥95%, WARN ≥80%, FAIL <80%. The cause
breakdown (inside vs outside a segment span) is a **diagnostic only** — it never
upgrades a verdict.

## Phase 6 — end-of-meeting correctness (what changed and why)

Three real gaps closed, all found by reading the stop path rather than by a
failing test:

- **Tail speech was dropped.** The open VAD segment never receives the closing
  silence, so `stop_capture` discarded it. It now calls `segmenter.flush()`
  and submits each segment through `_on_vad_segment` (keeps meeting-relative
  timestamps and the `segment_ready` timing event), then drains the worker for
  up to `DRAIN_TIMEOUT_S` (15 s) **before** `end_meeting()`.
- **A transcript could be sent after `END_MEETING`.** Both workers joined with
  `timeout=10`, but a 35 s chunk at `small`-model RTF 0.58 takes ~20 s and a
  13 s VAD segment at high RTF is similar — the thread outlived the join and
  sent late. `SegmentWorker` now tracks `_inflight` (exposes `busy` and
  `drain(timeout)`) and both `stop()`s join with 60 s and print a warning if
  the thread is still alive. Queued backlog is still discarded (meeting is
  over); only the in-flight segment is guaranteed to land.
- **The 5-strike VAD breaker was never fault-injected.** Now covered.

`verify_pipeline.py`'s `spans_cover()` / `classify_lost()` were lifted out of
`main()` so the loss-cause classification is unit-testable.

`agent/test_pipeline.py`: 12 asserts, no new dependency (pytest is pinned in
`requirements-dev.txt` but not installed, so the file is a plain script —
`python test_pipeline.py`, exit 0/1 — with `test_*` functions that pytest can
collect if it ever is). Uses a scripted stub scorer, so it runs in ~2 s with
no Whisper and no ONNX.

Two of those tests initially failed and the *fix was to the test*, not the
code: one used `worker.thread` instead of `_thread`, the other asserted that a
still-*queued* segment survives `stop()` — discarding the backlog is the
documented design, so the test was rewritten to assert the real guarantee (the
in-flight segment is sent before `stop()` returns, and nothing arrives after).

## Verification (all green as of Phase 8)

- Compile + `node --check renderer.js`; no stale references after refactors.
- Phase 1 live: vad frames exactly 30.0 ms both channels, 0 dropped; chunk path
  unchanged.
- Phase 2: scorer parity 0.00000 vs faster-whisper's model; scripted
  pre-roll/close exact; forced cut at the quiet frame, 1.02 s overlap; stereo
  OTHERS path; post/pre-pad configs honored; env validation.
- Phase 3 unit: queue priority/FIFO/wake/stop; prompt chain
  (YOU→YOU, OTHERS→OTHERS, never cross); tuning kwargs on every call; dedup
  drops only the overlapping duplicate (keys A,B,C,E — D dropped); eviction
  policy at capacity; transcriber signature defaults == old behaviour; timing
  file has `transcript_ready` per key.
- Phase 3 real Whisper: both paths work on the TTS recording;
  `base` segment RTF **0.184** (chunk 2.47 s vs segment 1.72 s), `small` RTF
  **0.583**, 4/4 keyword hits each.
- Phase 3 integration (real devices, fake Whisper): vad → `SegmentWorker`
  wired, both segments sent with `seg-1`/`seg-2`, timestamps `< 60` (relative),
  stage chain `segment_ready → transcript_ready → cloud_sent` ascending per
  key, clean stop; chunk → `TranscriptionWorker` + `chunk_ready()` gating intact.
- Phase 4 (fake WS + fake LLM, real session/registry/context): merge window
  fires once after the wait with follow-up OTHERS speech in the LLM context;
  burst guard blocks <5 s; similarity blocks same-question re-asks; clearly-new
  question overrides cooldown (log line asserted); HELP ME answers with fresh
  cooldown AND cancels a pending auto (exactly 3 SUGGESTIONs); 5-min window
  ages segments → summary bullet appears in `build_context`; persistence
  round-trip; server import chain OK.
- **Pre-existing bug fixed:** `MeetingSession.__aexit__` called `self._detach()`
  which never existed (only `_detach_sync`) — every disconnect raised
  AttributeError; now calls `_detach_sync()` (idempotent).
- Test assets (all gitignored): `agent/recordings/system_*.wav` (real meeting
  speech — the default `verify_pipeline.py` input), `mic_*.wav` (many near-
  silent), `phase2_speech.wav` (TTS, 9.36 s, 22050 Hz — Phase 2-3).
- Phase 6 (`python test_pipeline.py`, 12/12, exit 0): flush is empty without
  frames, emits the open segment and resets, and works end-to-end through
  `Segmenter` on both channels; `drain()` waits for queued + in-flight;
  `stop()` returns only after the in-flight transcript is sent (SegmentWorker
  and TranscriptionWorker) with no late send afterwards; `stop_capture` sends
  the flushed tail *before* `END_MEETING` with meeting-relative timestamps and
  leaves the chunk path free of flush/drain; VAD breaker disables after 5
  consecutive errors, emits one UI error, and a success resets the counter;
  `UniformDecode` overrides all four decode kwargs; `spans_cover`/
  `classify_lost` classify inside vs gap.
- Phase 6 regression: `verify_pipeline.py` re-run after the worker changes —
  exit 0, verdict WARN, retention 92.2%, 0 coverage gaps, 26 segments,
  structural waits unchanged (17.5 s / 3.6 s).
- Phase 8 (`python test_pipeline.py`, 13/13, exit 0): all Phase 6 tests still
  pass; new `test_help_me_flushes_and_drains_before_cloud_send` verifies that
  `_flush_and_request_help` flushes the segmenter, drains the worker, sends the
  transcript segment to cloud, and only then sends `help_me` — ordering
  asserted via `cloud.events.index`.
- Phase 0 baseline for the before/after table: `agent/timings/*.jsonl` +
  `summary-*.json` (gitignored — regenerate if cleaned).

## Remaining phases

None planned — all phases are done. Re-run the checks any time with:

```powershell
cd agent && venv\Scripts\activate && python test_pipeline.py && python verify_pipeline.py
```

## Known gaps / gotchas

- `verify_pipeline.py` snapshots the timing file **after** pass 2 on purpose:
  `TranscriptionWorker._process_chunk` hardcodes timing keys `chunk-N`, so the
  uniform pass would overwrite the production pass's `segment_ready` /
  `transcript_ready` events (last event per stage wins) and corrupt the chunk
  latency table.

- Live vad session with real speech → real Whisper → real cloud not yet
  observed end-to-end (unit + integration with fake Whisper passed; steps given
  to the user).
- `DRAIN_TIMEOUT_S = 15.0` in `main.py` is a module constant, not an env var:
  if stop ever needs to wait longer than 15 s for the queue, make it
  configurable.
- If a worker thread ever outlives `stop()` (60 s join), it is a daemon and
  can still send after `END_MEETING`; the code now prints a loud warning in
  that case but does not prevent it.
- Long bash heredocs have occasionally eaten a script's trailing marker line —
  prefer short scripts or a temp test file; always assert exit codes.
- Windows/bash: use POSIX syntax, forward slashes; Python is
  `agent/venv/Scripts/python.exe`.
