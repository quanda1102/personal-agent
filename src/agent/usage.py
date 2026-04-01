"""
openclawd.core.loop.usage
──────────────────────────
Provider-agnostic token usage and cost tracking.

Every provider adapter maps its response onto TurnUsage.
The loop accumulates TurnUsage instances into RunUsage.

Cost model:
  Prices are per-million tokens ($/MTok).
  Cache read tokens are cheaper than regular input tokens.
  Cache write tokens have a small premium.
  All prices are approximate — update MODEL_PRICING as needed.

Adding a new model: add one entry to MODEL_PRICING. Nothing else changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple


# ── Model pricing table ────────────────────────────────────────────────────────
# All prices in USD per million tokens

class _Price(NamedTuple):
    input:       float   # regular input $/MTok
    output:      float   # output $/MTok
    cache_write: float   # cache write $/MTok  (0 if not supported)
    cache_read:  float   # cache read $/MTok   (0 if not supported)


MODEL_PRICING: dict[str, _Price] = {
    # ── Anthropic Claude — current (March 2026) ───────────────────────────────
    # Cache pricing: write = 1.25x input, read = 0.10x input
    #
    # Claude 4.6 family
    "claude-opus-4-6":              _Price( 5.00,  25.00,   6.25,  0.50),
    "claude-sonnet-4-6":            _Price( 3.00,  15.00,   3.75,  0.30),
    # Claude 4.5 family
    "claude-opus-4-5":              _Price( 5.00,  25.00,   6.25,  0.50),
    "claude-sonnet-4-5":            _Price( 3.00,  15.00,   3.75,  0.30),
    "claude-haiku-4-5":             _Price( 1.00,   5.00,   1.25,  0.10),
    "claude-haiku-4-5-20251001":    _Price( 1.00,   5.00,   1.25,  0.10),
    # Claude 4.x legacy
    "claude-opus-4-1":              _Price(15.00,  75.00,  18.75,  1.50),
    "claude-sonnet-4":              _Price( 3.00,  15.00,   3.75,  0.30),
    # Claude 3.x (legacy fallback)
    "claude-3-5-sonnet-20241022":   _Price( 3.00,  15.00,   3.75,  0.30),
    "claude-haiku-3-5":             _Price( 0.80,   4.00,   1.00,  0.08),
    "claude-haiku-3":               _Price( 0.25,   1.25,   0.30,  0.03),

    # ── OpenAI — current (March 2026) ─────────────────────────────────────────
    # Cache pricing: read = 0.50x input (automatic, no write premium)
    #
    # Flagship
    "gpt-5":                        _Price( 1.25,  10.00,   0.00,  0.625),
    "gpt-4o":                       _Price( 2.50,  10.00,   0.00,  1.25),
    # Long-context
    "gpt-4.1":                      _Price( 2.00,   8.00,   0.00,  1.00),
    "gpt-4.1-mini":                 _Price( 0.40,   1.60,   0.00,  0.20),
    "gpt-4.1-nano":                 _Price( 0.10,   0.40,   0.00,  0.05),
    # Reasoning
    "o3":                           _Price( 2.00,   8.00,   0.00,  1.00),
    "o4-mini":                      _Price( 1.10,   4.40,   0.00,  0.55),
    # Budget
    "gpt-4o-mini":                  _Price( 0.15,   0.60,   0.00,  0.075),
    "gpt-5-mini":                   _Price( 0.25,   1.00,   0.00,  0.125),

    # ── Google Gemini — current (March 2026) ──────────────────────────────────
    "gemini-2.5-pro":               _Price( 1.25,  10.00,   0.00,  0.00),
    "gemini-2.5-flash":             _Price( 0.15,   0.60,   0.00,  0.00),
    "gemini-2.0-flash":             _Price( 0.10,   0.40,   0.00,  0.00),
}

_UNKNOWN_PRICE = _Price(0.0, 0.0, 0.0, 0.0)


def compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Compute estimated cost in USD for a given token usage."""
    p = MODEL_PRICING.get(model, _UNKNOWN_PRICE)
    mtok = 1_000_000
    return (
        (input_tokens       * p.input)       / mtok
        + (output_tokens    * p.output)      / mtok
        + (cache_write_tokens * p.cache_write) / mtok
        + (cache_read_tokens  * p.cache_read)  / mtok
    )


