import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# --- daemon.py fixtures --------------------------------------------------


@pytest.fixture
def recording_daemon(monkeypatch):
    """A Daemon() with pad.write_json replaced by a recorder, so tests
    can assert on what would've been sent to the pad without a real
    connection. The pad's open() is never called in these tests (only
    serve() calls it), so no discovery probing happens either.
    """
    import daemon as daemon_mod

    d = daemon_mod.Daemon()
    sent = []
    monkeypatch.setattr(d.pad, "write_json", sent.append)
    return d, sent
