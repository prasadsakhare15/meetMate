import os
import queue
import time
import wave
import threading
from collections import deque

import pyaudiowpatch as pyaudio


# Segmentation modes (SEGMENTATION_MODE env var).
#
#   "chunk" — original fixed-length chunks (30 s + 5 s overlap).
#   "vad"   — small ~30 ms frames per channel, consumed by the VAD
#             segmenter (agent/segmenter.py, Phase 2).
MODE_CHUNK = "chunk"
MODE_VAD = "vad"

# How many frames may wait in the queue before the oldest is dropped
# (~30 s of audio at 30 ms per frame).
FRAME_QUEUE_MAX = 1000


class DualAudioCapture:

    def __init__(
        self,
        chunk_duration=60,
        overlap=5,
        output_dir="recordings",
        mode=MODE_CHUNK,
        frame_ms=30,
    ):
        self.chunk_duration = chunk_duration
        self.overlap = overlap

        if mode not in (MODE_CHUNK, MODE_VAD):
            raise ValueError(f"Unknown capture mode: {mode!r}")

        self.mode = mode
        self.frame_ms = frame_ms

        # Small frames for the VAD segmenter (Phase 2). Bounded so a
        # stalled consumer cannot grow memory without limit.
        self.frame_queue = queue.Queue(maxsize=FRAME_QUEUE_MAX)
        self.frames_dropped = 0

        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

        self.audio = pyaudio.PyAudio()

        self.wasapi = (
            self.audio.get_host_api_info_by_type(
                pyaudio.paWASAPI
            )
        )

        # -----------------------------------------
        # Streams
        # -----------------------------------------

        self.mic_stream = None
        self.system_stream = None

        self.mic_thread = None
        self.system_thread = None

        self.stop_event = threading.Event()
        self.capture_started = False

        # -----------------------------------------
        # Buffers
        # Each item = raw PCM bytes
        # -----------------------------------------

        self.mic_buffer = deque()
        self.system_buffer = deque()

        self.mic_lock = threading.Lock()
        self.system_lock = threading.Lock()

        self.mic_frames = 0
        self.system_frames = 0

        # -----------------------------------------
        # Audio configuration
        # -----------------------------------------

        self.mic_sample_rate = None
        self.system_sample_rate = None

        self.mic_channels = 1
        self.system_channels = 2

        self.sample_width = 2  # int16

        self.frames_per_buffer = 1024

        # Frames read per stream.read() call; set in start() — 1024 in
        # chunk mode (unchanged), exactly frame_ms of audio in vad mode.
        self.mic_read_frames = self.frames_per_buffer
        self.system_read_frames = self.frames_per_buffer

        # -----------------------------------------
        # Chunking
        # -----------------------------------------

        self.chunk_number = 1

    # =================================================
    # DEVICE SELECTION
    # =================================================

    @staticmethod
    def _device_name(device):
        return str(device.get("name", "")).replace("\x00", "").strip()

    def serialize_output_devices(self):

        devices = []

        for i in range(self.audio.get_device_count()):
            device = self.audio.get_device_info_by_index(i)

            if (
                device["hostApi"] == self.wasapi["index"]
                and device["maxOutputChannels"] > 0
            ):
                devices.append({
                    "index": device["index"],
                    "name": self._device_name(device),
                })

        return devices

    def serialize_microphones(self):

        devices = []

        for i in range(self.audio.get_device_count()):
            device = self.audio.get_device_info_by_index(i)

            if device["maxInputChannels"] > 0:
                devices.append({
                    "index": device["index"],
                    "name": self._device_name(device),
                })

        return devices

    def resolve_loopback(self, output_index):

        output_device = self.audio.get_device_info_by_index(
            int(output_index)
        )

        loopback = (
            self.audio
            .get_wasapi_loopback_analogue_by_dict(
                output_device
            )
        )

        if loopback is None:
            raise RuntimeError(
                "Could not find WASAPI loopback device."
            )

        print(
            f"Selected output: {self._device_name(output_device)}"
        )
        print(f"Loopback: {self._device_name(loopback)}")

        return loopback

    def resolve_microphone(self, mic_index):

        device = self.audio.get_device_info_by_index(int(mic_index))

        if device["maxInputChannels"] <= 0:
            raise RuntimeError("Selected device is not a microphone.")

        print(f"Selected microphone: {self._device_name(device)}")
        return device

    def list_output_devices(self):

        devices = []

        print(
            "\nAvailable WASAPI output devices:\n"
        )

        for i in range(
            self.audio.get_device_count()
        ):

            device = (
                self.audio.get_device_info_by_index(i)
            )

            if (
                device["hostApi"]
                == self.wasapi["index"]
                and device["maxOutputChannels"] > 0
            ):
                devices.append(device)

                print(
                    f"[{len(devices) - 1}] "
                    f"{device['name']}"
                )

        return devices

    def select_output_device(self):

        devices = self.list_output_devices()

        while True:

            try:

                choice = int(
                    input(
                        "\nSelect speakers/headphones: "
                    )
                )

                if 0 <= choice < len(devices):
                    break

            except ValueError:
                pass

            print("Invalid selection.")

        output_device = devices[choice]

        loopback = (
            self.audio
            .get_wasapi_loopback_analogue_by_dict(
                output_device
            )
        )

        if loopback is None:

            raise RuntimeError(
                "Could not find WASAPI loopback device."
            )

        print(
            f"\nSelected output: "
            f"{output_device['name']}"
        )

        print(
            f"Loopback: "
            f"{loopback['name']}"
        )

        return loopback

    # =================================================
    # MICROPHONE SELECTION
    # =================================================

    def list_microphones(self):

        devices = []

        print(
            "\nAvailable microphones:\n"
        )

        for i in range(
            self.audio.get_device_count()
        ):

            device = (
                self.audio.get_device_info_by_index(i)
            )

            if device["maxInputChannels"] > 0:

                devices.append(device)

                print(
                    f"[{len(devices) - 1}] "
                    f"{device['name']}"
                )

        return devices

    def select_microphone(self):

        devices = self.list_microphones()

        while True:

            try:

                choice = int(
                    input(
                        "\nSelect microphone: "
                    )
                )

                if 0 <= choice < len(devices):
                    break

            except ValueError:
                pass

            print("Invalid selection.")

        device = devices[choice]

        print(
            f"Selected microphone: "
            f"{device['name']}"
        )

        return device

    # =================================================
    # START
    # =================================================

    def start(
        self,
        system_device,
        microphone_device
    ):

        if self.capture_started:
            return

        self.stop_event.clear()

        self.mic_sample_rate = int(
            microphone_device[
                "defaultSampleRate"
            ]
        )

        self.system_sample_rate = int(
            system_device[
                "defaultSampleRate"
            ]
        )

        self.mic_channels = 1

        self.system_channels = min(
            2,
            int(
                system_device[
                    "maxInputChannels"
                ]
            )
        )

        if self.system_channels <= 0:

            raise RuntimeError(
                "WASAPI loopback has no input channels."
            )

        # -----------------------------------------
        # Read size
        # -----------------------------------------

        if self.mode == MODE_VAD:
            # Exactly frame_ms of audio per read, per channel.
            self.mic_read_frames = max(
                1, int(self.mic_sample_rate * self.frame_ms / 1000)
            )
            self.system_read_frames = max(
                1, int(self.system_sample_rate * self.frame_ms / 1000)
            )
        else:
            self.mic_read_frames = self.frames_per_buffer
            self.system_read_frames = self.frames_per_buffer

        # -----------------------------------------
        # Open microphone ONCE
        # -----------------------------------------

        print(
            "\nOpening microphone stream..."
        )

        self.mic_stream = self.audio.open(
            format=pyaudio.paInt16,
            channels=self.mic_channels,
            rate=self.mic_sample_rate,
            input=True,
            input_device_index=microphone_device[
                "index"
            ],
            frames_per_buffer=self.mic_read_frames,
            start=True
        )

        # -----------------------------------------
        # Open WASAPI loopback ONCE
        # -----------------------------------------

        print(
            "Opening WASAPI loopback stream..."
        )

        self.system_stream = self.audio.open(
            format=pyaudio.paInt16,
            channels=self.system_channels,
            rate=self.system_sample_rate,
            input=True,
            input_device_index=system_device[
                "index"
            ],
            frames_per_buffer=self.system_read_frames,
            start=True
        )

        # -----------------------------------------
        # Threads
        # -----------------------------------------

        self.mic_thread = threading.Thread(
            target=self._mic_capture_loop,
            name="MicrophoneCapture",
            daemon=True
        )

        self.system_thread = threading.Thread(
            target=self._system_capture_loop,
            name="SystemAudioCapture",
            daemon=True
        )

        self.capture_started = True

        self.mic_thread.start()
        self.system_thread.start()

        print(
            "Microphone capture thread running."
        )

        print(
            "WASAPI loopback thread running."
        )

        print(
            "Audio capture started."
        )

    # =================================================
    # MICROPHONE THREAD
    # =================================================

    def _mic_capture_loop(self):

        while not self.stop_event.is_set():

            try:

                data = self.mic_stream.read(
                    self.mic_read_frames,
                    exception_on_overflow=False
                )

                if self.mode == MODE_VAD:
                    self._emit_frame(
                        "YOU",
                        data,
                        self.mic_sample_rate,
                        self.mic_channels,
                    )
                else:
                    with self.mic_lock:

                        self.mic_buffer.append(data)

                        self.mic_frames += (
                            self.mic_read_frames
                        )

            except Exception as e:

                print(
                    "\n❌ Microphone capture error:"
                )

                print(e)

                self.stop_event.set()

                break

    # =================================================
    # SYSTEM THREAD
    # =================================================

    def _system_capture_loop(self):

        while not self.stop_event.is_set():

            try:

                data = self.system_stream.read(
                    self.system_read_frames,
                    exception_on_overflow=False
                )

                if self.mode == MODE_VAD:
                    self._emit_frame(
                        "OTHERS",
                        data,
                        self.system_sample_rate,
                        self.system_channels,
                    )
                else:
                    with self.system_lock:

                        self.system_buffer.append(data)

                        self.system_frames += (
                            self.system_read_frames
                        )

            except Exception as e:

                print(
                    "\n❌ WASAPI capture error:"
                )

                print(e)

                self.stop_event.set()

                break

    # =================================================
    # FRAME EMISSION (vad mode)
    # =================================================

    def _emit_frame(self, channel, data, sample_rate, n_channels):
        """Queue one small frame for the VAD segmenter (Phase 2)."""

        duration = len(data) / (
            self.sample_width * n_channels * sample_rate
        )

        frame = {
            "channel": channel,
            "pcm": data,
            "sample_rate": sample_rate,
            "channels": n_channels,
            "sample_width": self.sample_width,
            # Wall-clock time the frame finished arriving (epoch
            # seconds), so segment start timestamps need no extra
            # bookkeeping downstream.
            "ts": time.time(),
            "duration": duration,
        }

        try:
            self.frame_queue.put_nowait(frame)

        except queue.Full:
            # Consumer is behind: drop the oldest frame so live audio
            # stays fresh, then retry once.
            try:
                self.frame_queue.get_nowait()
                self.frames_dropped += 1
            except queue.Empty:
                pass

            try:
                self.frame_queue.put_nowait(frame)
            except queue.Full:
                self.frames_dropped += 1

    def drain_frames(self, max_items=64):
        """Take up to max_items queued frames (FIFO)."""

        frames = []

        for _ in range(max_items):
            try:
                frames.append(self.frame_queue.get_nowait())
            except queue.Empty:
                break

        return frames

    # =================================================
    # CHUNK READY
    # =================================================

    def chunk_ready(self):

        # The chunk path is disabled in vad mode — frames only.
        if self.mode == MODE_VAD:
            return False

        if not self.capture_started:
            return False

        required_mic_frames = int(
            self.mic_sample_rate
            * (
                self.chunk_duration
                + self.overlap
            )
        )

        required_system_frames = int(
            self.system_sample_rate
            * (
                self.chunk_duration
                + self.overlap
            )
        )

        with self.mic_lock:

            mic_ready = (
                self.mic_frames
                >= required_mic_frames
            )

        with self.system_lock:

            system_ready = (
                self.system_frames
                >= required_system_frames
            )

        return (
            mic_ready
            and system_ready
        )

    # =================================================
    # CREATE CHUNK
    # =================================================

    def record_chunk(self):

        if not self.chunk_ready():

            return None

        chunk_number = self.chunk_number

        mic_file = os.path.join(
            self.output_dir,
            f"mic_{chunk_number:04d}.wav"
        )

        system_file = os.path.join(
            self.output_dir,
            f"system_{chunk_number:04d}.wav"
        )

        mic_frames_needed = int(
            self.mic_sample_rate
            * (
                self.chunk_duration
                + self.overlap
            )
        )

        system_frames_needed = int(
            self.system_sample_rate
            * (
                self.chunk_duration
                + self.overlap
            )
        )

        # -----------------------------------------
        # Extract data
        # -----------------------------------------

        with self.mic_lock:

            mic_bytes = b"".join(
                self.mic_buffer
            )

        with self.system_lock:

            system_bytes = b"".join(
                self.system_buffer
            )

        mic_bytes_needed = (
            mic_frames_needed
            * self.mic_channels
            * self.sample_width
        )

        system_bytes_needed = (
            system_frames_needed
            * self.system_channels
            * self.sample_width
        )

        mic_chunk = mic_bytes[
            :mic_bytes_needed
        ]

        system_chunk = system_bytes[
            :system_bytes_needed
        ]

        # -----------------------------------------
        # Save WAV
        # -----------------------------------------

        self._write_wav(
            mic_file,
            mic_chunk,
            self.mic_channels,
            self.mic_sample_rate
        )

        self._write_wav(
            system_file,
            system_chunk,
            self.system_channels,
            self.system_sample_rate
        )

        # -----------------------------------------
        # Remove only one chunk duration.
        #
        # Keep the final `overlap` seconds!
        # -----------------------------------------

        mic_frames_to_remove = int(
            self.mic_sample_rate
            * self.chunk_duration
        )

        system_frames_to_remove = int(
            self.system_sample_rate
            * self.chunk_duration
        )

        self._remove_frames(
            "mic",
            mic_frames_to_remove
        )

        self._remove_frames(
            "system",
            system_frames_to_remove
        )

        self.chunk_number += 1

        print(
            f"\nCreated chunk {chunk_number}"
        )

        print(
            f"  {mic_file}"
        )

        print(
            f"  {system_file}"
        )

        return (
            system_file,
            mic_file,
            chunk_number
        )

    # =================================================
    # REMOVE FRAMES WHILE KEEPING OVERLAP
    # =================================================

    def _remove_frames(
        self,
        source,
        frames_to_remove
    ):

        if source == "mic":

            buffer = self.mic_buffer
            lock = self.mic_lock
            channels = self.mic_channels

        else:

            buffer = self.system_buffer
            lock = self.system_lock
            channels = self.system_channels

        bytes_to_remove = (
            frames_to_remove
            * channels
            * self.sample_width
        )

        with lock:

            data = b"".join(buffer)

            remaining = data[
                bytes_to_remove:
            ]

            buffer.clear()

            if remaining:

                buffer.append(
                    remaining
                )

            if source == "mic":

                self.mic_frames = (
                    len(remaining)
                    // (
                        channels
                        * self.sample_width
                    )
                )

            else:

                self.system_frames = (
                    len(remaining)
                    // (
                        channels
                        * self.sample_width
                    )
                )

    # =================================================
    # WRITE WAV
    # =================================================

    def _write_wav(
        self,
        filename,
        data,
        channels,
        sample_rate
    ):

        with wave.open(
            filename,
            "wb"
        ) as wf:

            wf.setnchannels(channels)

            wf.setsampwidth(
                self.sample_width
            )

            wf.setframerate(
                sample_rate
            )

            wf.writeframes(data)

    # =================================================
    # STOP
    # =================================================

    def stop(self):

        if not self.capture_started:
            return

        print(
            "\nStopping audio capture..."
        )

        self.stop_event.set()

        if self.mic_thread:

            self.mic_thread.join(
                timeout=2
            )

        if self.system_thread:

            self.system_thread.join(
                timeout=2
            )

        if self.mic_stream:

            try:
                self.mic_stream.stop_stream()
            except Exception:
                pass

            try:
                self.mic_stream.close()
            except Exception:
                pass

            self.mic_stream = None

        if self.system_stream:

            try:
                self.system_stream.stop_stream()
            except Exception:
                pass

            try:
                self.system_stream.close()
            except Exception:
                pass

            self.system_stream = None

        with self.mic_lock:
            self.mic_buffer.clear()
            self.mic_frames = 0

        with self.system_lock:
            self.system_buffer.clear()
            self.system_frames = 0

        while True:
            try:
                self.frame_queue.get_nowait()
            except queue.Empty:
                break

        self.chunk_number = 1
        self.mic_thread = None
        self.system_thread = None
        self.capture_started = False

        print(
            "Audio streams closed safely."
        )

    # =================================================
    # CLOSE
    # =================================================

    def close(self):

        self.stop()

        try:
            self.audio.terminate()
        except Exception:
            pass

        print(
            "PyAudio terminated."
        )