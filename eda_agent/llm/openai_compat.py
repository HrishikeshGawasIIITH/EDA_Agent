"""
openai_compat.py — Multi-turn streaming chat for OpenAI-compatible APIs.

Supports NVIDIA NIM endpoints (DeepSeek V4 Pro, Nemotron, Qwen3) with:
  - Rate-limit retry with exponential backoff (honours Retry-After header)
  - Transient error recovery (502, 503)
  - Streaming response aggregation
  - Thinking toggle for supported models
"""

import re
import time

from eda_agent.llm.base import LLMResponse


class OpenAISession:
    """Stateful multi-turn chat for OpenAI-compatible endpoints.

    History is maintained as a messages list so each call includes the
    full prior context — equivalent to Gemini's stateful chat session.

    Args:
        client: An OpenAI client instance (configured with base_url).
        model: Model identifier (e.g. "deepseek-ai/deepseek-v4-pro").
        system: System prompt text.
        temperature: Sampling temperature.
        max_tokens: Max output tokens per response.
    """

    def __init__(self, client, model: str, system: str,
                 temperature: float = 0.1, max_tokens: int = 8192):
        self._client = client
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._messages: list[dict] = [{"role": "system", "content": system}]

        # Some models support disabling internal chain-of-thought
        self._extra_body = (
            {"chat_template_kwargs": {"thinking": False}}
            if any(x in model for x in ("deepseek", "nemotron", "qwen3"))
            else {}
        )

    def send_message(self, text: str) -> LLMResponse:
        """Send a user message and return the assistant's response."""
        self._messages.append({"role": "user", "content": text})
        response = self._call_with_retry()
        self._messages.append({"role": "assistant", "content": response.text})
        return response

    # ── Retry logic ───────────────────────────────────────────────────────

    def _call_with_retry(self) -> LLMResponse:
        """Retry on rate limits (indefinitely) and transient errors (up to 5x)."""
        from openai import RateLimitError

        rl_attempt = 0
        transient_attempt = 0

        while True:
            try:
                return self._stream_once()

            except RateLimitError as exc:
                wait = self._parse_retry_after(exc) or min(30 * (2 ** rl_attempt), 600)
                rl_attempt += 1
                print(f"\n  Rate limit (attempt {rl_attempt}) — waiting {wait:.0f}s ...")
                for remaining in range(int(wait), 0, -10):
                    print(f"  ...resuming in {remaining}s ", end="\r", flush=True)
                    time.sleep(min(10, remaining))
                print(f"  Retrying now.{'':40}")

            except Exception as exc:
                transient_attempt += 1
                if transient_attempt > 5:
                    raise
                wait = 5 * transient_attempt
                print(f"\n  API error ({transient_attempt}/5): "
                      f"{str(exc)[:120]} — waiting {wait}s...")
                time.sleep(wait)

    def _stream_once(self) -> LLMResponse:
        """Make a single streaming API call and aggregate the response."""
        stream = self._client.chat.completions.create(
            model=self._model,
            messages=self._messages,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            stream=True,
            extra_body=self._extra_body or None,
        )

        chunks: list[str] = []
        in_tokens = out_tokens = 0

        for chunk in stream:
            if not getattr(chunk, "choices", None):
                # Usage chunk (no choices, just token counts)
                if hasattr(chunk, "usage") and chunk.usage:
                    in_tokens = getattr(chunk.usage, "prompt_tokens", 0)
                    out_tokens = getattr(chunk.usage, "completion_tokens", 0)
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content is not None:
                chunks.append(delta.content)

        return LLMResponse("".join(chunks), in_tokens, out_tokens)

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _parse_retry_after(exc) -> float | None:
        """Extract wait seconds from rate-limit error headers or message body."""
        # Check HTTP headers first
        hdrs = getattr(getattr(exc, "response", None), "headers", None)
        if hdrs:
            val = hdrs.get("retry-after") or hdrs.get("Retry-After")
            if val:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass

        # Fall back to parsing the error message body
        m = re.search(r"try again in (\d+\.?\d*)\s*(ms|s|m)\b", str(exc), re.IGNORECASE)
        if m:
            amount, unit = float(m.group(1)), m.group(2).lower()
            if unit == "ms":
                return max(1.0, amount / 1000)
            if unit == "m":
                return amount * 60
            return max(1.0, amount)
        return None
