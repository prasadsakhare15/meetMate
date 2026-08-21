import re


class ConversationProcessor:

    def __init__(self):
        self.previous_text = ""

    # =========================================
    # CLEAN WHISPER TEXT
    # =========================================

    def clean_text(self, text):

        if not text:
            return ""

        text = text.strip()

        # Remove excessive whitespace
        text = re.sub(
            r"\s+",
            " ",
            text
        )

        # Remove common Whisper artifacts
        text = re.sub(
            r"^\[.*?\]\s*",
            "",
            text
        )

        # Remove repeated punctuation
        text = re.sub(
            r"([.!?])\1+",
            r"\1",
            text
        )

        return text.strip()

    # =========================================
    # PROCESS SEGMENTS
    # =========================================

    def process(self, segments):

        cleaned = []

        for segment in segments:

            text = self.clean_text(
                segment.get("text", "")
            )

            if not text:
                continue

            cleaned.append({
                "speaker": segment["speaker"],
                "start": segment["start"],
                "end": segment["end"],
                "text": text
            })

        return cleaned

    # =========================================
    # BUILD CONVERSATION
    # =========================================

    def build_conversation(self, segments):

        lines = []

        for segment in segments:

            speaker = segment["speaker"]

            text = segment["text"]

            lines.append(
                f"{speaker}: {text}"
            )

        return "\n".join(lines)

    # =========================================
    # FIND QUESTIONS
    # =========================================

    def find_questions(self, segments):

        questions = []

        for segment in segments:

            text = segment["text"].strip()

            if (
                "?" in text
                or self.looks_like_question(text)
            ):

                questions.append(segment)

        return questions

    # =========================================
    # QUESTION HEURISTIC
    # =========================================

    def looks_like_question(self, text):

        question_starters = (
            "what ",
            "why ",
            "how ",
            "when ",
            "where ",
            "who ",
            "which ",
            "can ",
            "could ",
            "would ",
            "should ",
            "do ",
            "does ",
            "did ",
            "is ",
            "are ",
            "will ",
            "have ",
            "has "
        )

        text = text.lower().strip()

        return text.startswith(
            question_starters
        )

    # =========================================
    # IMPORTANT SEGMENTS
    # =========================================

    def find_important(self, segments):

        keywords = (
            "deadline",
            "budget",
            "problem",
            "issue",
            "decision",
            "requirement",
            "priority",
            "risk",
            "blocker",
            "important",
            "meeting",
            "release",
            "launch",
            "client",
            "customer"
        )
        
        important = []

        for segment in segments:

            text = segment["text"].lower()

            if any(
                keyword in text
                for keyword in keywords
            ):

                important.append(segment)

        return important