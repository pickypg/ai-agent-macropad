"""Grok Build's own hook payloads look nothing like Claude Code's or
Codex's on the wire (camelCase fields, snake_case hookEventName
values) — grok/hook.sh translates them before forwarding, so these
tests exercise daemon.py with the *post-translation* PascalCase shape
hook_to_state() expects, same as tests/test_codex_hook_mapping.py does
for Codex, rather than re-testing mapping rules already covered by
tests/test_hook_mapping.py and tests/test_handle_hook_event.py.

Event names, field names, and values (including "run_terminal_command"
as Grok Build's own shell tool name, and permission_prompt/idle_prompt
as its two live Notification subtypes) come from a real `grok -p ...`
run through grok/hook.sh into a live daemon.py during development, not
guessed from Grok's bundled docs alone — see grok/hook.sh's own
comments for the couple of places the docs turned out to disagree with
the real wire values (hookEventName's value casing, and how rarely
PostToolUseFailure actually fires).
"""
import daemon


def test_grok_build_session_lifecycle_maps_like_claude_code(recording_daemon):
    d, sent = recording_daemon

    d.handle_hook_event({
        "hook_event_name": "SessionStart",
        "session_id": "g1",
        "cwd": "/x/my-project",
        "agent": "grok-build",
    })
    assert d.slots.slot_for("g1") == 0
    assert d.session_agents["g1"] == "grok-build"
    assert sent[-1] == {"t": "slot", "i": 0, "state": "idle", "label": "my-project"}

    d.handle_hook_event({
        "hook_event_name": "PreToolUse", "session_id": "g1",
        "tool_name": "run_terminal_command", "agent": "grok-build",
    })
    assert sent[-1] == {"t": "slot", "i": 0, "state": "tool_running", "label": "run_terminal_command"}

    # Grok Build has no PermissionRequest-style event — a permission UI
    # actually waiting on you surfaces only via Notification, confirmed
    # live and reliable (unlike Claude Code's version of this subtype).
    d.handle_hook_event({
        "hook_event_name": "Notification", "session_id": "g1",
        "notification_type": "permission_prompt", "agent": "grok-build",
    })
    assert sent[-1]["state"] == "question"

    d.handle_hook_event({
        "hook_event_name": "PostToolUse", "session_id": "g1",
        "tool_name": "run_terminal_command", "agent": "grok-build",
    })
    assert sent[-1]["state"] == "working"

    d.handle_hook_event({
        "hook_event_name": "Stop", "session_id": "g1", "agent": "grok-build",
    })
    assert sent[-1]["state"] == "done"

    d.handle_hook_event({
        "hook_event_name": "SessionEnd", "session_id": "g1", "agent": "grok-build",
    })
    assert d.slots.slot_for("g1") is None
    assert "g1" not in d.session_agents
    assert sent[-1] == {"t": "clear", "i": 0}


def test_agent_field_backfilled_on_lazy_allocation(recording_daemon):
    """A Grok Build event arriving before this daemon ever saw that
    session's SessionStart (daemon restart mid-turn, same case already
    covered for Codex) should still attribute the right agent, not
    silently default to claude-code.
    """
    d, sent = recording_daemon
    d.handle_hook_event({
        "hook_event_name": "PreToolUse", "session_id": "g1",
        "tool_name": "run_terminal_command", "cwd": "/x/proj", "agent": "grok-build",
    })
    assert d.session_agents["g1"] == "grok-build"


def test_stop_failure_and_stop_cancelled_are_grok_build_only():
    """StopFailure and StopCancelled don't exist for Claude Code or
    Codex (both only ever send Stop) — Grok Build's own hooks reference
    documents them, and a live run confirmed StopFailure fires on an
    API-error turn end and StopCancelled fires on an interrupted one.
    See hook_to_state()'s docstring for why they map to "error" and
    "done" respectively.
    """
    assert daemon.hook_to_state("StopFailure") == "error"
    assert daemon.hook_to_state("StopCancelled") == "done"


def test_post_tool_use_failure_rarely_fires_for_grok_build():
    """Documents a real gap found during development, contradicting
    Grok Build's own docs: both a nonzero shell exit code and a
    nonexistent-file read came back live as a plain PostToolUse, not
    PostToolUseFailure — same practical gap as Codex, despite Grok
    Build's docs listing PostToolUseFailure as a distinct event. Still
    mapped to "error" here (translated by grok/hook.sh, same as for
    Claude Code) in case a genuine infra-level failure does use it.
    """
    assert daemon.hook_to_state("PostToolUseFailure") == "error"


def test_permission_denied_is_not_a_question():
    """permission_denied fires *after* Grok Build's permission system
    has already auto-denied a call by rule — confirmed live — with
    nothing left pending for you to answer, unlike Claude
    Code/Codex's PermissionRequest (a live "waiting on you" prompt).
    grok/hook.sh translates it to PermissionDenied, which deliberately
    maps to no state change here.
    """
    assert daemon.hook_to_state("PermissionDenied") is None
