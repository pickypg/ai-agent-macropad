import daemon


def test_allocate_assigns_first_free_slot_and_is_idempotent():
    slots = daemon.SlotManager(3)
    assert slots.allocate("a") == 0
    assert slots.allocate("a") == 0  # repeat call for the same session is a no-op
    assert slots.allocate("b") == 1


def test_allocate_returns_none_when_full():
    slots = daemon.SlotManager(1)
    assert slots.allocate("a") == 0
    assert slots.allocate("b") is None


def test_free_releases_slot_for_reuse():
    slots = daemon.SlotManager(1)
    slots.allocate("a")
    assert slots.free("a") == 0
    assert slots.allocate("b") == 0


def test_free_unknown_session_is_a_noop():
    slots = daemon.SlotManager(2)
    assert slots.free("never-allocated") is None


def test_slot_for_reflects_current_mapping():
    slots = daemon.SlotManager(2)
    assert slots.slot_for("a") is None
    slots.allocate("a")
    assert slots.slot_for("a") == 0
    slots.free("a")
    assert slots.slot_for("a") is None


def test_evict_clears_an_occupied_slot_and_returns_its_session():
    slots = daemon.SlotManager(2)
    slots.allocate("a")
    assert slots.evict(0) == "a"
    assert slots.slot_for("a") is None
    assert slots.allocate("b") == 0  # slot is free again


def test_evict_empty_slot_is_a_noop():
    slots = daemon.SlotManager(2)
    assert slots.evict(0) is None
