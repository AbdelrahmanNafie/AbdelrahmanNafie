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


def test_usage_limit_gets_a_plain_explanation():
    msg = describe_claude_failure(1, {"result": "You've hit your weekly limit · resets Oct 11"}, "", "")
    assert "Not a Jarvis bug" in msg


def test_brain_uses_configured_model(settings, monkeypatch):
    import jarvis.bridge as bridge
    monkeypatch.setattr(bridge.shutil, "which", lambda _: "claude")
    cmd = bridge.build_claude_command(settings, continue_session=False)
    assert cmd[cmd.index("--model") + 1] == "sonnet"
