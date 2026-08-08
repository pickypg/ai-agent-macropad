import daemon


def test_session_start_maps_to_idle():
    assert daemon.hook_to_state("SessionStart") == "idle"


def test_prompt_and_posttooluse_map_to_working():
    assert daemon.hook_to_state("UserPromptSubmit") == "working"
    assert daemon.hook_to_state("PostToolUse", tool_name="Read") == "working"


def test_pretooluse_maps_to_tool_running():
    assert daemon.hook_to_state("PreToolUse", tool_name="Read") == "tool_running"


def test_attention_tools_escalate_pretooluse_to_question():
    assert daemon.hook_to_state("PreToolUse", tool_name="AskUserQuestion") == "question"
    assert daemon.hook_to_state("PreToolUse", tool_name="ExitPlanMode") == "question"


def test_permission_request_maps_to_question():
    assert daemon.hook_to_state("PermissionRequest") == "question"


def test_notification_subtypes():
    assert daemon.hook_to_state("Notification", notification_type="agent_needs_input") == "question"
    assert daemon.hook_to_state("Notification", notification_type="idle_prompt") == "waiting"
    assert daemon.hook_to_state("Notification", notification_type="auth_success") is None
    assert daemon.hook_to_state("Notification") is None


def test_failure_and_stop_and_unknown_event():
    assert daemon.hook_to_state("PostToolUseFailure") == "error"
    assert daemon.hook_to_state("Stop") == "done"
    assert daemon.hook_to_state("SubagentStop") is None
    assert daemon.hook_to_state("SomethingUnrecognized") is None
