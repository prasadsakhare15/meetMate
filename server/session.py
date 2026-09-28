import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import WebSocket

from conversation_processor import ConversationProcessor
from llm_client import LLMClient
from meeting_context import MeetingContext
from transcript_store import TranscriptStore


AUTO_SUGGEST_COOLDOWN = 45  # seconds between auto-triggered suggestions


class MeetingSession:

    def __init__(self, websocket: WebSocket):

        self.websocket = websocket
        self.context = MeetingContext(recent_seconds=300)
        self.processor = ConversationProcessor()
        self.store = TranscriptStore()
        self.llm = LLMClient()
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.last_auto_suggest = 0.0

    async def run(self):

        print("Client connected — new meeting session started.")
        print(f"  Transcript TXT : {self.store.txt_file}")
        print(f"  Transcript JSON: {self.store.json_file}")

        await self.send_status("LISTENING")

        try:
            async for raw in self.websocket.iter_text():
                await self._handle_message(raw)
        finally:
            self.executor.shutdown(wait=False)
            print("Meeting session ended.")

    async def _handle_message(self, raw: str):

        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            await self.send_error("Invalid JSON message.")
            return

        msg_type = message.get("type")

        if msg_type == "SEGMENTS":
            await self._handle_segments(message)
        elif msg_type == "HELP_ME":
            await self._handle_help_me()
        else:
            await self.send_error(f"Unknown message type: {msg_type}")

    async def _handle_segments(self, message):

        segments = message.get("segments", [])

        if not segments:
            return

        combined = self.processor.process(segments)
        combined.sort(key=lambda x: x["start"])

        self.store.add_segments(combined)
        self.context.add_segments(combined)

        await self._maybe_summarize_pending()

        conversation = self.processor.build_conversation(combined)
        questions = self.processor.find_questions(combined)
        important = self.processor.find_important(combined)

        await self.send({
            "type": "CHUNK_PROCESSED",
            "conversation": conversation,
            "questions": [
                {"speaker": q["speaker"], "text": q["text"]}
                for q in questions
            ],
            "important": [
                {"speaker": i["speaker"], "text": i["text"]}
                for i in important
            ],
        })

        others_questions = [
            q for q in questions
            if q["speaker"] == "OTHERS"
        ]

        current_time = time.time()

        if (
            others_questions
            and current_time - self.last_auto_suggest >= AUTO_SUGGEST_COOLDOWN
        ):
            meeting_context = self.context.build_context()

            if meeting_context.strip():
                print("\nQuestion detected from OTHERS — auto-suggesting...")
                try:
                    await self._generate_and_send_suggestion(
                        meeting_context,
                        auto=True,
                    )
                    self.last_auto_suggest = current_time
                except Exception as e:
                    print(f"Auto-suggest error: {e}")

    async def _maybe_summarize_pending(self):

        pending = self.context.get_pending_summary()

        if not pending:
            return

        cleaned = self.processor.process(pending)
        conversation = self.processor.build_conversation(cleaned)

        self.context.clear_pending_summary()

        if not conversation.strip():
            return

        print(f"\nSummarizing {len(pending)} aged segments...")

        loop = asyncio.get_running_loop()

        try:
            bullets = await loop.run_in_executor(
                self.executor,
                self.llm.summarize_conversation,
                conversation,
            )

            for bullet in bullets:
                self.context.add_summary_item(bullet)
                print(f"  + {bullet}")

        except Exception as e:
            print(f"Summary error: {e}")

    async def _handle_help_me(self):

        print("\n" + "=" * 70)
        print("HELP ME requested")
        print("=" * 70)

        meeting_context = self.context.build_context()

        if not meeting_context.strip():
            await self.send_status("LISTENING")
            return

        try:
            await self._generate_and_send_suggestion(
                meeting_context,
                auto=False,
            )
        except Exception as e:
            print(f"LLM error: {e}")
            await self.send_status("ERROR")

    async def _generate_and_send_suggestion(
        self,
        meeting_context: str,
        auto=False,  # noqa: ARG002
    ):

        await self.send_status("THINKING...")

        loop = asyncio.get_running_loop()

        suggestion = await loop.run_in_executor(
            self.executor,
            self.llm.generate_suggestions,
            meeting_context,
        )

        print("\nLLM suggestion generated.")
        print("  question:", suggestion.get("question", ""))
        print("  answer  :", suggestion.get("answer", ""))
        print("  key_point:", suggestion.get("key_point", ""))

        await self.send({
            "type": "SUGGESTION",
            "question_detected": suggestion.get(
                "question_detected", True
            ),
            "question": suggestion.get("question", ""),
            "answer": suggestion.get("answer", ""),
            "follow_up": suggestion.get("follow_up", ""),
            "key_point": suggestion.get("key_point", ""),
            "confidence": suggestion.get("confidence", 0),
        })

        await self.send_status("LISTENING")

    async def send(self, message: dict):

        await self.websocket.send_json(message)

    async def send_status(self, text: str):

        await self.send({"type": "STATUS", "text": text})

    async def send_error(self, text: str):

        await self.send({"type": "ERROR", "text": text})
