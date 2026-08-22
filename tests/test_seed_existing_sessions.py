import subprocess
import types

import daemon


# --- discover_running_sessions -------------------------------------------


def fake_run(returncode=0, stdout="", raise_exc=None):
    def _run(*args, **kwargs):
        if raise_exc:
            raise raise_exc
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="boom")
    return _run


def test_discover_running_sessions_parses_json_array(monkeypatch):
    stdout = '[{"pid": 1, "cwd": "/x/proj", "sessionId": "s1", "kind": "interactive"}]'
    monkeypatch.setattr(daemon.subprocess, "run", fake_run(stdout=stdout))
    assert daemon.discover_running_sessions() == [
        {"pid": 1, "cwd": "/x/proj", "sessionId": "s1", "kind": "interactive"}
    ]


def test_discover_running_sessions_missing_claude_binary(monkeypatch):
    monkeypatch.setattr(
        daemon.subprocess, "run", fake_run(raise_exc=FileNotFoundError())
    )
    assert daemon.discover_running_sessions() == []


def test_discover_running_sessions_timeout(monkeypatch):
    monkeypatch.setattr(
        daemon.subprocess, "run",
        fake_run(raise_exc=subprocess.TimeoutExpired(cmd="claude", timeout=5)),
    )
    assert daemon.discover_running_sessions() == []


def test_discover_running_sessions_nonzero_exit(monkeypatch):
    monkeypatch.setattr(daemon.subprocess, "run", fake_run(returncode=1, stdout=""))
    assert daemon.discover_running_sessions() == []


def test_discover_running_sessions_bad_json(monkeypatch):
    monkeypatch.setattr(daemon.subprocess, "run", fake_run(stdout="not json"))
    assert daemon.discover_running_sessions() == []


def test_discover_running_sessions_unexpected_shape(monkeypatch):
    monkeypatch.setattr(daemon.subprocess, "run", fake_run(stdout='{"not": "a list"}'))
    assert daemon.discover_running_sessions() == []


# --- _controlling_tty ------------------------------------------------------


def test_controlling_tty_returns_dev_path(monkeypatch):
    monkeypatch.setattr(
        daemon.subprocess, "run",
        fake_run(stdout="ttys003\n"),
    )
    assert daemon._controlling_tty(123) == "/dev/ttys003"


def test_controlling_tty_none_for_no_controlling_terminal(monkeypatch):
    monkeypatch.setattr(daemon.subprocess, "run", fake_run(stdout="??\n"))
    assert daemon._controlling_tty(123) is None


def test_controlling_tty_none_for_dead_pid(monkeypatch):
    monkeypatch.setattr(daemon.subprocess, "run", fake_run(stdout=""))
    assert daemon._controlling_tty(123) is None


# --- _tmux_pane_for_tty ------------------------------------------------------


def test_tmux_pane_for_tty_matches(monkeypatch):
    monkeypatch.setattr(
        daemon.subprocess, "run",
        fake_run(stdout="/dev/ttys001 %3\n/dev/ttys003 %7\n"),
    )
    assert daemon._tmux_pane_for_tty("/dev/ttys003") == "%7"


def test_tmux_pane_for_tty_no_match(monkeypatch):
    monkeypatch.setattr(daemon.subprocess, "run", fake_run(stdout="/dev/ttys001 %3\n"))
    assert daemon._tmux_pane_for_tty("/dev/ttys099") is None


def test_tmux_pane_for_tty_no_tmux_on_path(monkeypatch):
    monkeypatch.setattr(
        daemon.subprocess, "run", fake_run(raise_exc=FileNotFoundError())
    )
    assert daemon._tmux_pane_for_tty("/dev/ttys001") is None


# --- Daemon.seed_existing_sessions -----------------------------------------


def test_seed_existing_sessions_allocates_slots_and_sends_idle(recording_daemon, monkeypatch):
    d, sent = recording_daemon
    monkeypatch.setattr(
        daemon, "discover_running_sessions",
        lambda: [
            {"pid": 1, "cwd": "/x/proj-a", "sessionId": "sa", "kind": "interactive"},
            {"pid": 2, "cwd": "/x/proj-b", "sessionId": "sb", "kind": "interactive"},
        ],
    )
    monkeypatch.setattr(daemon, "_controlling_tty", lambda pid: None)

    d.seed_existing_sessions()

    assert d.slots.slot_for("sa") == 0
    assert d.slots.slot_for("sb") == 1
    assert d.session_projects == {"sa": "proj-a", "sb": "proj-b"}
    # This seeding path can only ever see Claude Code sessions — see
    # discover_running_sessions()'s docstring
    assert d.session_agents == {"sa": "claude-code", "sb": "claude-code"}
    # The two real sessions get idle; every other slot this daemon
    # process didn't just claim gets explicitly cleared to off, so a
    # slot left glowing by a dead session/previous daemon run doesn't
    # linger forever.
    assert sent[:2] == [
        {"t": "slot", "i": 0, "state": "idle", "label": "proj-a"},
        {"t": "slot", "i": 1, "state": "idle", "label": "proj-b"},
    ]
    assert sent[2:] == [{"t": "clear", "i": i} for i in range(2, d.slots.num_slots)]


