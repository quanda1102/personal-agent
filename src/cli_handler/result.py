"""
openclawd.core.runner.output
─────────────────────────────
Result — the single return type for every command execution, and the home
of Layer 2: the LLM Presentation Layer.

Two-layer architecture
──────────────────────
  Layer 1 (cli_handler.router + cli_handler.dispatch)
    Pure Unix execution semantics. Commands run, pipes flow, exit codes set.
    stdout is raw bytes-as-text. No truncation. No footer. No metadata.
    The pipe chain uses result.stdout directly — clean, lossless, composable.

  Layer 2 (this file — Result.render())
    Fires exactly once, after the full chain completes.
    Translates raw execution output into what an LLM can actually use.

    Four mechanisms (in order):
      A. Binary guard     — detect binary data, redirect to right command
      B. Overflow mode    — truncate + spill to /tmp, add explore hints
      D. stderr attach    — surface stderr so agent knows why things fail
      C. Metadata footer  — [exit:N | Xms] always appended last

    The order matters: binary guard short-circuits before overflow.
    Footer is always last — it must not pollute pipe data if it ran mid-chain.

Why Layer 2 is necessary
─────────────────────────
  Constraint A: LLMs have finite, expensive context windows.
    A 5,000-line log stuffed into context pushes earlier conversation out.
    The agent "forgets." Overflow mode gives it a map, not the territory.

  Constraint B: LLMs only process text.
    Binary bytes through a tokenizer produce meaningless high-entropy tokens
    that disrupt attention on surrounding valid content. The binary guard
    catches this before it reaches the LLM.

Usage:
    result = executor.exec(command)   # Layer 1 — raw
    text   = result.render()          # Layer 2 — LLM-ready
"""

from __future__ import annotations

import itertools
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path


# ── Layer 2 tuning constants ────────────────────────────────────────────────────

_OVERFLOW_LINES = 200          # truncate at 200 lines
_OVERFLOW_BYTES = 50_000       # or 50 KB, whichever triggers first
_BINARY_THRESHOLD = 0.05       # >5% control/replacement chars → binary
_OVERFLOW_DIR = Path(tempfile.gettempdir()) / "home-agent-overflow"

# Thread-safe monotonic counter for spill file names (GIL-protected in CPython)
_overflow_seq: itertools.count = itertools.count(1)


# ── Result ─────────────────────────────────────────────────────────────────────

@dataclass
class Result:
    """
    Output of a single command execution.  Lives in Layer 1 as a plain data
    object.  Becomes Layer 2 output when render() is called.

    Fields:
      stdout      — raw text output from the command.  Mutable — pipe
                    emulation concatenates left-stdout into this field.
      stderr      — captured stderr.  Attached to render() output on failure
                    so the agent can see WHY something went wrong, not just
                    that it did.  Never silently dropped.
      exit        — exit code.  0 = success.  Conventions the LLM already
                    knows: 1 = error, 127 = not found, 124 = timeout.
      elapsed_ms  — wall-clock execution time in milliseconds.
      image       — optional raw image bytes (PNG/JPEG/…) for vision results.
                    When set, the loop passes it to format_tool_result() for
                    image-aware multimodal rendering.

    The LLM never sees this object — it only ever sees render().
    """
    stdout:     str          = ""
    stderr:     str          = ""
    exit:       int          = 0
    elapsed_ms: float        = 0.0
    image:      bytes | None = field(default=None, repr=False)

    def render(self) -> str:
        """
        Layer 2 presentation pipeline.

        Fires once after the full pipe/chain completes.
        Never called mid-pipe — Layer 1 uses .stdout directly.

        Pipeline:
          A → binary guard
          B → overflow mode
          D → stderr attachment
          C → metadata footer  (always last)
        """
        text = self.stdout

        # ── A: Binary guard ────────────────────────────────────────────────────
        # If stdout contains significant binary data, the LLM cannot process it.
        # Return a helpful error that steers the agent toward the right command.
        if _is_binary(text):
            size_kb = len(text.encode("utf-8", errors="replace")) / 1024
            if _looks_like_image(text):
                guidance = (
                    f"[error] binary image content ({size_kb:.0f}KB) — "
                    f"use: see <filename>  to view images"
                )
            else:
                guidance = (
                    f"[error] binary file content ({size_kb:.0f}KB) — "
                    f"this tool only handles text. "
                    f"For hex inspection: xxd <filename> | head 20"
                )
            return _footer(guidance, self.exit, self.elapsed_ms)

        # ── B: Overflow mode ───────────────────────────────────────────────────
        # Large output overwhelms the context window and pushes earlier
        # conversation out.  Spill the full content to /tmp and give the agent
        # a map + the tools it already knows to explore it.
        lines    = text.splitlines()
        byte_len = len(text.encode("utf-8"))
        if len(lines) > _OVERFLOW_LINES or byte_len > _OVERFLOW_BYTES:
            text = _apply_overflow(text, lines, byte_len)

        # ── D: stderr attachment ───────────────────────────────────────────────
        # stderr is the information agents need most, precisely when commands fail.
        # Attach it whenever present — never silently drop it.
        if self.stderr:
            stripped = self.stderr.strip()
            if stripped:
                if text:
                    text = text.rstrip("\n") + f"\n[stderr] {stripped}"
                else:
                    text = f"[stderr] {stripped}"

        # ── C: Metadata footer ─────────────────────────────────────────────────
        # Exit code + duration appended as the final line.  Gives the agent
        # a success/failure signal and a cost-awareness signal on every call.
        # Added here, after pipe chain is complete — never polutes pipe data.
        return _footer(text, self.exit, self.elapsed_ms)

    def __bool__(self) -> bool:
        """True when exit == 0 (command succeeded)."""
        return self.exit == 0


