"""
cost.py — Token usage and cost tracking across LLM API calls.
"""

from dataclasses import dataclass

from eda_agent.llm.base import LLMResponse

# Per-model cost table: (input $/M tokens, output $/M tokens)
COST_TABLE = {
    "gemini-2.5-pro":       (1.25,  10.00),
    "gemini-2.5-flash":     (0.075,  0.30),
    "gemini-2.5-flash-lite": (0.02,  0.08),
    "gemini-2.0-flash":     (0.10,   0.40),
    "deepseek-v4-pro":      (0.0,    0.0),   # free tier on NVIDIA NIM
    "deepseek-v3":          (0.27,   1.10),
    "default":              (1.0,    4.0),
}


@dataclass
class CostTracker:
    """Accumulates token usage across all API calls in a single agent cycle.

    Usage:
        tracker = CostTracker(model="gemini-2.5-flash")
        tracker.add(response)  # after each LLM call
        print(tracker.summary())
    """

    model: str = "default"
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, response: LLMResponse) -> None:
        """Add token counts from an LLM response."""
        self.input_tokens += response.input_tokens
        self.output_tokens += response.output_tokens

    def cost_usd(self) -> float:
        """Estimate cost in USD based on the model's pricing."""
        m = self.model.lower()
        for key, (in_rate, out_rate) in COST_TABLE.items():
            if key in m:
                return ((self.input_tokens / 1e6 * in_rate) +
                        (self.output_tokens / 1e6 * out_rate))
        # Fallback to default pricing
        in_r, out_r = COST_TABLE["default"]
        return (self.input_tokens / 1e6 * in_r) + (self.output_tokens / 1e6 * out_r)

    def summary(self) -> str:
        """One-line summary of token usage and estimated cost."""
        return (
            f"Tokens — in: {self.input_tokens:,}  out: {self.output_tokens:,}"
            f"  |  ~${self.cost_usd():.4f}"
        )