def test_seed_existing_sessions_backfills_tty_and_tmux_pane(recording_daemon, monkeypatch):
    d, sent = recording_daemon
    monkeypatch.setattr(
        daemon, "discover_running_sessions",
        lambda: [{"pid": 42, "cwd": "/x/proj", "sessionId": "s1", "kind": "interactive"}],
    )
    monkeypatch.setattr(daemon, "_controlling_tty", lambda pid: "/dev/ttys003")
    monkeypatch.setattr(daemon, "_tmux_pane_for_tty", lambda tty: "%7")

    d.seed_existing_sessions()

    assert d.session_ttys["s1"] == "/dev/ttys003"
    assert d.session_panes["s1"] == "%7"


def test_seed_existing_sessions_no_pad_target_when_tty_unknown(recording_daemon, monkeypatch):
    """VS Code-hosted sessions have no controlling tty (see
    _controlling_tty's docstring) — seeding shouldn't invent a tmux
    lookup for them.
    """
    d, sent = recording_daemon
    monkeypatch.setattr(
        daemon, "discover_running_sessions",
        lambda: [{"pid": 1, "cwd": "/x/proj", "sessionId": "s1", "kind": "interactive"}],
    )
    monkeypatch.setattr(daemon, "_controlling_tty", lambda pid: None)
    monkeypatch.setattr(
        daemon, "_tmux_pane_for_tty",
        lambda tty: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    d.seed_existing_sessions()

    assert "s1" not in d.session_ttys
    assert "s1" not in d.session_panes


def test_seed_existing_sessions_skips_entries_without_session_id(recording_daemon, monkeypatch):
    d, sent = recording_daemon
    monkeypatch.setattr(
        daemon, "discover_running_sessions",
        lambda: [{"pid": 1, "cwd": "/x/proj"}],
    )
    d.seed_existing_sessions()
    # No real session got allocated, so every slot is "other" — all of
    # them get cleared to off.
    assert sent == [{"t": "clear", "i": i} for i in range(d.slots.num_slots)]


def test_seed_existing_sessions_stops_at_slot_capacity(recording_daemon, monkeypatch):
    d, sent = recording_daemon
    d.slots = daemon.SlotManager(1)
    monkeypatch.setattr(
        daemon, "discover_running_sessions",
        lambda: [
            {"pid": 1, "cwd": "/x/a", "sessionId": "sa"},
            {"pid": 2, "cwd": "/x/b", "sessionId": "sb"},
        ],
    )
    monkeypatch.setattr(daemon, "_controlling_tty", lambda pid: None)

    d.seed_existing_sessions()

    assert d.slots.slot_for("sa") == 0
    assert d.slots.slot_for("sb") is None
    assert len(sent) == 1


def test_seed_existing_sessions_clears_unclaimed_slots_only(recording_daemon, monkeypatch):
    """A slot with no matching entry in `claude agents --json` gets an
    explicit "off" (clear) — covers a session that died without ever
    sending SessionEnd (crash, kill -9, a previous daemon run that never
    shut down cleanly), which would otherwise leave that slot showing
    whatever it last displayed forever, since nothing else ever
    revisits an unallocated slot.
    """
    d, sent = recording_daemon
    d.slots = daemon.SlotManager(4)
    monkeypatch.setattr(
        daemon, "discover_running_sessions",
        lambda: [{"pid": 1, "cwd": "/x/proj", "sessionId": "s1", "kind": "interactive"}],
    )
    monkeypatch.setattr(daemon, "_controlling_tty", lambda pid: None)

    d.seed_existing_sessions()

    assert sent == [
        {"t": "slot", "i": 0, "state": "idle", "label": "proj"},
        {"t": "clear", "i": 1},
        {"t": "clear", "i": 2},
        {"t": "clear", "i": 3},
    ]


def test_seeded_session_pane_backfilled_by_later_hook_event(recording_daemon, monkeypatch):
    """A seeded session has no tmux_pane (claude agents --json carries no
    tmux info) until its first real hook event supplies one — covers the
    handle_hook_event fix that no longer gates this backfill on lazy
    allocation.
    """
    d, sent = recording_daemon
    monkeypatch.setattr(
        daemon, "discover_running_sessions",
        lambda: [{"pid": 1, "cwd": "/x/proj", "sessionId": "s1", "kind": "interactive"}],
    )
    monkeypatch.setattr(daemon, "_controlling_tty", lambda pid: None)
    d.seed_existing_sessions()
    assert "s1" not in d.session_panes

    d.handle_hook_event(
        {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash", "tmux_pane": "%9"}
    )
    assert d.session_panes["s1"] == "%9"
