import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher

from fastapi import WebSocket

from conversation_processor import ConversationProcessor
from llm_client import LLMClient
from meeting_registry import REGISTRY


AUTO_SUGGEST_COOLDOWN = 45         # default; env AUTO_SUGGEST_COOLDOWN overrides
AUTO_SUGGEST_WAIT_S = 1.0          # merge window before an auto-suggest fires
AUTO_SUGGEST_MAX_WAIT_S = 3.0      # cap while OTHERS keeps talking
AUTO_SUGGEST_MIN_INTERVAL_S = 5.0  # burst guard for cooldown overrides


def _env_float(name, default):
    """Read a float env var. Called late (not at import) because
    load_dotenv() runs after this module is imported."""

    raw = os.getenv(name)

    if raw is None or not str(raw).strip():
        return float(default)

    try:
        return float(str(raw).strip())
    except ValueError:
        raise SystemExit(f"Invalid {name}={raw!r} — expected a number.")


def _similar_question(new_text, previous_text):
    """True when two question texts are effectively the same question."""

    def normalize(text):
        return " ".join(text.lower().split())

    new_text = normalize(new_text)
    previous_text = normalize(previous_text)

    if not new_text or not previous_text:
        return True  # nothing to compare — assume same (no override)

    if new_text in previous_text or previous_text in new_text:
        return True

    return (
        SequenceMatcher(None, new_text, previous_text).ratio() >= 0.75
    )