# ── Per-turn usage ─────────────────────────────────────────────────────────────

@dataclass
class TurnUsage:
    """
    Token usage for a single LLM API call (one turn in the agentic loop).
    Providers map their response fields onto this.
    """
    turn:               int   = 0
    input_tokens:       int   = 0
    output_tokens:      int   = 0
    cache_write_tokens: int   = 0
    cache_read_tokens:  int   = 0
    model:              str   = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def estimated_cost_usd(self) -> float:
        return compute_cost(
            self.model,
            self.input_tokens,
            self.output_tokens,
            self.cache_write_tokens,
            self.cache_read_tokens,
        )

    def __repr__(self) -> str:
        return (
            f"TurnUsage(turn={self.turn}, "
            f"in={self.input_tokens}, out={self.output_tokens}, "
            f"cache_r={self.cache_read_tokens}, cache_w={self.cache_write_tokens}, "
            f"cost=${self.estimated_cost_usd:.6f})"
        )


# ── Full run usage ─────────────────────────────────────────────────────────────

@dataclass
class RunUsage:
    """
    Accumulated token usage and cost across an entire agentic run.
    Built up by calling .add_turn() after each LLM API call.
    """
    model:              str             = ""
    turns:              list[TurnUsage] = field(default_factory=list)

    # Accumulated totals
    total_input_tokens:       int   = 0
    total_output_tokens:      int   = 0
    total_cache_write_tokens: int   = 0
    total_cache_read_tokens:  int   = 0
    total_tool_calls:         int   = 0

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def estimated_cost_usd(self) -> float:
        return compute_cost(
            self.model,
            self.total_input_tokens,
            self.total_output_tokens,
            self.total_cache_write_tokens,
            self.total_cache_read_tokens,
        )

    def add_turn(self, turn: TurnUsage) -> None:
        self.turns.append(turn)
        self.total_input_tokens       += turn.input_tokens
        self.total_output_tokens      += turn.output_tokens
        self.total_cache_write_tokens += turn.cache_write_tokens
        self.total_cache_read_tokens  += turn.cache_read_tokens
        if not self.model and turn.model:
            self.model = turn.model

    def summary_line(self) -> str:
        """Compact one-line summary for display."""
        cache_info = ""
        if self.total_cache_read_tokens:
            cache_info = f" | cache_r={self.total_cache_read_tokens}"
        if self.total_cache_write_tokens:
            cache_info += f" cache_w={self.total_cache_write_tokens}"
        return (
            f"tokens: in={self.total_input_tokens} "
            f"out={self.total_output_tokens}{cache_info} "
            f"| tools={self.total_tool_calls} "
            f"| cost=${self.estimated_cost_usd:.4f}"
        )

    def to_dict(self) -> dict:
        return {
            "model":                    self.model,
            "total_input_tokens":       self.total_input_tokens,
            "total_output_tokens":      self.total_output_tokens,
            "total_cache_write_tokens": self.total_cache_write_tokens,
            "total_cache_read_tokens":  self.total_cache_read_tokens,
            "total_tool_calls":         self.total_tool_calls,
            "estimated_cost_usd":       self.estimated_cost_usd,
            "turns": [
                {
                    "turn":               t.turn,
                    "input_tokens":       t.input_tokens,
                    "output_tokens":      t.output_tokens,
                    "cache_write_tokens": t.cache_write_tokens,
                    "cache_read_tokens":  t.cache_read_tokens,
                    "cost_usd":           t.estimated_cost_usd,
                }
                for t in self.turns
            ],
        }

    def __repr__(self) -> str:
        return f"RunUsage({self.summary_line()})"