"""
The run(command="...") entry point — the single tool the LLM calls.

Handles Unix-style command chains:
  |    pipe     stdout of left becomes stdin of right
  &&   and      run right only if left exit:0
  ||   or       run right only if left exit:non-0
  ;    seq       run right regardless

Examples:
  run("cat notes.md")
  run("memory search 'breakfast' | head 5")
  run("memory store 'user likes pho' && memory count")
  run("cat config.yaml || echo 'config not found, using defaults'")

The chain parser is intentionally simple — it splits on operators at the
top level only (not inside quoted strings) and executes left to right.
Pipe is emulated: stdout of the left command is passed as stdin context
to the right command (appended as a prefix argument for custom commands,
passed via subprocess stdin for UNIX commands).
"""

from __future__ import annotations

import re

from .result import Result, ok, err, Timer
# TODO: implement dispatch() and tokenize() in src/cli_handler/dispatch.py
from .dispatch import dispatch, tokenize

# ── Heredoc pre-processing ─────────────────────────────────────────────────────
#
# Shell heredoc syntax (<<EOF ... EOF) cannot be handled by a chain parser
# because the content between the markers contains unescaped |, ;, &&, etc.
# We detect the pattern early and bypass chain splitting entirely.

_HEREDOC_RE = re.compile(
    r"^(?P<prefix>.+?)\s+<<['\"]?(?P<marker>\w+)['\"]?\n(?P<content>.*)\n(?P=marker)\s*$",
    re.DOTALL,
)


def _handle_heredoc(command: str) -> Result | None:
    """
    Detect shell heredoc syntax and handle it directly, bypassing chain parsing.

    Supported patterns:
      write <path> <<'EOF'   →  write content to path   (overwrite)
      cat > <path> <<'EOF'   →  write content to path   (overwrite)
      cat >> <path> <<'EOF'  →  append content to path  (append)

    Returns Result if heredoc detected, None to fall through to normal parsing.

    Why this is needed:
      The LLM writes  write file <<'EOF'\nline with | pipes\nEOF  which is
      valid bash but cannot survive our chain splitter — the | inside the
      heredoc body gets split as a pipe operator.  We must consume the whole
      heredoc as an atomic unit before any splitting happens.
    """
    m = _HEREDOC_RE.match(command)
    if not m:
        return None

    prefix  = m.group("prefix").strip()
    content = m.group("content")

    # ── write <path> ──────────────────────────────────────────────────────────
    write_m = re.match(r'^write\s+(.+)$', prefix)
    if write_m:
        path = Path(write_m.group(1).strip())
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return ok(f"wrote {len(content)} chars → {path}")
        except Exception as e:
            return err(f"write: {e}")

    # ── cat > <path>  (overwrite) ─────────────────────────────────────────────
    cat_w = re.match(r'^cat\s+>\s+(.+)$', prefix)
    if cat_w:
        path = Path(cat_w.group(1).strip())
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return ok(f"wrote {len(content)} chars → {path}")
        except Exception as e:
            return err(f"write: {e}")

    # ── cat >> <path>  (append) ───────────────────────────────────────────────
    cat_a = re.match(r'^cat\s+>>\s+(.+)$', prefix)
    if cat_a:
        path = Path(cat_a.group(1).strip())
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(content + "\n")
            return ok(f"appended {len(content)} chars → {path}")
        except Exception as e:
            return err(f"append: {e}")

    # Unsupported heredoc (python3 - <<PY, etc.) — let it fail naturally
    return None


# ── Chain operator parsing ─────────────────────────────────────────────────────

