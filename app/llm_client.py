import os
import json

from groq import Groq
from dotenv import load_dotenv


load_dotenv()


class LLMClient:

    def __init__(self):

        api_key = os.getenv("GROQ_API_KEY")

        if not api_key:
            raise ValueError(
                "GROQ_API_KEY not found in .env"
            )

        self.client = Groq(
            api_key=api_key
        )

        self.model = (
            "openai/gpt-oss-20b"
        )

    def generate_suggestions(
        self,
        meeting_context
    ):

        system_prompt = """
You are an AI meeting copilot.

Your job is to help the user participate
naturally in a live corporate meeting.

Analyze the recent conversation and determine:

1. What is the latest relevant question?
2. Whether the question appears to require
   a response from the user.
3. A concise, natural answer the user could say.
4. One useful follow-up question if appropriate.
5. The most important point from the discussion.

IMPORTANT:

- Do not invent facts.
- Use only information available in the context.
- If information is insufficient, say so.
- Answers should sound natural when spoken.
- Keep the answer concise.
- Do not generate an answer if no meaningful
  question requires the user's response.

Return ONLY valid JSON.

Use exactly this structure:

{
  "question_detected": true,
  "question": "...",
  "answer": "...",
  "follow_up": "...",
  "key_point": "...",
  "confidence": 0.0
}

If there is no question requiring the user's
response:

{
  "question_detected": false,
  "question": "",
  "answer": "",
  "follow_up": "",
  "key_point": "...",
  "confidence": 0.0
}
"""

        user_prompt = f"""
Here is the recent meeting conversation:

--- BEGIN CONTEXT ---

{meeting_context}

--- END CONTEXT ---

Analyze the conversation and return
the required JSON.
"""

        response = self.client.chat.completions.create(
            model=self.model,

            messages=[
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": user_prompt
                }
            ],

            temperature=0.2,

            response_format={
                "type": "json_object"
            }
        )

        content = (
            response
            .choices[0]
            .message
            .content
        )

        try:

            result = json.loads(content)

        except json.JSONDecodeError:

            raise ValueError(
                "LLM returned invalid JSON:\n"
                + content
            )

        return result