# ── Convenience constructors ────────────────────────────────────────────────────

def ok(stdout: str = "", elapsed_ms: float = 0.0) -> Result:
    """Return a successful Result (exit=0)."""
    return Result(stdout=stdout, exit=0, elapsed_ms=elapsed_ms)


def err(stdout: str = "", elapsed_ms: float = 0.0, stderr: str = "", exit: int = 1) -> Result:
    """
    Return a failed Result.

    exit codes follow standard Unix conventions:
      1   — general runtime error (default)
      2   — misuse / bad arguments (wrong flags, missing required arg)
      127 — command not found
    """
    return Result(stdout=stdout, stderr=stderr, exit=exit, elapsed_ms=elapsed_ms)


# ── Timer ──────────────────────────────────────────────────────────────────────

class Timer:
    """
    Context manager for wall-clock execution timing.

    Usage:
        with Timer() as t:
            result = subprocess.run(...)
        elapsed = t.elapsed_ms
    """

    def __init__(self) -> None:
        self._start:     float = 0.0
        self.elapsed_ms: float = 0.0

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_) -> None:
        self.elapsed_ms = (time.perf_counter() - self._start) * 1000


# ── Layer 2 internals ──────────────────────────────────────────────────────────

def _footer(text: str, exit_code: int, elapsed_ms: float) -> str:
    """Append the [exit:N | Xms] footer. Always the final line."""
    tag = f"[exit:{exit_code} | {elapsed_ms:.0f}ms]"
    return (text.rstrip("\n") + "\n" + tag) if text else tag


def _is_binary(text: str) -> bool:
    """
    Heuristic: does this string contain significant binary data?

    Checks (in order of cost):
      1. Null bytes — definitive binary indicator
      2. Ratio of control characters + UTF-8 replacement chars in first 4 KB
         If > _BINARY_THRESHOLD (5%) → binary
    """
    if not text:
        return False
    if "\x00" in text:
        return True
    sample = text[:4096]
    bad = sum(
        1 for c in sample
        if (ord(c) < 32 and c not in "\t\n\r\x0b\x0c") or c == "\ufffd"
    )
    return (bad / len(sample)) > _BINARY_THRESHOLD


def _looks_like_image(text: str) -> bool:
    """
    Try to recognise common image magic bytes in binary-decoded text.
    Used by the binary guard to give a better redirect message.
    """
    try:
        b = text[:16].encode("latin-1", errors="replace")
    except Exception:
        return False
    return (
        b[:4] == b"\x89PNG"                             # PNG
        or b[:3] == b"\xff\xd8\xff"                     # JPEG
        or b[:6] in (b"GIF87a", b"GIF89a")              # GIF
        or (b[:4] == b"RIFF" and b[8:12] == b"WEBP")    # WEBP
        or b[:2] == b"BM"                               # BMP
    )


def _apply_overflow(text: str, lines: list[str], byte_len: int) -> str:
    """
    Truncate large output and spill the full content to a temp file.

    Returns the first _OVERFLOW_LINES lines plus a navigation block that
    gives the agent the file path and the grep/tail commands it already knows.

    Key insight: the LLM already knows how to use grep, head, tail to navigate
    files.  Overflow mode turns "large data exploration" into a skill the LLM
    already has — no new commands needed.
    """
    seq_n = next(_overflow_seq)
    _OVERFLOW_DIR.mkdir(parents=True, exist_ok=True)
    spill = _OVERFLOW_DIR / f"cmd-{seq_n}.txt"

    try:
        spill.write_text(text, encoding="utf-8")
        spill_path = str(spill)
    except Exception as e:
        spill_path = f"(could not write spill file: {e})"

    truncated = "\n".join(lines[:_OVERFLOW_LINES])
    size_label = f"{byte_len / 1024:.1f}KB" if byte_len < 1_048_576 else f"{byte_len / 1_048_576:.1f}MB"

    nav = (
        f"\n--- output truncated ({len(lines)} lines, {size_label}) ---\n"
        f"Full output saved to: {spill_path}\n"
        f"Explore:  cat {spill_path} | grep <pattern>\n"
        f"          cat {spill_path} | tail 100\n"
        f"          cat {spill_path} | head 50"
    )
    return truncated + nav
