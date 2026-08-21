import threading
from collections import deque


class MeetingContext:

    def __init__(
        self,
        recent_seconds=300,
        max_summary_items=20
    ):
        self.lock = threading.Lock()
        self.recent_seconds = recent_seconds
        self.max_summary_items = max_summary_items

        # Complete transcript
        self.segments = []

        # Exact recent conversation
        self.recent_segments = deque()

        # Segments that have moved outside
        # the recent conversation window
        self.pending_summary = []

        # Persistent summary
        self.summary = []

    # =========================================================
    # ADD SEGMENTS
    # =========================================================

    def add_segments(self, segments):
        with self.lock:
            for segment in segments:

                self.segments.append(segment)
                self.recent_segments.append(segment)

            self._move_old_segments()

    # =========================================================
    # MOVE OLD SEGMENTS
    # =========================================================

    def _move_old_segments(self):

        if not self.recent_segments:
            return

        latest_time = (
            self.recent_segments[-1]["end"]
        )

        cutoff = (
            latest_time
            - self.recent_seconds
        )

        while (
            self.recent_segments
            and self.recent_segments[0]["end"] < cutoff
        ):

            old_segment = (
                self.recent_segments.popleft()
            )

            self.pending_summary.append(
                old_segment
            )

    # =========================================================
    # GET PENDING SUMMARY
    # =========================================================

    def get_pending_summary(self):

        return list(
            self.pending_summary
        )

    # =========================================================
    # CLEAR PENDING SUMMARY
    # =========================================================

    def clear_pending_summary(self):

        self.pending_summary.clear()

    # =========================================================
    # ADD SUMMARY
    # =========================================================

    def add_summary_item(self, text):

        text = text.strip()

        if not text:
            return

        self.summary.append(text)

        if (
            len(self.summary)
            > self.max_summary_items
        ):

            self.summary = (
                self.summary[
                    -self.max_summary_items:
                ]
            )

    # =========================================================
    # GET RECENT CONVERSATION
    # =========================================================

    def get_recent_conversation(self):

        return list(
            self.recent_segments
        )

    # =========================================================
    # BUILD LLM CONTEXT
    # =========================================================

    def build_context(self):
        with self.lock:
            output = []

            # ---------------------------------
            # LONG TERM SUMMARY
            # ---------------------------------

            if self.summary:

                output.append(
                    "MEETING SUMMARY:"
                )

                for item in self.summary:

                    output.append(
                        f"- {item}"
                    )

                output.append("")

            # ---------------------------------
            # RECENT CONVERSATION
            # ---------------------------------

            output.append(
                "RECENT CONVERSATION:"
            )

            for segment in self.recent_segments:

                timestamp = self._format_time(
                    segment["start"]
                )

                output.append(
                    f"[{timestamp}] "
                    f"{segment['speaker']}: "
                    f"{segment['text']}"
                )

            return "\n".join(output)

    # =========================================================

    @staticmethod
    def _format_time(seconds):

        minutes = int(
            seconds // 60
        )

        seconds = int(
            seconds % 60
        )

        return (
            f"{minutes:02d}:{seconds:02d}"
        )