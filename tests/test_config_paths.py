from pathlib import Path

import daemon


def test_socket_and_events_log_live_under_config_dir():
    assert Path(daemon.SOCKET_PATH).parent == daemon.CONFIG_DIR
    assert Path(daemon.EVENTS_LOG_PATH).parent == daemon.CONFIG_DIR


def test_config_dir_is_created():
    assert daemon.CONFIG_DIR.is_dir()
