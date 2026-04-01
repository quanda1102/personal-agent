from src.heartbeat.llm_plan import extract_queue_inserts


def test_extract_queue_inserts_finds_block():
    text = """# Plan

```json
{"queue_inserts": [{"action": "hello", "needs_user": true, "priority": "routine"}]}
```
"""
    rows = extract_queue_inserts(text)
    assert len(rows) == 1
    assert rows[0]["action"] == "hello"


def test_extract_queue_inserts_empty():
    assert extract_queue_inserts("no json here") == []
