import asyncio
import contextlib

import daemon


def test_session_start_allocates_slot_and_sends_idle(recording_daemon):
    d, sent = recording_daemon
    d.handle_hook_event(
        {"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/x/my-project"}
    )

    assert d.slots.slot_for("s1") == 0
    assert sent == [{"t": "slot", "i": 0, "state": "idle", "label": "my-project"}]


def test_session_start_without_cwd_falls_back_to_session_id_prefix(recording_daemon):
    d, sent = recording_daemon
    d.handle_hook_event({"hook_event_name": "SessionStart", "session_id": "abcdefgh12"})
    assert sent[0]["label"] == "abcdefgh"


def test_pretooluse_labels_slot_with_tool_name(recording_daemon):
    d, sent = recording_daemon
    d.handle_hook_event({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/p"})
    d.handle_hook_event(
        {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Read"}
    )
    assert sent[-1] == {"t": "slot", "i": 0, "state": "tool_running", "label": "Read"}


def test_pretooluse_ask_user_question_maps_to_question(recording_daemon):
    d, sent = recording_daemon
    d.handle_hook_event({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/p"})
    d.handle_hook_event(
        {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "AskUserQuestion"}
    )
    assert sent[-1]["state"] == "question"


def test_posttoolusefailure_maps_to_error(recording_daemon):
    d, sent = recording_daemon
    d.handle_hook_event({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/p"})
    d.handle_hook_event({"hook_event_name": "PostToolUseFailure", "session_id": "s1"})
    assert sent[-1] == {"t": "slot", "i": 0, "state": "error"}


def test_session_end_frees_slot_and_sends_clear(recording_daemon):
    d, sent = recording_daemon
    d.handle_hook_event({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/p"})
    d.handle_hook_event({"hook_event_name": "SessionEnd", "session_id": "s1"})

    assert d.slots.slot_for("s1") is None
    assert sent[-1] == {"t": "clear", "i": 0}
    # session bookkeeping is cleared too, not just the slot
    assert "s1" not in d.session_projects
    assert "s1" not in d.pending_calls


def test_event_missing_session_id_is_dropped(recording_daemon):
    d, sent = recording_daemon
    d.handle_hook_event({"hook_event_name": "SessionStart"})
    assert sent == []


def test_event_with_no_state_mapping_sends_nothing(recording_daemon):
    d, sent = recording_daemon
    d.handle_hook_event({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/p"})
    sent.clear()
    d.handle_hook_event({"hook_event_name": "SubagentStop", "session_id": "s1"})
    assert sent == []


def test_event_before_session_start_lazily_allocates(recording_daemon):
    """Covers the daemon-restarted-mid-session case: an event for a
    session we never saw SessionStart for should still get a slot and
    a project label, not be silently dropped.
    """
    d, sent = recording_daemon
    d.handle_hook_event(
        {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash", "cwd": "/x/proj"}
    )

    assert d.slots.slot_for("s1") == 0
    assert d.session_projects["s1"] == "proj"
    assert sent[-1] == {"t": "slot", "i": 0, "state": "tool_running", "label": "Bash"}


def test_event_dropped_when_no_free_slot_for_new_session(recording_daemon):
    d, sent = recording_daemon
    d.slots = daemon.SlotManager(1)
    d.handle_hook_event({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/p"})
    sent.clear()

    d.handle_hook_event({"hook_event_name": "SessionStart", "session_id": "s2", "cwd": "/p2"})
    assert sent == []
    assert d.slots.slot_for("s2") is None


def test_pretooluse_then_posttoolure_clears_pending_call(recording_daemon):
    d, sent = recording_daemon
    d.handle_hook_event({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/p"})
    d.handle_hook_event({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash"})
    assert "s1" in d.pending_calls

    d.handle_hook_event({"hook_event_name": "PostToolUse", "session_id": "s1"})
    assert "s1" not in d.pending_calls


def test_stalled_call_escalates_to_tool_stalled(recording_daemon, monkeypatch):
    d, sent = recording_daemon
    d.handle_hook_event({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/p"})
    d.handle_hook_event({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash"})

    # Backdate the pending call past STALL_THRESHOLD_SECONDS instead of
    # sleeping in real time for the full threshold.
    d.pending_calls["s1"]["since"] -= daemon.STALL_THRESHOLD_SECONDS + 1

    async def run_briefly():
        task = asyncio.ensure_future(d.watch_stalled_calls())
        await asyncio.sleep(1.1)  # watch_stalled_calls polls every 1s
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run_briefly())

    assert sent[-1] == {"t": "slot", "i": 0, "state": "tool_stalled"}
    assert d.pending_calls["s1"]["escalated"] is True


def test_stall_watcher_disabled_when_threshold_is_none(recording_daemon, monkeypatch):
    monkeypatch.setattr(daemon, "STALL_THRESHOLD_SECONDS", None)
    d, sent = recording_daemon

    # watch_stalled_calls() should return immediately rather than loop
    asyncio.run(asyncio.wait_for(d.watch_stalled_calls(), timeout=1))
