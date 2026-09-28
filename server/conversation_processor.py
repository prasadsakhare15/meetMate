import re


class ConversationProcessor:

    def __init__(self):
        self.previous_text = ""

    def clean_text(self, text):

        if not text:
            return ""

        text = text.strip()

        text = re.sub(r"\s+", " ", text)

        text = re.sub(r"^\[.*?\]\s*", "", text)

        text = re.sub(r"([.!?])\1+", r"\1", text)

        return text.strip()

    def process(self, segments):

        cleaned = []

        for segment in segments:
            text = self.clean_text(segment.get("text", ""))

            if not text:
                continue

            cleaned.append({
                "speaker": segment["speaker"],
                "start": segment["start"],
                "end": segment["end"],
                "text": text
            })

        return cleaned

    def build_conversation(self, segments):

        lines = []

        for segment in segments:
            lines.append(
                f"{segment['speaker']}: {segment['text']}"
            )

        return "\n".join(lines)

    def find_questions(self, segments):

        questions = []

        for segment in segments:
            text = segment["text"].strip()

            if "?" in text or self.looks_like_question(text):
                questions.append(segment)

        return questions

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

        return text.startswith(question_starters)

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

            if any(keyword in text for keyword in keywords):
                important.append(segment)

        return important