class MeetingSession:
    """One authenticated WebSocket connection.

    The user id that opened the socket is held here so every meeting operation
    can be checked against the token that validated it. Nothing durable lives
    in the session itself -- context, transcript, and the auto-suggest cooldown
    all live in the ``MeetingState`` keyed by ``meeting_id``.
    """

    def __init__(self, websocket: WebSocket, user_id: str):

        self.websocket = websocket
        self.user_id = user_id
        self.processor = ConversationProcessor()
        self.llm = LLMClient()
        self.executor = ThreadPoolExecutor(max_workers=2)

        self.state = None
        self.generation = None
        self._accepted = asyncio.Event()

        # Phase 4 trigger tuning — read here (not at module import)
        # because load_dotenv() runs after this module is imported.
        self.auto_cooldown_s = _env_float(
            "AUTO_SUGGEST_COOLDOWN", AUTO_SUGGEST_COOLDOWN
        )
        self.auto_wait_s = _env_float(
            "AUTO_SUGGEST_WAIT_S", AUTO_SUGGEST_WAIT_S
        )
        self.auto_max_wait_s = _env_float(
            "AUTO_SUGGEST_MAX_WAIT_S", AUTO_SUGGEST_MAX_WAIT_S
        )
        self.auto_min_interval_s = _env_float(
            "AUTO_SUGGEST_MIN_INTERVAL_S", AUTO_SUGGEST_MIN_INTERVAL_S
        )

        # Pending auto-suggest (Phase 4 merge window).
        self._auto_task = None
        self._auto_pending_texts = []
        self._auto_merge_dirty = False

    async def __aenter__(self):
        await self.websocket.accept()
        self._accepted.set()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._cancel_auto_suggest()
        # _detach_sync is idempotent (run()'s finally also calls it).
        self._detach_sync()
        self.executor.shutdown(wait=False)

    # =================================================
    # LIFECYCLE
    # =================================================

    async def run(self):
        print("Client connected — waiting for START_MEETING.")

        # The socket is already accepted by __aenter__.
        await self.send_status("LISTENING")

        try:
            async for raw in self.websocket.iter_text():

                if (
                    self.state is not None
                    and not self.state.is_current(self.generation)
                ):
                    print(
                        "Connection superseded by a newer session "
                        "for this meeting — closing."
                    )
                    break

                await self._handle_message(raw)

        finally:
            self._detach_sync()
            print("Connection closed.")

    def _detach_sync(self):
        """Leave the meeting alive on the server; only drop the socket."""
        if self.state is None:
            return

        if self.state.is_current(self.generation):
            self.state.websocket = None
            self.state.touch()
            self.state.save()

    # =================================================
    # MESSAGE ROUTING
    # =================================================

    async def _handle_message(self, raw: str):

        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            await self.send_error("Invalid JSON message.")
            return

        msg_type = message.get("type")

        if msg_type == "START_MEETING":
            await self._handle_start_meeting(message)
            return

        if msg_type == "END_MEETING":
            self._handle_end_meeting(message)
            return

        if self.state is None:
            await self.send_error(
                f"No active meeting. Send START_MEETING before {msg_type}."
            )
            return

        # Every non-start message is gated on who owns the meeting.
        if self.state.owner_user_id != self.user_id:
            await self.send_error("Not your meeting.")
            return

        self.state.touch()

        if msg_type == "SEGMENTS":
            await self._handle_segments(message)
        elif msg_type == "HELP_ME":
            await self._handle_help_me()
        else:
            await self.send_error(f"Unknown message type: {msg_type}")

    async def _handle_start_meeting(self, message):

        meeting_id = message.get("meeting_id")

        if not meeting_id:
            await self.send_error("START_MEETING requires a meeting_id.")
            return

        state, created = REGISTRY.get_or_create(meeting_id)

        # A brand-new meeting must be owned by the current user.
        if created:
            state.owner_user_id = self.user_id

        # Only the owner of a meeting may attach to it.
        if state.owner_user_id != self.user_id:
            await self.send_error("Not your meeting.")
            return

        self.state = state
        self.generation = state.attach(self.websocket)

        print(
            f"{'Started' if created else 'Resumed'} meeting {meeting_id} "
            f"— {len(state.store.segments)} segments so far."
        )

        state.save()

        await self.send({
            "type": "MEETING_STARTED",
            "meeting_id": meeting_id,
            "resumed": not created,
            "segment_count": len(state.store.segments),
        })

    def _handle_end_meeting(self, message):

        meeting_id = message.get("meeting_id")

        if not meeting_id and self.state is not None:
            meeting_id = self.state.meeting_id

        if not meeting_id:
            return

        state = REGISTRY.end(meeting_id)

        if state is not None:
            print(
                f"Meeting {meeting_id} ended "
                f"({len(state.store.segments)} segments)."
            )

        if self.state is not None and self.state.meeting_id == meeting_id:
            self.state = None
            self.generation = None

    # =================================================
    # SEGMENTS
    # =================================================

    async def _handle_segments(self, message):

        segments = message.get("segments", [])

        if not segments:
            return

        # Phase 0 timing: epoch ms, comparable with the agent's logs.
        print(
            f"[timing] segments_received n={len(segments)} "
            f"ts_ms={int(time.time() * 1000)}"
        )

        store = self.state.store
        context = self.state.context

        combined = self.processor.process(segments)
        combined.sort(key=lambda x: x["start"])

        store.add_segments(combined)
        context.add_segments(combined)

        # Persist before the slow LLM work below, so a crash mid-call does
        # not lose the segments we just accepted.
        self.state.save()

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

        # Phase 4: the LLM fires after a short merge window so
        # immediately-following OTHERS speech lands in the context first.
        if self._auto_task_pending():
            if others_questions:
                self._auto_pending_texts.extend(
                    q["text"] for q in others_questions
                )
            if any(s["speaker"] == "OTHERS" for s in combined):
                self._auto_merge_dirty = True
        elif others_questions and self._should_auto_suggest(others_questions):
            self._queue_auto_suggest(others_questions)

        self.state.save()

    # =================================================
    # AUTO-SUGGEST MERGE WINDOW (Phase 4)
    # =================================================

    def _auto_task_pending(self):
        return self._auto_task is not None and not self._auto_task.done()

    def _should_auto_suggest(self, questions):
        """Cooldown gate: normal after the cooldown; sooner only for a
        clearly new question, and never inside the burst guard."""

        since = time.time() - self.state.last_auto_suggest

        if since >= self.auto_cooldown_s:
            return True

        if since < self.auto_min_interval_s:
            return False

        last = getattr(self.state, "last_auto_question", "") or ""

        if _similar_question(
            " ".join(q["text"] for q in questions), last
        ):
            return False

        print(
            f"\nNew question during cooldown — overriding "
            f"({self.auto_cooldown_s - since:.0f}s left)."
        )
        return True

    def _queue_auto_suggest(self, questions):
        """Start (or extend) the merge window."""

        self._auto_pending_texts.extend(q["text"] for q in questions)

        if self._auto_task_pending():
            return

        self._auto_merge_dirty = False
        self._auto_task = asyncio.create_task(
            self._auto_suggest_after_wait()
        )

    async def _auto_suggest_after_wait(self):
        """Wait ~1 s so immediately-following OTHERS speech is merged
        into the context first; extend while they keep talking (capped)."""

        waited = 0.0

        try:
            while True:
                await asyncio.sleep(self.auto_wait_s)
                waited += self.auto_wait_s

                if (
                    not self._auto_merge_dirty
                    or waited >= self.auto_max_wait_s
                ):
                    break

                self._auto_merge_dirty = False

            if self.state is None:  # meeting ended while waiting
                return

            meeting_context = self.state.context.build_context()

            if not meeting_context.strip():
                return

            print(
                f"\nQuestion detected from OTHERS — auto-suggesting "
                f"after {waited:.1f}s wait "
                f"({len(self._auto_pending_texts)} question line(s))..."
            )

            try:
                await self._generate_and_send_suggestion(
                    meeting_context,
                    auto=True,
                )
                self.state.last_auto_suggest = time.time()
                self.state.last_auto_question = " ".join(
                    self._auto_pending_texts
                )
                self.state.save()
            except Exception as e:
                print(f"Auto-suggest error: {e}")

        finally:
            self._auto_pending_texts.clear()
            self._auto_merge_dirty = False
            self._auto_task = None

    async def _cancel_auto_suggest(self):
        """Drop a pending auto-suggest (HELP ME takes over)."""

        task = self._auto_task
        self._auto_task = None

        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    async def _maybe_summarize_pending(self):

        context = self.state.context
        pending = context.get_pending_summary()

        if not pending:
            return

        cleaned = self.processor.process(pending)
        conversation = self.processor.build_conversation(cleaned)

        context.clear_pending_summary()

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
                context.add_summary_item(bullet)
                print(f"  + {bullet}")

        except Exception as e:
            print(f"Summary error: {e}")

    # =================================================
    # SUGGESTIONS
    # =================================================

    async def _handle_help_me(self):

        print("\n" + "=" * 70)
        print("HELP ME requested")
        print("=" * 70)

        # Phase 4: HELP ME always answers (bypasses the cooldown) and
        # takes over from any pending auto-suggest.
        await self._cancel_auto_suggest()

        meeting_context = self.state.context.build_context()

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

        llm_started = time.time()

        suggestion = await loop.run_in_executor(
            self.executor,
            self.llm.generate_suggestions,
            meeting_context,
        )

        print(
            f"[timing] llm_done duration_s={time.time() - llm_started:.3f} "
            f"ts_ms={int(time.time() * 1000)}"
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

    # =================================================
    # SENDING
    # =================================================

    async def send(self, message: dict):
        """Send a frame, waiting until the socket is accepted first."""
        if not self._accepted.is_set():
            await self._accepted.wait()

        if self.state is not None:

            if not self.state.is_current(self.generation):
                return

            target = self.state.websocket or self.websocket

        else:
            target = self.websocket

        await target.send_json(message)

    async def send_status(self, text: str):
        await self.send({"type": "STATUS", "text": text})

    async def send_error(self, text: str):
        await self.send({"type": "ERROR", "text": text})
