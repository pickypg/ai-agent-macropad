"""Cursor's own hook payloads are camelCase-valued (hook_event_name:
"sessionStart", "preToolUse", ...) — cursor/hook.sh translates them to
this repo's PascalCase wire vocabulary before forwarding, same idea as
grok/hook.sh's translation. These tests exercise daemon.py with the
*post-translation* shape hook_to_state() expects, same as
tests/test_codex_hook_mapping.py and tests/test_grok_hook_mapping.py do
for their agents.

Event names, field shapes, and gaps (postToolUseFailure firing
reliably, afterFileEdit's real edits:[{old_string,new_string}] shape,
subagentStart/subagentStop never firing — a subagent call surfaces as
a generic Task tool call instead) come from real `agent` runs
(2026-08-23, both interactive sessions and `agent -p`), not guessed
from documentation alone — see cursor/hook.sh's own comments for the
two narrow gaps that still are (MCP events, preCompact).
"""
import daemon


def test_cursor_session_lifecycle_maps_like_claude_code(recording_daemon):
    d, sent = recording_daemon

    d.handle_hook_event({
        "hook_event_name": "SessionStart",
        "session_id": "u1",
        "cwd": "/x/my-project",
        "agent": "cursor",
    })
    assert d.slots.slot_for("u1") == 0
    assert d.session_agents["u1"] == "cursor"
    assert sent[-1] == {"t": "slot", "i": 0, "state": "idle", "label": "my-project"}

    d.handle_hook_event({
        "hook_event_name": "PreToolUse", "session_id": "u1",
        "tool_name": "Shell", "agent": "cursor",
    })
    assert sent[-1] == {"t": "slot", "i": 0, "state": "tool_running", "label": "Shell"}

    d.handle_hook_event({
        "hook_event_name": "PostToolUse", "session_id": "u1",
        "tool_name": "Shell", "agent": "cursor",
    })
    assert sent[-1]["state"] == "working"

    d.handle_hook_event({
        "hook_event_name": "Stop", "session_id": "u1", "agent": "cursor",
    })
    assert sent[-1]["state"] == "done"

    d.handle_hook_event({
        "hook_event_name": "SessionEnd", "session_id": "u1", "agent": "cursor",
    })
    assert d.slots.slot_for("u1") is None
    assert "u1" not in d.session_agents
    assert sent[-1] == {"t": "clear", "i": 0}


def test_agent_field_backfilled_on_lazy_allocation(recording_daemon):
    """A Cursor event arriving before this daemon ever saw that
    session's SessionStart (daemon restart mid-turn, same case already
    covered for Codex/Grok Build) should still attribute the right
    agent, not silently default to claude-code.
    """
    d, sent = recording_daemon
    d.handle_hook_event({
        "hook_event_name": "PreToolUse", "session_id": "u1",
        "tool_name": "Shell", "cwd": "/x/proj", "agent": "cursor",
    })
    assert d.session_agents["u1"] == "cursor"


def test_cursor_has_no_question_or_waiting_equivalent():
    """Documents the known gap (see hook_to_state()'s docstring) —
    deliberately re-tested, not just undocumented. Cursor's
    preToolUse/beforeShellExecution hooks CAN return
    {"permission": "ask"} to force Cursor's approval UI, but that's a
    hook-INITIATED control channel: live testing (2026-08-23, a second
    test hook registered alongside cursor/hook.sh that always asked)
    confirmed a denied command reaches cursor/hook.sh's own events
    completely unmarked (PostToolUse's tool_output even reports
    {"output":"","exitCode":0}, indistinguishable from a real success).
    A hook has no visibility into another hook's (or Cursor's own
    native approval logic's) ask/deny decision, so nothing
    cursor/hook.sh forwards ever maps to "question" or "waiting" today
    — a bigger gap than even Codex's single-event PermissionRequest
    coverage.
    """
    assert daemon.hook_to_state("PermissionRequest") == "question"  # shared logic — Cursor just never sends it
    assert daemon.hook_to_state("Notification", notification_type="agent_needs_input") == "question"  # ditto


