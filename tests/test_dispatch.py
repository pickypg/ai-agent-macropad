import subprocess
import types

import daemon


def fake_result(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# --- _dispatch_tmux: representative of the subprocess-driven dispatch
# helpers' shared shape (success / nonzero exit / not-found / timeout). ---


def test_dispatch_tmux_success(recording_daemon, monkeypatch):
    d, _ = recording_daemon
    monkeypatch.setattr(daemon.subprocess, "run", lambda *a, **k: fake_result(0))
    assert d._dispatch_tmux(0, "%3") is True


def test_dispatch_tmux_nonzero_exit_is_failure(recording_daemon, monkeypatch):
    d, _ = recording_daemon
    monkeypatch.setattr(
        daemon.subprocess, "run", lambda *a, **k: fake_result(1, stderr="can't find pane")
    )
    assert d._dispatch_tmux(0, "%3") is False


def test_dispatch_tmux_missing_binary_is_failure(recording_daemon, monkeypatch):
    d, _ = recording_daemon

    def raise_not_found(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(daemon.subprocess, "run", raise_not_found)
    assert d._dispatch_tmux(0, "%3") is False


def test_dispatch_tmux_timeout_is_failure(recording_daemon, monkeypatch):
    d, _ = recording_daemon

    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="tmux", timeout=2)

    monkeypatch.setattr(daemon.subprocess, "run", raise_timeout)
    assert d._dispatch_tmux(0, "%3") is False


# --- dispatch_bring_to_front: fallthrough order across targets ---------


def test_dispatch_bring_to_front_tries_tmux_first(recording_daemon, monkeypatch):
    d, _ = recording_daemon
    d.session_panes["s1"] = "%3"
    d.session_ttys["s1"] = "/dev/ttys003"
    d.session_projects["s1"] = "my-project"

    calls = []
    monkeypatch.setattr(d, "_dispatch_tmux", lambda *a: calls.append("tmux") or True)
    monkeypatch.setattr(d, "_dispatch_terminal", lambda *a: calls.append("terminal") or True)
    monkeypatch.setattr(d, "_dispatch_vscode", lambda *a: calls.append("vscode") or True)
    monkeypatch.setattr(d, "_dispatch_intellij", lambda *a: calls.append("intellij") or True)

    d.dispatch_bring_to_front(0, "s1")
    assert calls == ["tmux"]


def test_dispatch_bring_to_front_falls_through_to_terminal(recording_daemon, monkeypatch):
    d, _ = recording_daemon
    d.session_panes["s1"] = "%3"
    d.session_ttys["s1"] = "/dev/ttys003"

    calls = []
    monkeypatch.setattr(d, "_dispatch_tmux", lambda *a: calls.append("tmux") or False)
    monkeypatch.setattr(d, "_dispatch_terminal", lambda *a: calls.append("terminal") or True)
    monkeypatch.setattr(d, "_dispatch_vscode", lambda *a: calls.append("vscode") or True)

    d.dispatch_bring_to_front(0, "s1")
    assert calls == ["tmux", "terminal"]


def test_dispatch_bring_to_front_falls_through_to_vscode_then_intellij(
    recording_daemon, monkeypatch
):
    d, _ = recording_daemon
    d.session_projects["s1"] = "my-project"  # no pane, no tty known

    calls = []
    monkeypatch.setattr(d, "_dispatch_vscode", lambda *a: calls.append("vscode") or False)
    monkeypatch.setattr(d, "_dispatch_intellij", lambda *a: calls.append("intellij") or True)

    d.dispatch_bring_to_front(0, "s1")
    assert calls == ["vscode", "intellij"]


def test_dispatch_bring_to_front_no_target_known_is_a_noop(recording_daemon):
    d, _ = recording_daemon
    # No pane, tty, or project recorded for this session — should not
    # raise, just log and return.
    d.dispatch_bring_to_front(0, "unknown-session")


# --- on_device_event: key press routing ---------------------------------


def test_key_press_with_no_mapped_session_clears_the_slot(recording_daemon):
    d, sent = recording_daemon
    d.on_device_event({"t": "key", "i": 0})
    assert sent == [{"t": "clear", "i": 0}]


def test_key_press_with_mapped_session_dispatches(recording_daemon, monkeypatch):
    d, sent = recording_daemon
    d.slots.allocate("s1")  # occupies slot 0

    calls = []
    monkeypatch.setattr(d, "dispatch_bring_to_front", lambda slot, sid: calls.append((slot, sid)))

    d.on_device_event({"t": "key", "i": 0})
    assert calls == [(0, "s1")]
    assert sent == []  # no clear sent — a real session was found


def test_key_press_out_of_range_index_is_ignored(recording_daemon):
    d, sent = recording_daemon
    d.on_device_event({"t": "key", "i": 999})
    assert sent == []


def test_encoder_and_click_events_do_not_raise(recording_daemon):
    d, _ = recording_daemon
    d.on_device_event({"t": "enc", "d": 1})
    d.on_device_event({"t": "enc_click"})
    d.on_device_event({"t": "something_unknown"})
