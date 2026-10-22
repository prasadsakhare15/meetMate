import json
import os
from datetime import datetime


class TranscriptStore:

    def __init__(self, output_dir="transcripts", label=None):
        """`label` keeps two meetings started in the same second apart."""

        os.makedirs(output_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = f"_{label}" if label else ""

        self.txt_file = os.path.join(
            output_dir,
            f"meeting_{timestamp}{suffix}.txt"
        )

        self.json_file = os.path.join(
            output_dir,
            f"meeting_{timestamp}{suffix}.json"
        )

        self.segments = []

    @classmethod
    def resume(cls, txt_file, json_file):
        """Reopen an existing transcript pair so a resumed meeting appends to it."""

        # Bypass __init__ on purpose: it would pick a new timestamp and
        # create a second set of files.
        store = cls.__new__(cls)

        store.txt_file = txt_file
        store.json_file = json_file
        store.segments = []

        if os.path.exists(json_file):

            try:

                with open(json_file, encoding="utf-8") as file:
                    store.segments = json.load(file).get(
                        "segments", []
                    )

            except (OSError, json.JSONDecodeError) as e:
                print(f"Could not reload transcript {json_file}: {e}")

        store.segments.sort(key=lambda x: x["start"])

        return store

    def add_segments(self, segments):
        """Add pre-timestamped segments from the client."""

        for segment in segments:
            if self._is_duplicate(segment):
                continue

            self.segments.append(segment)

        self.segments.sort(key=lambda x: x["start"])
        self._save()

    def _is_duplicate(self, new_segment):

        new_text = new_segment["text"].lower().strip()

        for existing in reversed(self.segments):

            if new_segment["start"] - existing["start"] > 10:
                break

            old_text = existing["text"].lower().strip()

            if (
                new_segment["speaker"] == existing["speaker"]
                and new_text == old_text
            ):
                return True

            if (
                new_segment["speaker"] == existing["speaker"]
                and len(new_text) > 12
                and (
                    new_text in old_text
                    or old_text in new_text
                )
            ):
                return True

        return False

    def _save(self):

        data = {
            "segments": [
                {
                    "speaker": segment["speaker"],
                    "start": round(segment["start"], 2),
                    "end": round(segment["end"], 2),
                    "text": segment["text"],
                }
                for segment in self.segments
            ]
        }

        with open(self.json_file, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2, ensure_ascii=False)

        with open(self.txt_file, "w", encoding="utf-8") as file:
            for segment in self.segments:
                start = self._format_time(segment["start"])
                file.write(
                    f"[{start}] "
                    f"{segment['speaker']}: "
                    f"{segment['text']}\n\n"
                )

    @staticmethod
    def _format_time(seconds):

        minutes = int(seconds // 60)
        seconds = int(seconds % 60)

        return f"{minutes:02d}:{seconds:02d}"
