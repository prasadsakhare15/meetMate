import os
from groq import Groq
from dotenv import load_dotenv

load_dotenv()


class LLMClient:

    def __init__(
        self,
        model="openai/gpt-oss-20b"
    ):
        api_key = os.getenv("GROQ_API_KEY")

        if not api_key:
            raise RuntimeError(
                "GROQ_API_KEY not found in .env"
            )

        self.client = Groq(
            api_key=api_key
        )

        self.model = model

    def generate_suggestions(
        self,
        conversation
    ):

        prompt = f"""
You are a real-time corporate meeting assistant.

You are helping the user participate intelligently
in a professional meeting.

Analyze the recent conversation below.

Return exactly these three sections:

ANSWER:
Give a concise answer the user could say if a
question was directed at them.

FOLLOW-UP:
Suggest one intelligent follow-up question the
user could ask.

KEY POINT:
Give one important point the user should mention
or remember.

Keep everything concise and practical.
Do not invent facts that aren't supported by
the conversation.

RECENT MEETING CONVERSATION:

{conversation}
"""

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a concise corporate "
                        "meeting assistant."
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0.2,
            max_tokens=500
        )

        return response.choices[0].message.content