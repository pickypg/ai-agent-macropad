#!/usr/bin/env python3
"""
Fake a Claude Code session's hook lifecycle against the daemon,
so you can develop/test daemon.py before wiring real hooks (Phase 4).

Usage:
    python3 fake_hooks.py                  # one simulated session
    python3 fake_hooks.py --sessions 3     # simulate 3 concurrent sessions
"""
import argparse
import json
import socket
import time
import uuid
import threading
import os

SOCKET_PATH = os.path.expanduser("~/.claude-macropad/daemon.sock")


def send(payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(SOCKET_PATH)
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))


def simulate_session(cwd_name):
    session_id = str(uuid.uuid4())
    base = {"session_id": session_id, "cwd": f"/Users/you/code/{cwd_name}"}

    send({**base, "hook_event_name": "SessionStart"})
    time.sleep(0.5)

    send({**base, "hook_event_name": "UserPromptSubmit"})
    time.sleep(0.5)

    send({**base, "hook_event_name": "PreToolUse", "tool_name": "Read"})
    time.sleep(0.5)

    send({**base, "hook_event_name": "PostToolUse", "tool_name": "Read"})
    time.sleep(0.5)

    # Claude blocks on a choice — this should light the slot up as
    # "question" and blink, not just look like any other tool call
    send({**base, "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion"})
    time.sleep(3.0)

    send({**base, "hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion"})
    time.sleep(0.5)

    # Same attention state, different tool — Claude wants to leave
    # plan mode and start executing
    send({**base, "hook_event_name": "PreToolUse", "tool_name": "ExitPlanMode"})
    time.sleep(3.0)

    send({**base, "hook_event_name": "PostToolUse", "tool_name": "ExitPlanMode"})
    time.sleep(0.5)

    # Bash wants to run something and is blocked on an "Allow this
    # command?" prompt — confirmed live that PermissionRequest fires
    # reliably for this, unlike Notification:permission_prompt
    send({**base, "hook_event_name": "PermissionRequest", "tool_name": "Bash"})
    time.sleep(2.0)

    # lower-urgency notification — Claude's just idle, not blocked
    send({
        **base,
        "hook_event_name": "Notification",
        "notification_type": "idle_prompt",
    })
    time.sleep(1.5)

    send({**base, "hook_event_name": "Stop"})  # -> done
    time.sleep(1.0)

    send({**base, "hook_event_name": "SessionEnd"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=1)
    args = parser.parse_args()

    names = ["api-refactor", "flaky-tests", "docs-pass", "migration"]
    threads = [
        threading.Thread(target=simulate_session, args=(names[i % len(names)],))
        for i in range(args.sessions)
    ]
    for t in threads:
        t.start()
        time.sleep(0.2)  # stagger starts so slot allocation order is visible
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
