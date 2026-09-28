import os
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

from audio_capture import DualAudioCapture
from cloud_client import CloudClient
from transcriber import Transcriber
from transcription_worker import TranscriptionWorker
from ui_server import UIServer


load_dotenv(Path(__file__).resolve().parent / ".env")

CHUNK_DURATION = 60
OVERLAP = 5


class MeetingAgent:

    def __init__(self):

        self.capture = DualAudioCapture(
            chunk_duration=CHUNK_DURATION,
            overlap=OVERLAP,
        )

        self.ui_server = UIServer()
        self.cloud_client = CloudClient()
        self.transcriber = None
        self.worker = None

        self._transcriber_lock = threading.Lock()
        self._running = True

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
        print("Select devices in the desktop app, then start capture.")
        print("Press Ctrl+C to stop.\n")

        try:
            while self._running:
                if (
                    self.capture.capture_started
                    and self.worker is not None
                    and self.capture.chunk_ready()
                ):
                    result = self.capture.record_chunk()

                    if result is not None:
                        system_file, mic_file, chunk_number = result
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
            transcriber = Transcriber(
                model_size=os.getenv("WHISPER_MODEL", "small"),
                device=os.getenv("WHISPER_DEVICE", "cpu"),
                compute_type=os.getenv("WHISPER_COMPUTE", "int8"),
            )

            with self._transcriber_lock:
                self.transcriber = transcriber

            self._send_status("Ready")
            self.send_devices()
            print("Whisper model ready.")

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

        if msg_type == "HELP_ME":
            print("\n" + "=" * 70)
            print("HELP ME requested — forwarding to cloud")
            print("=" * 70)
            self.cloud_client.send_help_me()

    def handle_cloud_message(self, message):

        msg_type = message.get("type")

        if msg_type == "CLOUD_STATUS":
            state = message.get("text", "disconnected")
            print(f"Cloud backend: {state}")
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
                print("  question:", message.get("question", ""))
                print("  answer  :", message.get("answer", ""))

            self.ui_server.send(message)
            return

        if msg_type == "CHUNK_PROCESSED":
            print("\n" + "=" * 70)
            print("CLEAN CONVERSATION")
            print("=" * 70)
            print(message.get("conversation", ""))
            print("=" * 70)

    def send_devices(self):

        self.ui_server.send({
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

            if self.worker is None:
                self.worker = TranscriptionWorker(
                    transcriber=transcriber,
                    chunk_duration=CHUNK_DURATION,
                    overlap=OVERLAP,
                    on_segments_ready=self.on_segments_ready,
                )
                self.worker.start()

            self.capture.start(system_device, microphone_device)

            self.ui_server.send({
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

        if self.worker is not None:
            self.worker.stop()
            self.worker = None

        self.ui_server.send({"type": "CAPTURE_STOPPED"})
        self._send_status("Ready")
        print("Audio capture stopped from UI.")

    def on_segments_ready(self, combined, chunk_number):

        print(
            f"Sending {len(combined)} segments to cloud "
            f"(chunk {chunk_number})..."
        )
        self.cloud_client.send_segments(combined)

    def _send_status(self, text):

        self.ui_server.send({
            "type": "STATUS",
            "text": text,
        })

    def _send_error(self, text):

        self.ui_server.send({
            "type": "ERROR",
            "text": text,
        })

    def shutdown(self):

        self._running = False

        if self.worker is not None:
            self.worker.stop()

        self.cloud_client.stop()
        self.capture.close()
        print("\nLocal agent stopped.")


def main():
    MeetingAgent().start()


if __name__ == "__main__":
    main()
