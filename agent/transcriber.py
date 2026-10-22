from faster_whisper import WhisperModel


class Transcriber:

    def __init__(
        self,
        model_size="small",
        device="cpu",
        compute_type="int8",
        cpu_threads=0,
    ):

        print(
            f"\nLoading Whisper model: "
            f"{model_size}"
        )

        self.model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
            cpu_threads=cpu_threads,
        )

        print("Whisper model loaded.")

    def transcribe(
        self,
        audio,
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=True,
        initial_prompt=None,
    ):
        """Transcribe a file path or a 16 kHz float32 mono array.

        The defaults reproduce the original chunk-path behaviour exactly;
        the Phase 3 segment worker passes tuned values instead
        (beam_size=1, condition_on_previous_text=False, prompt).
        """

        segments, info = self.model.transcribe(
            audio,
            beam_size=beam_size,
            vad_filter=vad_filter,
            condition_on_previous_text=condition_on_previous_text,
            initial_prompt=initial_prompt,
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