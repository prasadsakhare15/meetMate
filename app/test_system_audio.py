from audio_capture import DualAudioCapture


capture = DualAudioCapture(
    chunk_duration=10,
    overlap=0
)

try:

    device = capture.select_output_device()

    print("\nStarting system audio test...")
    print("Play some audio for 10 seconds.")

    capture._record_system_audio(
        device,
        "test_system.wav"
    )

    print("\nSaved: test_system.wav")

finally:

    capture.close()