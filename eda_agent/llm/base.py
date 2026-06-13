"""
base.py — Shared types for the LLM provider layer.
"""


class LLMResponse:
    """Unified response object wrapping any LLM provider's output.

    Attributes:
        text: The generated text content.
        input_tokens: Number of input/prompt tokens used.
        output_tokens: Number of output/completion tokens generated.
    """

    def __init__(self, text: str, input_tokens: int = 0, output_tokens: int = 0):
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
