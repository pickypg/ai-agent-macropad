"""Codex CLI sends the same hook_event_name vocabulary as Claude Code
(confirmed against Codex's own hooks reference) with the same core
payload fields (hook_event_name, session_id, cwd, tool_name), so
daemon.py's mapping logic is intentionally shared rather than forked
per agent — see hook_to_state()'s docstring. These tests exercise that
shared path with realistic Codex-shaped payloads (Codex's own tool
names, its "agent" tag from codex/hook.sh) rather than re-testing
mapping rules already covered by tests/test_hook_mapping.py and
tests/test_handle_hook_event.py.

Field names and values (including "Bash" as Codex's own shell tool
name — same as Claude Code's, not the "shell" name it's sometimes
called elsewhere) come from a real `codex exec` run through
codex/hook.sh into a live daemon.py during development, not guessed
from documentation alone.
"""
import daemon


def test_codex_session_lifecycle_maps_like_claude_code(recording_daemon):
    d, sent = recording_daemon

    d.handle_hook_event({
        "hook_event_name": "SessionStart",
        "session_id": "c1",
        "cwd": "/x/my-project",
        "agent": "codex",
        "source": "startup",
    })
    assert d.slots.slot_for("c1") == 0
    assert d.session_agents["c1"] == "codex"
    assert sent[-1] == {"t": "slot", "i": 0, "state": "idle", "label": "my-project"}

    d.handle_hook_event({
        "hook_event_name": "PreToolUse", "session_id": "c1",
        "tool_name": "Bash", "agent": "codex",
    })
    assert sent[-1] == {"t": "slot", "i": 0, "state": "tool_running", "label": "Bash"}

    d.handle_hook_event({
        "hook_event_name": "PermissionRequest", "session_id": "c1",
        "tool_name": "Bash", "agent": "codex",
    })
    assert sent[-1]["state"] == "question"

    d.handle_hook_event({
        "hook_event_name": "PostToolUse", "session_id": "c1",
        "tool_name": "Bash", "agent": "codex",
    })
    assert sent[-1]["state"] == "working"

    d.handle_hook_event({
        "hook_event_name": "Stop", "session_id": "c1", "agent": "codex",
    })
    assert sent[-1]["state"] == "done"

    d.handle_hook_event({
        "hook_event_name": "SessionEnd", "session_id": "c1", "agent": "codex",
    })
    assert d.slots.slot_for("c1") is None
    assert "c1" not in d.session_agents
    assert sent[-1] == {"t": "clear", "i": 0}


def test_agent_field_defaults_to_claude_code_when_absent(recording_daemon):
    """Payloads that predate the "agent" field (or a hand-rolled test
    payload) shouldn't error out — bookkeeping just attributes them to
    claude-code, the only agent that existed before this field did.
    """
    d, sent = recording_daemon
    d.handle_hook_event({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/p"})
    assert d.session_agents["s1"] == "claude-code"


def test_agent_field_backfilled_on_lazy_allocation(recording_daemon):
    """A Codex event arriving before this daemon ever saw that
    session's SessionStart (daemon restart mid-turn, same case already
    covered for project/pane backfill) should still attribute the
    right agent, not silently default to claude-code.
    """
    d, sent = recording_daemon
    d.handle_hook_event({
        "hook_event_name": "PreToolUse", "session_id": "c1",
        "tool_name": "Bash", "cwd": "/x/proj", "agent": "codex",
    })
    assert d.session_agents["c1"] == "codex"


def test_codex_has_no_posttoolusefailure_or_notification_equivalent():
    """Documents the known gap (see hook_to_state()'s docstring):
    Codex never sends these two Claude-Code-only event names, so they
    only exist here as inputs a future regression could theoretically
    introduce, not as something Codex payloads actually contain.
    """
    assert daemon.hook_to_state("PostToolUseFailure") == "error"
    assert daemon.hook_to_state("Notification", notification_type="idle_prompt") == "waiting"
