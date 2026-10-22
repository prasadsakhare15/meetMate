import json
import os
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

import auth_client
from audio_capture import DualAudioCapture
from cloud_client import CloudClient
from segmenter import Segmenter
from segment_worker import SegmentWorker
from transcriber import Transcriber
from timing_log import TIMING
from transcription_worker import TranscriptionWorker
from ui_server import UIServer


load_dotenv(Path(__file__).resolve().parent / ".env")

# "chunk" = fixed 30 s chunks (original path).
# "vad"   = ~30 ms frames + VAD segmentation (Phases 2-3).
SEGMENTATION_MODE = os.getenv("SEGMENTATION_MODE", "chunk").strip().lower()

if SEGMENTATION_MODE not in ("chunk", "vad"):
    raise SystemExit(
        f"Invalid SEGMENTATION_MODE={SEGMENTATION_MODE!r} — "
        f"use 'chunk' or 'vad'."
    )

CHUNK_DURATION = 30
OVERLAP = 5

# At end of meeting the worker is given this long to finish the tail
# segment before the pending backlog is discarded.
DRAIN_TIMEOUT_S = 15.0


class MeetingAgent:

    def __init__(self):

        self.capture = DualAudioCapture(
            chunk_duration=CHUNK_DURATION,
            overlap=OVERLAP,
            mode=SEGMENTATION_MODE,
        )

        self.ui_server = UIServer()
        self.cloud_client = CloudClient()
        self.transcriber = None
        self.worker = None

        self._transcriber_lock = threading.Lock()
        self._segmenter_lock = threading.Lock()
        self._running = True

        # Phase 1: vad-mode frame accounting (reported every ~10 s).
        self._vad_counts = {"YOU": 0, "OTHERS": 0}
        self._vad_report_start = time.monotonic()

        # Phase 2/3: VAD segmenter (vad mode only), segment numbering,
        # and error bookkeeping; the transcription backlog lives in the
        # worker's priority queue.
        self.segmenter = None
        self._vad_segment_number = 0
        self._vad_errors = 0

        # Meeting-relative time base for sent timestamps (the server
        # renders mm:ss from segment["start"] — epochs would break it).
        self._meeting_epoch = time.time()

        # False until the desktop UI signs in; all UI pushes except auth
        # messages wait for it.
        self.authenticated = False

    def start(self):

        self.ui_server.set_message_callback(self.handle_ui_message)
        self.ui_server.start()

        self.cloud_client.set_message_callback(self.handle_cloud_message)
        self.cloud_client.start()

        self._send_status("Loading Whisper...")
        threading.Thread(
            target=self._load_transcriber,
            name="WhisperLoader",
            daemon=True,
        ).start()

        print("\nLocal agent ready. Waiting for Electron UI.")
        print(f"Cloud backend : {self.cloud_client.url}")
        print(f"Segmentation  : {SEGMENTATION_MODE}")
        print("Select devices in the desktop app, then start capture.")
        print("Press Ctrl+C to stop.\n")

        try:
            while self._running:
                if SEGMENTATION_MODE == "vad":
                    self._process_vad_frames()
                    time.sleep(0.05)
                    continue

                if (
                    self.capture.capture_started
                    and self.worker is not None
                    and self.capture.chunk_ready()
                ):
                    result = self.capture.record_chunk()

                    if result is not None:
                        system_file, mic_file, chunk_number = result
                        TIMING.event(
                            "audio_captured",
                            key=f"chunk-{chunk_number}",
                            chunk=chunk_number,
                        )
                        print(
                            f"\n========== CHUNK {chunk_number} queued =========="
                        )
                        self.worker.submit(
                            system_file,
                            mic_file,
                            chunk_number,
                        )
                else:
                    time.sleep(0.1)

        except KeyboardInterrupt:
            print("\n\nCtrl+C detected.")

        finally:
            self.shutdown()

    def _load_transcriber(self):

        try:
            # Explicit thread budget: leave 1-2 cores for capture/VAD/UI.
            cpu_threads = int(os.getenv("WHISPER_CPU_THREADS", "0") or 0)
            if cpu_threads <= 0:
                cpu_threads = max(1, (os.cpu_count() or 2) - 2)

            transcriber = Transcriber(
                model_size=os.getenv("WHISPER_MODEL", "small"),
                device=os.getenv("WHISPER_DEVICE", "cpu"),
                compute_type=os.getenv("WHISPER_COMPUTE", "int8"),
                cpu_threads=cpu_threads,
            )

            with self._transcriber_lock:
                self.transcriber = transcriber

            self._send_status("Ready")
            self.send_devices()
            print(f"Whisper model ready ({cpu_threads} CPU threads).")

        except Exception as e:
            print(f"Failed to load Whisper: {e}")
            self._send_error(f"Whisper failed to load: {e}")

    def handle_ui_message(self, message):

        msg_type = message.get("type")

        if msg_type in ("UI_CONNECTED", "LIST_DEVICES"):
            self.send_devices()
            return

        if msg_type == "START_CAPTURE":
            self.start_capture(
                message.get("output_index"),
                message.get("mic_index"),
            )
            return

        if msg_type == "STOP_CAPTURE":
            self.stop_capture()
            return

        if msg_type == "AUTH":
            self._handle_auth(message)
            return

        if msg_type == "HELP_ME":

            if not self.cloud_client.meeting_id:
                self._send_error("Start the meeting first.")
                return

            threading.Thread(
                target=self._flush_and_request_help,
                name="HelpMeHandler",
                daemon=True,
            ).start()
            return

    def _flush_and_request_help(self):
        """Cut and flush open audio in the VAD segmenter on HELP_ME,
        wait for the worker to finish transcribing, then ask cloud for suggestions."""
        if not self.cloud_client.meeting_id:
            self._send_error("Start the meeting first.")
            return

        print("\n" + "=" * 70)
        print("HELP ME requested")
        print("=" * 70)

        # Notify UI immediately that thinking has started
        self._send_status("Thinking...")

        # If in VAD mode and capturing, cut and drain whatever speech is in flight
        if SEGMENTATION_MODE == "vad" and getattr(self.capture, "capture_started", False):
            lock = getattr(self, "_segmenter_lock", None)
            if lock is None:
                self._segmenter_lock = threading.Lock()
                lock = self._segmenter_lock

            with lock:
                if self.segmenter is not None:
                    # Drain any frames just captured
                    if hasattr(self.capture, "drain_frames"):
                        try:
                            frames = self.capture.drain_frames(max_items=512)
                            for frame in frames:
                                closed = self.segmenter.add_frame(frame)
                                for segment in closed:
                                    self._on_vad_segment(segment)
                        except Exception as e:
                            print(f"VAD error during help drain: {e}")

                    # Cut open segment right now
                    try:
                        flushed = self.segmenter.flush()
                        for segment in flushed:
                            print(
                                f"[help_me] Cut & flush segment: {segment['channel']} "
                                f"({segment['duration']:.2f}s)"
                            )
                            self._on_vad_segment(segment)
                    except Exception as e:
                        print(f"VAD flush error on HELP_ME: {e}")

            # Wait for worker to finish transcribing the cut segment (up to 3.0s)
            if self.worker is not None and isinstance(self.worker, SegmentWorker):
                drained = self.worker.drain(timeout=3.0)
                if not drained:
                    print("[help_me] Drain timeout; sending HELP_ME with available context.")

        print("Forwarding HELP ME to cloud...")
        self.cloud_client.send_help_me()

    def handle_cloud_message(self, message):

        msg_type = message.get("type")

        if msg_type == "CLOUD_STATUS":
            state = message.get("text", "disconnected")
            print(f"Cloud backend: {state}")

            if state == "unauthorized":
                self.authenticated = False
                self._send_to_ui({"type": "AUTH_REQUIRED"})
            return

        if msg_type == "MEETING_STARTED":
            print(
                f"Cloud meeting {'resumed' if message.get('resumed') else 'started'}"
                f" ({message.get('segment_count', 0)} segments so far)."
            )
            self._send_to_ui(message)
            return

        if msg_type in ("SUGGESTION", "STATUS", "ERROR"):
            if (
                msg_type == "STATUS"
                and not self.capture.capture_started
                and str(message.get("text", "")).upper() == "LISTENING"
            ):
                return
            print(f"Forwarding {msg_type} to Electron UI...")

            if msg_type == "SUGGESTION":
                TIMING.event("llm_received")
                print("  question:", message.get("question", ""))
                print("  answer  :", message.get("answer", ""))

            self._send_to_ui(message)
            return

        if msg_type == "CHUNK_PROCESSED":
            print("\n" + "=" * 70)
            print("CLEAN CONVERSATION")
            print("=" * 70)
            print(message.get("conversation", ""))
            print("=" * 70)

    # =================================================
    # AUTHENTICATION
    # =================================================

    def _handle_auth(self, message):

        action = message.get("action")
        username = str(message.get("username", "")).strip()
        password = str(message.get("password", ""))

        if action not in ("login", "register", "update_password"):
            self._send_to_ui({
                "type": "AUTH_RESULT",
                "ok": False,
                "error": "Unknown auth action.",
            })
            return

        if not username or not password:
            self._send_to_ui({
                "type": "AUTH_RESULT",
                "ok": False,
                "error": "Username and password are required.",
            })
            return

        try:
            if action == "login":
                response = auth_client.login(username, password)
            elif action == "register":
                response = auth_client.register(username, password)
            elif action == "update_password":
                new_password = str(message.get("new_password", ""))
                if not new_password:
                    self._send_to_ui({
                        "type": "AUTH_RESULT",
                        "ok": False,
                        "error": "New password is required.",
                    })
                    return
                auth_client.update_password(username, password, new_password)
                self._send_to_ui({
                    "type": "AUTH_RESULT",
                    "ok": True,
                    "action": "update_password",
                })
                return

        except auth_client.AuthError as e:
            print(f"Auth failed: {e}")
            self._send_to_ui({
                "type": "AUTH_RESULT",
                "ok": False,
                "error": str(e),
            })
            return

        print(f"Signed in as {response.get('username', username)}.")

        self.authenticated = True
        self.cloud_client.set_token(response.get("token"))

        self._send_to_ui({
            "type": "AUTH_RESULT",
            "ok": True,
            "username": response.get("username", username),
        })

    # =================================================
    # UI
    # =================================================

    def _send_to_ui(self, message):
        """Push a message to the desktop UI.

        Before login, only auth-related messages are delivered; anything else
        (Whisper status, device lists) is held in the UI server's queue so it
        reaches the app once the user is signed in.
        """

        if message.get("type") == "SUGGESTION":
            # queued=True means it is held until the desktop UI signs in.
            TIMING.event("ui_shown", queued=bool(not self.authenticated))

        if not self.authenticated and message.get("type") not in (
            "AUTH_RESULT",
            "AUTH_REQUIRED",
        ):
            self.ui_server.pending.append(json.dumps(message))
            return

        self.ui_server.send(message)

    def send_devices(self):

        self._send_to_ui({
            "type": "DEVICES",
            "outputs": self.capture.serialize_output_devices(),
            "microphones": self.capture.serialize_microphones(),
            "capturing": self.capture.capture_started,
            "whisper_ready": self.transcriber is not None,
        })

    def start_capture(self, output_index, mic_index):

        if self.capture.capture_started:
            self._send_error("Capture is already running.")
            return

        if output_index is None or mic_index is None:
            self._send_error("Select speakers and a microphone first.")
            return

        with self._transcriber_lock:
            transcriber = self.transcriber

        if transcriber is None:
            self._send_status("Loading Whisper...")
            return

        try:
            system_device = self.capture.resolve_loopback(output_index)
            microphone_device = self.capture.resolve_microphone(mic_index)

            if SEGMENTATION_MODE == "vad":
                # Load Silero before opening streams so a missing model
                # fails fast without starting capture.
                self.segmenter = Segmenter()
                self._vad_segment_number = 0
                self._vad_errors = 0
                print("VAD segmenter ready (Silero via onnxruntime).")

            self._meeting_epoch = time.time()

            if self.worker is None:
                if SEGMENTATION_MODE == "vad":
                    self.worker = SegmentWorker(
                        transcriber=transcriber,
                        on_transcript=self.on_vad_transcript,
                    )
                else:
                    self.worker = TranscriptionWorker(
                        transcriber=transcriber,
                        chunk_duration=CHUNK_DURATION,
                        overlap=OVERLAP,
                        on_segments_ready=self.on_segments_ready,
                    )
                self.worker.start()

            self.capture.start(system_device, microphone_device)

            if SEGMENTATION_MODE == "vad":
                self._vad_counts = {"YOU": 0, "OTHERS": 0}
                self._vad_report_start = time.monotonic()
                print("VAD frame mode: segments transcribed as they close.")

            # One meeting id per Start/Stop press, so the cloud keeps this
            # meeting separate from the previous one and can resume it
            # after a reconnect.
            self.cloud_client.start_meeting(str(uuid.uuid4()))

            self._send_to_ui({
                "type": "CAPTURE_STARTED",
            })
            self._send_status("Listening")
            print("Audio capture started from UI.")

        except Exception as e:
            print(f"Failed to start capture: {e}")
            self._send_error(str(e))

    def stop_capture(self):

        if not self.capture.capture_started:
            return

        self.capture.stop()

        if SEGMENTATION_MODE == "vad" and self.segmenter is not None:
            lock = getattr(self, "_segmenter_lock", None)
            if lock is None:
                self._segmenter_lock = threading.Lock()
                lock = self._segmenter_lock

            with lock:
                if self.segmenter is not None:
                    # The open segment will never see the silence that would
                    # close it, so flush it now — otherwise the last words of the
                    # meeting are never transcribed.
                    try:
                        for segment in self.segmenter.flush():
                            self._on_vad_segment(segment)
                    except Exception as e:
                        print(f"VAD flush error: {e}")

                    # Fresh segmenter per capture session; the worker's queue is
                    # dropped with it.
                    self.segmenter = None

        # Stop the worker before ending the meeting so an in-flight
        # segment still lands inside it (pending backlog is discarded).
        if self.worker is not None:
            if isinstance(self.worker, SegmentWorker):
                # Let the flushed tail segment reach the cloud.
                if not self.worker.drain(timeout=DRAIN_TIMEOUT_S):
                    print(
                        f"VAD drain timed out after {DRAIN_TIMEOUT_S:.0f}s "
                        f"— pending segments discarded."
                    )

            self.worker.stop()
            self.worker = None

        self.cloud_client.end_meeting()

        self._send_to_ui({"type": "CAPTURE_STOPPED"})
        self._send_status("Ready")
        print("Audio capture stopped from UI.")

    def _process_vad_frames(self):
        """Phase 2: feed frames to the VAD segmenter and report cadence.

        Finished segments are logged (Phase 0 stage `segment_ready`) and
        submitted to the transcription worker (Phase 3).
        """

        frames = self.capture.drain_frames(max_items=512)

        lock = getattr(self, "_segmenter_lock", None)
        if lock is None:
            self._segmenter_lock = threading.Lock()
            lock = self._segmenter_lock

        with lock:
            if self.segmenter is not None:
                for frame in frames:
                    try:
                        closed = self.segmenter.add_frame(frame)
                    except Exception as e:
                        # Do not let a transient ONNX error kill the agent;
                        # disable after repeated failures.
                        self._vad_errors += 1
                        print(f"VAD segmenter error: {e}")
                        if self._vad_errors >= 5:
                            self._send_error(f"VAD disabled: {e}")
                            self.segmenter = None
                        break

                    self._vad_errors = 0

                    for segment in closed:
                        self._on_vad_segment(segment)

        for frame in frames:
            self._vad_counts[frame["channel"]] += 1

        now = time.monotonic()

        if now - self._vad_report_start < 10.0:
            return

        elapsed = now - self._vad_report_start

        print(
            f"[frames] {elapsed:.1f}s: "
            f"YOU={self._vad_counts['YOU']} "
            f"OTHERS={self._vad_counts['OTHERS']} "
            f"frames_dropped={self.capture.frames_dropped} "
            f"segments={self._vad_segment_number} "
            f"queued={self.worker.pending if self.worker is not None else 0} "
            f"segs_dropped={self.worker.dropped if self.worker is not None else 0}"
        )

        self._vad_counts = {"YOU": 0, "OTHERS": 0}
        self._vad_report_start = now

    def _on_vad_segment(self, segment):
        """Phase 0 timing + hand a finished segment to the worker."""

        self._vad_segment_number += 1
        key = f"seg-{self._vad_segment_number}"

        # Meeting-relative seconds (server renders mm:ss from start).
        segment["start"] = max(0.0, segment["start"] - self._meeting_epoch)
        segment["end"] = segment["start"] + segment["duration"]

        TIMING.event(
            "segment_ready",
            key=key,
            channel=segment["channel"],
            duration_s=round(segment["duration"], 3),
        )
        print(
            f"[segment] {key} {segment['channel']} "
            f"{segment['duration']:.2f}s"
        )

        if self.worker is None:
            print(f"[segment] {key} dropped — no worker running.")
            return

        self.worker.submit(segment, key)

    def on_segments_ready(self, combined, chunk_number):

        print(
            f"Sending {len(combined)} segments to cloud "
            f"(chunk {chunk_number})..."
        )
        self.cloud_client.send_segments(combined, key=f"chunk-{chunk_number}")

    def on_vad_transcript(self, items, key):
        """One VAD segment transcribed → send it to the cloud now."""

        print(f"Transcript {key}: {len(items)} line(s)")
        for item in items:
            print(f"  {item['speaker']}: {item['text']}")

        self.cloud_client.send_segments(items, key=key)

    def _send_status(self, text):

        self._send_to_ui({
            "type": "STATUS",
            "text": text,
        })

    def _send_error(self, text):

        self._send_to_ui({
            "type": "ERROR",
            "text": text,
        })

    def shutdown(self):

        self._running = False

        if self.worker is not None:
            self.worker.stop()

        self.cloud_client.stop()
        self.capture.close()
        TIMING.close()
        print("\nLocal agent stopped.")


def main():
    MeetingAgent().start()


if __name__ == "__main__":
    main()
