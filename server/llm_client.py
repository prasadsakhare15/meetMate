import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq


load_dotenv(Path(__file__).resolve().parent / ".env")

DEFAULT_SUGGESTION = {
    "question_detected": False,
    "question": "",
    "answer": "",
    "follow_up": "",
    "key_point": "",
    "confidence": 0.0,
}


class LLMClient:

    def __init__(self):

        api_key = os.getenv("GROQ_API_KEY")

        if not api_key:
            raise ValueError(
                "GROQ_API_KEY not found in server/.env"
            )

        self.client = Groq(api_key=api_key)
        self.model = os.getenv(
            "GROQ_MODEL",
            "openai/gpt-oss-20b",
        )

    def generate_suggestions(self, meeting_context):

        system_prompt = """
You are an AI meeting copilot.

Your job is to help the user participate naturally in a live corporate meeting.

Analyze the recent conversation and return a single JSON object with these keys:
- question_detected (boolean)
- question (string)
- answer (string)
- follow_up (string)
- key_point (string)
- confidence (number from 0.0 to 1.0)

Rules:
- Do not invent facts.
- Use only information available in the context.
- If information is insufficient, say so in answer or key_point.
- Answers should sound natural when spoken.
- Keep the answer concise.
- If no question requires the user's response, set question_detected to false and leave question/answer/follow_up empty.

Return ONLY the JSON object. No markdown, no code fences, no extra text.
"""

        user_prompt = f"""
Here is the recent meeting conversation:

--- BEGIN CONTEXT ---

{meeting_context}

--- END CONTEXT ---

Analyze the conversation and return the JSON object.
"""

        result = self._complete_json(system_prompt, user_prompt)
        return self._normalize_suggestion(result)

    def summarize_conversation(self, conversation_text):

        if not conversation_text.strip():
            return []

        system_prompt = """
You summarize meeting conversation excerpts.

Return a single JSON object with one key:
- bullets: an array of 1 to 4 short summary strings

Rules:
- Capture decisions, action items, names, dates, and key topics.
- Do not invent facts.
- If there is nothing meaningful to summarize, return {"bullets": []}.

Return ONLY the JSON object. No markdown, no code fences, no extra text.
"""

        user_prompt = f"""
Summarize this meeting excerpt:

--- BEGIN EXCERPT ---

{conversation_text}

--- END EXCERPT ---
"""

        result = self._complete_json(system_prompt, user_prompt)
        bullets = result.get("bullets", [])

        if not isinstance(bullets, list):
            return []

        return [
            bullet.strip()
            for bullet in bullets
            if isinstance(bullet, str) and bullet.strip()
        ]

    def _complete_json(self, system_prompt, user_prompt):

        last_error = None

        for use_json_mode in (False, True):
            try:
                kwargs = {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.2,
                }

                if use_json_mode:
                    kwargs["response_format"] = {
                        "type": "json_object",
                    }

                response = self.client.chat.completions.create(
                    **kwargs
                )

                content = self._extract_message_text(
                    response.choices[0].message
                )

                if not content or not content.strip():
                    raise ValueError("LLM returned empty content.")

                print("LLM raw content:", content[:500])
                return self._parse_json_object(content)

            except Exception as e:
                last_error = e
                print(f"LLM request failed ({'json mode' if use_json_mode else 'text mode'}): {e}")

        raise last_error

    def _extract_message_text(self, message):

        parts = [
            getattr(message, "content", None),
            getattr(message, "reasoning", None),
        ]

        for part in parts:
            if isinstance(part, str) and part.strip():
                return part

        return ""

    def _parse_json_object(self, content):

        content = content.strip()

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        fenced = re.search(
            r"```(?:json)?\s*(\{.*?\})\s*```",
            content,
            re.DOTALL,
        )
        if fenced:
            return json.loads(fenced.group(1))

        start = content.find("{")
        end = content.rfind("}")

        if start != -1 and end != -1 and end > start:
            return json.loads(content[start:end + 1])

        raise ValueError(
            "LLM returned invalid JSON:\n" + content
        )

    def _normalize_suggestion(self, result):

        if not isinstance(result, dict):
            return dict(DEFAULT_SUGGESTION)

        for value in result.values():
            if (
                isinstance(value, dict)
                and any(
                    key in value
                    for key in (
                        "question",
                        "answer",
                        "question_detected",
                    )
                )
            ):
                result = value
                break

        normalized = dict(DEFAULT_SUGGESTION)

        detected = result.get(
            "question_detected",
            result.get("questionDetected", False),
        )
        normalized["question_detected"] = bool(detected)

        normalized["question"] = str(
            result.get("question")
            or result.get("latest_question")
            or ""
        ).strip()
        normalized["answer"] = str(
            result.get("answer")
            or result.get("suggested_response")
            or result.get("response")
            or ""
        ).strip()
        normalized["follow_up"] = str(
            result.get("follow_up")
            or result.get("followUp")
            or ""
        ).strip()
        normalized["key_point"] = str(
            result.get("key_point")
            or result.get("keyPoint")
            or ""
        ).strip()

        try:
            normalized["confidence"] = float(
                result.get("confidence", 0.0) or 0.0
            )
        except (TypeError, ValueError):
            normalized["confidence"] = 0.0

        return normalized