def _split_chain(command: str) -> list[tuple[str, str]]:
    """
    Split a command string into [(operator, segment), ...].
    The first segment has operator="" (no preceding operator).

    "a | b && c" → [("", "a"), ("|", "b"), ("&&", "c")]

    QUOTE-AWARE: operators inside single or double quoted strings are
    treated as literal text and never act as chain separators.

    Examples:
      'echo "a;b" && cat f'  → [("", 'echo "a;b"'), ("&&", "cat f")]
      "write f 'x; y' | cat" → [("", "write f 'x; y'"), ("|", "cat")]

    Why this matters:
      The LLM writes content like  write recipe.md "...; cover and steam..."
      Without quote awareness, "; cover" would be split off and "cover"
      would be dispatched as an unknown command — causing spurious failures.
    """
    segments: list[tuple[str, str]] = []
    current: list[str] = []
    op = ""
    i = 0
    in_single = False
    in_double = False

    while i < len(command):
        ch = command[i]

        # ── Track quote state ──────────────────────────────────────────────────
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
            i += 1
            continue

        # ── Inside quotes: everything is literal ──────────────────────────────
        if in_single or in_double:
            current.append(ch)
            i += 1
            continue

        # ── Outside quotes: check for chain operators ──────────────────────────
        # Check two-character operators first to avoid && → "&" + "&"
        if command[i : i + 2] in ("&&", "||"):
            seg = "".join(current).strip()
            if seg:
                segments.append((op, seg))
            op = command[i : i + 2]
            current = []
            i += 2
            continue
        if ch in ("|", ";"):
            seg = "".join(current).strip()
            if seg:
                segments.append((op, seg))
            op = ch
            current = []
            i += 1
            continue

        current.append(ch)
        i += 1

    # Final segment
    seg = "".join(current).strip()
    if seg:
        segments.append((op, seg))

    return segments


# ── Pipe emulation ─────────────────────────────────────────────────────────────

def _apply_pipe(left_stdout: str, right_command: str) -> Result:
    """
    Emulate pipe: inject left_stdout as context into the right command.

    For UNIX commands (grep, head, tail, cat): pass via subprocess stdin.
    For custom commands (memory, skills): prepend stdin content to args
    so the command can filter/process it.

    This is "good enough" pipe emulation — real shells do it at the OS level,
    we do it at the string level. Works well for the typical agent patterns.
    """
    tokens = tokenize(right_command)
    if not tokens:
        return err("pipe: empty right-hand command")

    cmd_name = tokens[0].lower()

    # ── write / append: accept piped stdin as file content ────────────────────
    # Enables:  printf "line1\nline2" | write path
    #           cat existing.md | write copy.md
    # This is the most reliable multi-line file writing idiom for the LLM.
    if cmd_name == "write" and len(tokens) >= 2:
        path = Path(tokens[1])
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(left_stdout, encoding="utf-8")
            return ok(f"wrote {len(left_stdout)} chars → {path}")
        except Exception as e:
            return Result(stdout=f"[error] write: {e}", exit=1)

    if cmd_name == "append" and len(tokens) >= 2:
        path = Path(tokens[1])
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(left_stdout)
            return ok(f"appended {len(left_stdout)} chars → {path}")
        except Exception as e:
            return Result(stdout=f"[error] append: {e}", exit=1)

    # ── memory store: accept piped stdin as the text to store ─────────────────
    # Enables:  echo "user likes pho" | memory store
    #           cat notes.md | memory store
    if cmd_name in ("memory", "mem") and len(tokens) >= 2 and tokens[1] == "store":
        store_tokens = [tokens[0], "store", left_stdout.strip()]
        return dispatch(store_tokens)

    # ── UNIX commands: pass via actual subprocess stdin ────────────────────
    if cmd_name in ("grep", "head", "tail", "sort", "wc", "awk", "sed", "uniq"):
        import subprocess
        with Timer() as t:
            try:
                proc = subprocess.run(
                    [cmd_name] + tokens[1:],
                    input=left_stdout,
                    capture_output=True,
                    text=True,
                )
                # Keep stderr separate — render() attaches it; don't merge into stdout
                # or it will flow into the next pipe stage as data.
                return Result(
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                    exit=proc.returncode,
                    elapsed_ms=t.elapsed_ms,
                )
            except FileNotFoundError:
                return Result(stdout=f"[error] binary not found: {cmd_name}", exit=127)

    # ── Custom commands: run normally, left stdout was the input ──────────
    # Only right command's output survives — this matches real pipe semantics.
    # (write and append are already handled above and consume left_stdout.)
    return dispatch(tokens)


