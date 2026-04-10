from __future__ import annotations

from src.multi_agent.spawn import _classify_spawn_outcome, _format_spawn_result


def test_spawn_result_completed_format():
    status, next_action, summary = _classify_spawn_outcome(
        role="obsidian",
        stop_reason="end_turn",
        final_response="Tags added successfully.",
    )
    text = _format_spawn_result(
        agent_id="obsidian-1234",
        role="obsidian",
        status=status,
        stop_reason="end_turn",
        next_action=next_action,
        tools_used=3,
        total_tokens=500,
        subtree_tool_calls=3,
        subtree_total_tokens=500,
        subtree_cost_usd=0.0,
        elapsed_ms=1200,
        summary=summary,
        result="Tags added successfully.",
    )

    assert status == "completed"
    assert next_action == "return_to_user"
    assert "[spawn_result]" in text
    assert "status: completed" in text
    assert "next_action: return_to_user" in text
    assert "result:" in text


def test_spawn_result_failed_tool_ceiling_suggests_recovery():
    status, next_action, summary = _classify_spawn_outcome(
        role="researcher",
        stop_reason="tool_ceiling",
        final_response="",
    )
    text = _format_spawn_result(
        agent_id="researcher-1234",
        role="researcher",
        status=status,
        stop_reason="tool_ceiling",
        next_action=next_action,
        tools_used=3,
        total_tokens=300,
        subtree_tool_calls=3,
        subtree_total_tokens=300,
        subtree_cost_usd=0.0,
        elapsed_ms=900,
        summary=summary,
        result="",
    )

    assert status == "failed"
    assert next_action == "respawn_better_specialist"
    assert "stop_reason: tool_ceiling" in text
    assert "summary:" in text
