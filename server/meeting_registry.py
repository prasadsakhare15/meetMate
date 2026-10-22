"""Durable per-meeting state that outlives a single WebSocket connection.

A meeting is identified by the ``meeting_id`` the agent generates when the
user clicks "Start meeting". Everything below — rolling context, summary
bullets, transcript files, auto-suggest cooldown — is keyed by that id, so it
survives both a dropped WebSocket and a cloud server restart.
"""

import json
import os
import time

from meeting_context import MeetingContext
from transcript_store import TranscriptStore


# How long a disconnected or finished meeting stays resumable.
MEETING_TTL = int(os.getenv("MEETING_TTL_SECONDS", 6 * 60 * 60))


class MeetingState:

    def __init__(
        self,
        meeting_id,
        transcript_dir="transcripts",
        state_dir="meetings",
    ):

        self.meeting_id = meeting_id
        self.transcript_dir = transcript_dir
        self.state_dir = state_dir

        self.context = MeetingContext(recent_seconds=300)
        self.store = TranscriptStore(
            output_dir=transcript_dir,
            label=meeting_id,
        )
        self.owner_user_id = None  # set by the session that created the meeting
        self.last_auto_suggest = 0.0
        self.last_auto_question = ""

        self.websocket = None
        self.generation = 0
        self.ended = False
        self.last_seen = time.time()

    # =================================================
    # CONNECTION BINDING
    # =================================================

    def attach(self, websocket):
        """Bind a socket, superseding any earlier session for this meeting."""

        self.generation += 1
        self.websocket = websocket
        self.touch()

        return self.generation

    def is_current(self, generation):
        return self.generation == generation

    def touch(self):
        self.last_seen = time.time()

    # =================================================
    # PERSISTENCE
    # =================================================

    @property
    def state_path(self):
        return os.path.join(
            self.state_dir,
            f"{self.meeting_id}.json",
        )

    def save(self):
        """Write the meeting to disk so a restart can pick it up again."""

        data = {
            "meeting_id": self.meeting_id,
            "ended": self.ended,
            "last_seen": self.last_seen,
            "last_auto_suggest": self.last_auto_suggest,
            "last_auto_question": self.last_auto_question,
            "transcript": {
                "txt_file": self.store.txt_file,
                "json_file": self.store.json_file,
            },
            "context": self.context.snapshot(),
        }

        os.makedirs(self.state_dir, exist_ok=True)

        # Write then swap, so a crash mid-write cannot corrupt the state.
        temporary = self.state_path + ".tmp"

        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2, ensure_ascii=False)

        os.replace(temporary, self.state_path)

    @classmethod
    def load(cls, meeting_id, state_dir, transcript_dir):
        """Rebuild a meeting from its state file."""

        with open(
            os.path.join(state_dir, f"{meeting_id}.json"),
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        state = cls(
            meeting_id,
            transcript_dir=transcript_dir,
            state_dir=state_dir,
        )

        transcript = data.get("transcript", {})
        txt_file = transcript.get("txt_file")
        json_file = transcript.get("json_file")

        if txt_file and json_file:
            state.store = TranscriptStore.resume(
                txt_file,
                json_file,
            )

        state.ended = bool(data.get("ended", False))
        state.last_seen = float(
            data.get("last_seen", time.time())
        )
        state.last_auto_suggest = float(
            data.get("last_auto_suggest", 0.0)
        )
        state.last_auto_question = str(
            data.get("last_auto_question", "")
        )
        state.context.restore(data.get("context", {}))

        return state


class MeetingRegistry:

    def __init__(
        self,
        state_dir="meetings",
        transcript_dir="transcripts",
        ttl=None,
    ):

        self.state_dir = state_dir
        self.transcript_dir = transcript_dir
        self.ttl = MEETING_TTL if ttl is None else ttl

        self._meetings = {}
        self._load_existing()

    def get(self, meeting_id):
        return self._meetings.get(meeting_id)

    def get_or_create(self, meeting_id):
        """Return ``(state, created)``.

        ``created`` is False when an earlier meeting is resumed, whether it
        was still in memory or restored from disk after a restart.
        """

        self._evict()

        existing = self._meetings.get(meeting_id)

        if existing is not None and not existing.ended:
            return existing, False

        # Already finished in memory: start over rather than re-read its
        # file. Otherwise try to restore it from disk.
        state = None if existing is not None else self._load(meeting_id)

        if state is not None and not state.ended:
            self._meetings[meeting_id] = state
            return state, False

        state = MeetingState(
            meeting_id,
            transcript_dir=self.transcript_dir,
            state_dir=self.state_dir,
        )
        state.save()

        self._meetings[meeting_id] = state

        return state, True

    def end(self, meeting_id):

        state = self._meetings.get(meeting_id)

        if state is None:
            return None

        state.ended = True
        state.touch()
        state.save()

        return state

    def _load(self, meeting_id):

        path = os.path.join(
            self.state_dir,
            f"{meeting_id}.json",
        )

        if not os.path.exists(path):
            return None

        try:
            state = MeetingState.load(
                meeting_id,
                self.state_dir,
                self.transcript_dir,
            )

        except Exception as e:
            print(f"Could not restore meeting {meeting_id}: {e}")
            return None

        if time.time() - state.last_seen > self.ttl:
            self._delete_state_file(meeting_id)
            return None

        print(f"Restored meeting {meeting_id} from disk.")

        return state

    def _load_existing(self):

        if not os.path.isdir(self.state_dir):
            return

        for name in os.listdir(self.state_dir):

            if not name.endswith(".json"):
                continue

            meeting_id = name[: -len(".json")]

            state = self._load(meeting_id)

            if state is not None:
                self._meetings[meeting_id] = state

    def _evict(self):

        cutoff = time.time() - self.ttl

        for meeting_id, state in list(self._meetings.items()):

            if state.last_seen >= cutoff:
                continue

            print(f"Evicting stale meeting {meeting_id}.")

            self._meetings.pop(meeting_id, None)
            self._delete_state_file(meeting_id)

    def _delete_state_file(self, meeting_id):

        try:
            os.remove(
                os.path.join(
                    self.state_dir,
                    f"{meeting_id}.json",
                )
            )
        except OSError:
            pass


REGISTRY = MeetingRegistry()