# ── Main entry point ───────────────────────────────────────────────────────────

def run(command: str) -> Result:
    """
    Execute a command string as the LLM would write it.

    This is the single function exposed as the LLM's tool:
        run(command="memory search 'pho' | head 3")

    Returns a Result with stdout text and exit code.
    The caller (agent loop) reads result.render() to get the
    [exit:N | Xms] footer appended.
    """
    command = command.strip()
    if not command:
        return err("run: empty command")

    # ── Heredoc pre-processing ─────────────────────────────────────────────────
    # Must happen BEFORE chain splitting — heredoc body contains unescaped
    # |, ;, etc. that would be split as chain operators otherwise.
    heredoc_result = _handle_heredoc(command)
    if heredoc_result is not None:
        return heredoc_result

    chain = _split_chain(command)
    if not chain:
        return err("run: could not parse command")

    current_result: Result | None = None

    for op, segment in chain:
        segment = segment.strip()
        if not segment:
            continue

        # Decide whether to execute this segment based on the operator + last exit.
        # IMPORTANT: use `is not None` — NOT a truthiness check.  Result.__bool__
        # returns False when exit != 0, so `if current_result and ...` would
        # short-circuit and never reach the exit check for failed results.
        if op == "&&":
            if current_result is not None and current_result.exit != 0:
                # Left failed — skip right
                continue
        elif op == "||":
            if current_result is not None and current_result.exit == 0:
                # Left succeeded — skip right
                continue
        # "|" and ";" and "" always execute

        if op == "|" and current_result is not None:
            # Pipe: pass left stdout into right command
            current_result = _apply_pipe(current_result.stdout, segment)
        else:
            tokens = tokenize(segment)
            current_result = dispatch(tokens)

    # NOTE: do NOT use `current_result or err(...)` here.
    # Result.__bool__ returns False when exit != 0, so Python's `or` would
    # silently replace a real failed result with this fallback message —
    # swallowing the actual error the agent needs to see.
    return current_result if current_result is not None else err("run: no commands executed")


def run_rendered(command: str) -> str:
    """
    Convenience wrapper — returns the fully rendered string the LLM reads,
    including the [exit:N | Xms] footer.
    """
    return run(command).render()


# ── Standalone test harness ────────────────────────────────────────────────────

