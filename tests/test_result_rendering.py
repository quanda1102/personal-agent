from __future__ import annotations

from src.cli_handler.result import Result


def test_render_attaches_stderr_and_footer():
    result = Result(stdout="hello", stderr="warning", exit=1, elapsed_ms=12.4)
    rendered = result.render()

    assert "hello" in rendered
    assert "[stderr] warning" in rendered
    assert rendered.endswith("[exit:1 | 12ms]")


def test_render_binary_image_redirects_to_see():
    binary_like = "\x89PNG\r\n\x1a\n" + ("\x01" * 12) + ("A" * 40)
    result = Result(stdout=binary_like, exit=0, elapsed_ms=5)
    rendered = result.render()

    assert "use: see <filename>" in rendered
    assert rendered.endswith("[exit:0 | 5ms]")


def test_render_overflow_spills_and_guides_navigation():
    text = "\n".join(f"line {i}" for i in range(250))
    result = Result(stdout=text, exit=0, elapsed_ms=7)
    rendered = result.render()

    assert "--- output truncated" in rendered
    assert "Full output saved to:" in rendered
    assert "cat " in rendered
    assert rendered.endswith("[exit:0 | 7ms]")
