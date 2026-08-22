import asyncio

import daemon


class FakePad:
    """Stands in for HidPadLink in Daemon._reconcile_pad() tests — just
    tracks .attached and how many times open()/close() were called,
    without any real HID I/O to avoid actually probing for hardware.
    """

    def __init__(self, attached=False):
        self.attached = attached
        self.open_calls = 0
        self.close_calls = 0
        self.written = []

    def open(self):
        self.open_calls += 1
        self.attached = True

    def close(self):
        self.close_calls += 1
        self.attached = False

    def write_json(self, msg):
        self.written.append(msg)


def test_reconcile_opens_pad_when_a_session_is_active():
    d = daemon.Daemon()
    d.pad = FakePad(attached=False)
    d.slots.allocate("s1")

    asyncio.run(d._reconcile_pad())

    assert d.pad.attached is True
    assert d.pad.open_calls == 1


def test_reconcile_closes_pad_when_no_sessions_are_active():
    d = daemon.Daemon()
    d.pad = FakePad(attached=True)  # simulate already open from an earlier session

    asyncio.run(d._reconcile_pad())

    assert d.pad.attached is False
    assert d.pad.close_calls == 1


def test_reconcile_is_a_noop_when_state_already_matches():
    d = daemon.Daemon()
    d.pad = FakePad(attached=False)
    # Zero sessions, pad already closed — nothing to reconcile.
    asyncio.run(d._reconcile_pad())
    assert d.pad.open_calls == 0
    assert d.pad.close_calls == 0


def test_delayed_close_is_a_noop_if_a_session_is_active_by_the_time_it_runs():
    """The debounce isn't implemented as cancel-on-transition — a
    scheduled close just recomputes "should the pad be open?" when it
    actually runs. A session that starts before the delay elapses (the
    real-world case: SessionEnd schedules a delayed close, a new
    SessionStart arrives before it fires) makes this a no-op without
    needing to explicitly cancel anything.
    """
    d = daemon.Daemon()
    d.pad = FakePad(attached=True)
    d.slots.allocate("s1")  # active by the time the delayed reconcile runs

    asyncio.run(d._reconcile_pad(delay=0.05))

    assert d.pad.close_calls == 0
    assert d.pad.attached is True


def test_kick_reconcile_is_a_noop_outside_a_running_event_loop():
    """Most of test_handle_hook_event.py calls handle_hook_event()
    directly and synchronously, with no asyncio event loop running.
    _kick_reconcile() (called from the SessionStart/SessionEnd/lazy-
    allocation paths) must not raise in that case, and must not have
    scheduled any pad I/O — there is nothing to reconcile without a
    loop actually driving it.
    """
    d = daemon.Daemon()
    d.pad = FakePad(attached=False)
    d.slots.allocate("s1")

    d._kick_reconcile()  # no running loop here

    assert d.pad.open_calls == 0


def test_reconcile_resyncs_cached_slot_state_when_reopening():
    """Reproduces the real-world race: a SessionStart's own _send_pad()
    call runs synchronously before its _kick_reconcile() task actually
    gets to open the pad (see the note in _reconcile_pad's docstring),
    so that write lands on a still-closed transport and is dropped.
    Without an explicit resync right after open() succeeds, that
    session's slot would never reach the pad until its next hook event.
    """
    d = daemon.Daemon()
    d.pad = FakePad(attached=False)
    # Stand in for the dropped write: allocate + cache state as
    # SessionStart would, without a transport open to actually receive
    # it (mirrors _send_pad()'s behavior when self.pad.write_json() is
    # a no-op because the device isn't attached yet).
    d.slots.allocate("s1")
    d.pad_state[0] = {"t": "slot", "i": 0, "state": "idle", "label": "s1"}

    asyncio.run(d._reconcile_pad())

    assert d.pad.attached is True
    assert d.pad.written == [{"t": "slot", "i": 0, "state": "idle", "label": "s1"}]


def test_reconcile_does_not_resync_when_pad_fails_to_attach():
    """open() found no pad — nothing changed, so resyncing (and logging
    that it did) would be misleading; there's nothing to resync onto.
    """
    class FailingPad(FakePad):
        def open(self):
            self.open_calls += 1
            # stays unattached — simulates no hardware answering

    d = daemon.Daemon()
    d.pad = FailingPad(attached=False)
    d.pad_state[0] = {"t": "slot", "i": 0, "state": "idle", "label": "s1"}
    d.slots.allocate("s1")

    asyncio.run(d._reconcile_pad())

    assert d.pad.attached is False
    assert d.pad.written == []


def test_kick_reconcile_opens_pad_inside_a_running_event_loop():
    d = daemon.Daemon()
    d.pad = FakePad(attached=False)
    d.slots.allocate("s1")

    async def run_briefly():
        d._kick_reconcile()
        await asyncio.sleep(0.05)

    asyncio.run(run_briefly())

    assert d.pad.open_calls == 1
    assert d.pad.attached is True