if __name__ == "__main__":
    """
    Run with:
        python -m Home_agent.src.cli_handler.router
        python router.py  (from the cli_handler directory)

    Tests chain parsing, pipe emulation, heredoc handling and operator semantics.
    Uses a temp directory — no real filesystem state required (except UNIX binaries).
    """
    import os
    import sys
    import tempfile
    from pathlib import Path

    _PASS = "\033[32mPASS\033[0m"
    _FAIL = "\033[31mFAIL\033[0m"

    def _check(label: str, command: str, want_exit: int, want_contains: str = "") -> bool:
        r = run(command)
        exit_ok     = r.exit == want_exit
        content_ok  = (want_contains in r.stdout) if want_contains else True
        ok_ = exit_ok and content_ok
        sym = _PASS if ok_ else _FAIL
        detail = ""
        if not exit_ok:
            detail += f" exit:{r.exit}≠{want_exit}"
        if not content_ok:
            detail += f" missing {want_contains!r}"
        print(f"  {sym}{detail:<20}  {command!r}")
        if not ok_:
            if r.stdout:
                print(f"             stdout: {r.stdout[:120]!r}")
            if r.stderr:
                print(f"             stderr: {r.stderr[:120]!r}")
        return ok_

    passed = failed = 0

    with tempfile.TemporaryDirectory() as _tmp, \
         tempfile.TemporaryDirectory() as _mem_dir, \
         tempfile.TemporaryDirectory() as _skills_dir:

        os.environ["HOMEAGENT_MEMORY_DB"]   = str(Path(_mem_dir) / "test.db")
        os.environ["OPENCLAWD_SKILLS_ROOT"] = _skills_dir

        # Create a test skill
        _w = Path(_skills_dir) / "weather"
        _w.mkdir()
        (_w / "SKILL.md").write_text(
            "---\ndescription: Weather skill\n---\n# Weather\nGet weather.\n"
        )

        # Create a test file for pipe tests
        _tf = Path(_tmp) / "sample.txt"
        _tf.write_text("line1\nline2\nline3 hello\nline4\n", encoding="utf-8")

        tests: list[tuple[str, str, int, str]] = [
            # ── Empty / trivial ───────────────────────────────────────────────
            ("empty cmd",          "",                              1, ""),
            ("single cmd",         "echo hello",                    0, "hello"),

            # ── && operator ───────────────────────────────────────────────────
            ("&& success-success",  "true && echo ok",              0, "ok"),
            ("&& fail-skip",        "false && echo skip",           1, ""),
            ("&& chain 3",          "true && true && echo three",   0, "three"),

            # ── || operator ───────────────────────────────────────────────────
            ("|| fail-run",         "false || echo fallback",       0, "fallback"),
            ("|| success-skip",     "true || echo skip",            0, ""),

            # ── ; operator ────────────────────────────────────────────────────
            ("; always runs",       "false ; echo always",          0, "always"),

            # ── | pipe ────────────────────────────────────────────────────────
            ("pipe head",           f"cat {_tf} | head -n 2",        0, "line1"),
            ("pipe grep",           f"cat {_tf} | grep hello",      0, "hello"),
            ("pipe wc",             f"cat {_tf} | wc -l",           0, ""),
            ("pipe grep no-match",  f"cat {_tf} | grep zzzmissing", 1, ""),

            # ── pipe right-only output (not prepended) ────────────────────────
            # grep output should be just the matched line, not the whole cat output
            ("pipe output is right-only",
                f"cat {_tf} | grep hello",    0, "line3"),

            # ── write via pipe ─────────────────────────────────────────────────
            ("pipe into write",
                f"echo content | write {_tmp}/out.txt",   0, ""),

            # ── memory store via pipe ──────────────────────────────────────────
            ("pipe into memory store",
                "echo piped memory entry | memory store",  0, ""),

            # ── heredoc ───────────────────────────────────────────────────────
            ("heredoc write",
                f"write {_tmp}/hd.txt <<'EOF'\nhello heredoc\nEOF",  0, ""),

            # ── skills pipe ───────────────────────────────────────────────────
            ("skills list pipe grep",  "skills list | grep weather",  0, "weather"),

            # ── quoted operators not split ─────────────────────────────────────
            ("quoted semicolon",
                "echo 'a;b'",    0, "a;b"),
            ("quoted pipe",
                'echo "a|b"',    0, "a|b"),
        ]

        for label, command, want_exit, want_contains in tests:
            ok_ = _check(label, command, want_exit, want_contains)
            if ok_:
                passed += 1
            else:
                failed += 1

        # Verify write-via-pipe actually wrote the file
        _out = Path(_tmp) / "out.txt"
        if _out.exists() and "content" in _out.read_text():
            print(f"  {_PASS}                    pipe→write file contents verified")
            passed += 1
        else:
            print(f"  {_FAIL}                    pipe→write file contents NOT verified")
            failed += 1

    print(f"\n{'─' * 40}")
    print(f"  {passed} passed  {failed} failed  ({passed + failed} total)")
    sys.exit(0 if failed == 0 else 1)