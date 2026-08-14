from llm_client import LLMClient


client = LLMClient()

conversation = """
OTHERS: We are planning to launch the application
next month.

OTHERS: The backend is currently using PostgreSQL.

OTHERS: What do you think could be the biggest
technical risk?

YOU: We should probably consider database scaling
and API rate limits.
"""

result = client.generate_suggestions(
    conversation
)

print("\n")
print("=" * 60)
print("LLM SUGGESTION")
print("=" * 60)
print(result)
print("=" * 60)