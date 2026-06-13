"""
gemini.py — Google Gemini chat session wrapper.

Wraps google.genai's stateful chat session to return LLMResponse objects
with token usage metadata.
"""

from eda_agent.llm.base import LLMResponse


class GeminiSession:
    """Stateful chat session using Google Gemini (via genai SDK).

    Args:
        session: A google.genai chat session created via gc.chats.create().
    """

    def __init__(self, session):
        self._session = session

    def send_message(self, text: str) -> LLMResponse:
        """Send a message and return the response with token counts."""
        resp = self._session.send_message(text)

        # Extract token usage from response metadata
        meta = getattr(resp, "usage_metadata", None)
        in_tok = getattr(meta, "prompt_token_count", 0) or 0 if meta else 0
        out_tok = getattr(meta, "candidates_token_count", 0) or 0 if meta else 0

        return LLMResponse(resp.text, in_tok, out_tok)