def test_cursor_pending_permission_ask_still_reaches_stall_backstop(recording_daemon):
    """It isn't silent in practice, though — confirmed on real hardware
    (2026-08-23, an actual `sleep` command gated behind a project hook
    asking for approval): a pending Cursor permission prompt leaves a
    PreToolUse with no matching PostToolUse, which is exactly what
    STALL_THRESHOLD_SECONDS's stall-detection backstop (see
    tests/test_handle_hook_event.py for the shared, agent-agnostic
    escalation mechanism itself) already exists to catch — no
    Cursor-specific code needed. This just confirms a Cursor-tagged
    PreToolUse populates pending_calls the same as any other agent's,
    so that backstop actually applies to it.
    """
    d, sent = recording_daemon
    d.handle_hook_event({
        "hook_event_name": "SessionStart", "session_id": "u4",
        "cwd": "/x/proj", "agent": "cursor",
    })
    d.handle_hook_event({
        "hook_event_name": "PreToolUse", "session_id": "u4",
        "tool_name": "Shell", "agent": "cursor",
    })
    assert "u4" in d.pending_calls
    assert d.pending_calls["u4"]["tool_name"] == "Shell"


def test_shell_and_mcp_event_pairs_fold_into_generic_pre_post_tool_use():
    """cursor/hook.sh maps beforeShellExecution/afterShellExecution and
    beforeMCPExecution/afterMCPExecution onto the same PreToolUse/
    PostToolUse pair as Cursor's generic tool-call events (see its own
    comments for why) — this just documents that hook_to_state() itself
    needs no Cursor-specific branch for that folding, since by the time
    an event reaches daemon.py it's indistinguishable from a generic one.
    """
    assert daemon.hook_to_state("PreToolUse", tool_name="Shell") == "tool_running"
    assert daemon.hook_to_state("PostToolUse", tool_name="Shell") == "working"


def test_post_tool_use_failure_maps_to_error(recording_daemon):
    """Confirmed live (2026-08-23): unlike Claude Code, Codex, and Grok
    Build — where the equivalent event rarely fires in practice — a
    genuine Cursor tool failure (a real Grep call against a nonexistent
    path) reliably produced postToolUseFailure, translated by
    cursor/hook.sh to PostToolUseFailure. Same shared mapping as the
    other three agents, just actually exercised for this one.
    """
    d, sent = recording_daemon
    d.handle_hook_event({
        "hook_event_name": "SessionStart", "session_id": "u2",
        "cwd": "/x/proj", "agent": "cursor",
    })
    d.handle_hook_event({
        "hook_event_name": "PostToolUseFailure", "session_id": "u2",
        "tool_name": "Grep", "agent": "cursor",
    })
    assert sent[-1]["state"] == "error"


def test_after_file_edit_folds_into_post_tool_use_working(recording_daemon):
    """Confirmed live: afterFileEdit's real payload shape is
    file_path + edits:[{old_string,new_string}], no tool_name of its
    own — cursor/hook.sh's TOOL_NAME_FALLBACK fills in "Edit" so the
    pad's PreToolUse label isn't blank, and the translated event still
    folds into the same PostToolUse -> "working" mapping as any other
    completed tool call.
    """
    d, sent = recording_daemon
    d.handle_hook_event({
        "hook_event_name": "SessionStart", "session_id": "u3",
        "cwd": "/x/proj", "agent": "cursor",
    })
    d.handle_hook_event({
        "hook_event_name": "PostToolUse", "session_id": "u3",
        "tool_name": "Edit", "agent": "cursor",
    })
    assert sent[-1]["state"] == "working"


def test_subagent_delegation_has_no_dedicated_events():
    """Confirmed live: subagentStart/subagentStop never fired for a
    real subagent delegation (`agent -p ... "use a subagent to..."`) —
    it surfaced only as an ordinary PreToolUse/PostToolUse pair for a
    "Task" tool (tool_input.subagent_type). hook_to_state() doesn't
    define SubagentStart/SubagentStop at all (same as Claude Code's own
    SubagentStop, which maps to no direct display change), so this just
    documents that a Cursor subagent call is indistinguishable from any
    other tool call on the pad — not a gap needing a fix.
    """
    assert daemon.hook_to_state("SubagentStart") is None
    assert daemon.hook_to_state("SubagentStop") is None
    assert daemon.hook_to_state("PreToolUse", tool_name="Task") == "tool_running"


def test_manual_compact_has_no_dedicated_event():
    """Confirmed live (2026-08-23): a plain `/compact` prompt visibly
    condensed a real session's context into a structured summary (it
    correctly referenced prior turns), but no PreCompact event ever
    reached cursor/hook.sh — same "deliberately tested, confirmed
    absent" category as subagentStart/subagentStop above, not just an
    untested assumption. Auto-triggered compaction (hitting the
    context-window threshold organically, rather than a manual prompt)
    remains untested — impractical to force deliberately — but the
    deliberate-trigger path is now ruled out too. hook_to_state()
    already maps PreCompact to no state change (matching Grok Build's
    own wired-but-unused PreCompact/PostCompact), so no daemon.py
    change is needed even if a future Cursor version starts sending it.
    """
    assert daemon.hook_to_state("PreCompact") is None
