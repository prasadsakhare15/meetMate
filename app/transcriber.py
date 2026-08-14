from faster_whisper import WhisperModel


class Transcriber:

    def __init__(
        self,
        model_size="small",
        device="cpu",
        compute_type="int8"
    ):

        print(
            f"\nLoading Whisper model: "
            f"{model_size}"
        )

        self.model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type
        )

        print("Whisper model loaded.")

    def transcribe(self, audio_file):

        segments, info = self.model.transcribe(
            audio_file,
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=True
        )

        results = []

        for segment in segments:

            text = segment.text.strip()

            if not text:
                continue

            results.append({
                "start": segment.start,
                "end": segment.end,
                "text": text
            })

        return results