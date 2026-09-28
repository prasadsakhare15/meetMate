import json
import os
from datetime import datetime


class TranscriptStore:

    def __init__(self, output_dir="transcripts"):

        os.makedirs(output_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        self.txt_file = os.path.join(
            output_dir,
            f"meeting_{timestamp}.txt"
        )

        self.json_file = os.path.join(
            output_dir,
            f"meeting_{timestamp}.json"
        )

        self.segments = []

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
