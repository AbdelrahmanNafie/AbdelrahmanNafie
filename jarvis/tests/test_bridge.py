from jarvis.bridge import describe_claude_failure


def test_failure_message_keeps_the_reason_and_drops_usage_noise():
    result = {"is_error": True, "subtype": "error_during_execution",
              "result": "MCP server 'jarvis' failed to start", "usage": {"input_tokens": 4272},
              "permission_denials": [{"tool_name": "mcp__jarvis__open_app"}]}
    msg = describe_claude_failure(1, result, stderr="", stdout="{...}")
    assert "error_during_execution" in msg
    assert "failed to start" in msg
    assert "mcp__jarvis__open_app" in msg
    assert "input_tokens" not in msg


def test_failure_without_json_shows_raw_output():
    msg = describe_claude_failure(1, {}, stderr="", stdout="Invalid API key")
    assert "Invalid API key" in msg
