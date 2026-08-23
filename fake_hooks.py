#!/usr/bin/env python3
"""
Fake an AI agent session's hook lifecycle against the daemon, so you
can develop/test daemon.py before wiring real hooks.

Usage:
    python3 fake_hooks.py                       # one simulated Claude Code session
    python3 fake_hooks.py --sessions 3          # three concurrent sessions, staggered
    python3 fake_hooks.py --agent codex         # simulate a Codex CLI session instead
    python3 fake_hooks.py --agent grok-build    # simulate a Grok Build session instead
    python3 fake_hooks.py --agent cursor        # simulate a Cursor CLI session instead

--agent only changes which event sequence gets simulated (see
simulate_session() below) and tags payloads with "agent" accordingly —
daemon.py's own mapping logic doesn't fork per agent, so this is purely
about exercising each agent's real event vocabulary (see
hook_to_state()'s docstring in daemon.py for exactly where they
diverge). All four simulators speak the shared post-translation
PascalCase shape hook_to_state() expects — for Grok Build, that's
already through the same camelCase/snake_case-value -> PascalCase
translation grok/hook.sh does for a real session, not Grok's own native
wire format; same idea for Cursor and cursor/hook.sh, except Cursor's
translation is UNVERIFIED against a real `agent` run (see
cursor/hook.sh's own comments) — this simulator is only as accurate as
that guesswork, not a substitute for testing against the real CLI.
"""
import argparse
import json
import socket
import time
import uuid
import threading
import os

SOCKET_PATH = os.path.expanduser("~/.ai-agent-macropad/daemon.sock")


def send(payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(SOCKET_PATH)
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))


def simulate_claude_code_session(base):
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


def simulate_codex_session(base):
    # Codex has no AskUserQuestion/ExitPlanMode-style tool names and no
    # Notification event (see ATTENTION_TOOLS and hook_to_state()'s
    # docstring in daemon.py) — PermissionRequest is the only "blocked
    # on you" signal available, so it's exercised twice here instead.
    # Tool names below are the real ones — confirmed live against
    # actual Codex CLI hook payloads: its shell tool reports tool_name
    # "Bash", same as Claude Code's.
    send({**base, "hook_event_name": "SessionStart"})
    time.sleep(0.5)

    send({**base, "hook_event_name": "UserPromptSubmit"})
    time.sleep(0.5)

    send({**base, "hook_event_name": "PreToolUse", "tool_name": "Bash"})
    time.sleep(0.5)

    send({**base, "hook_event_name": "PostToolUse", "tool_name": "Bash"})
    time.sleep(0.5)

    send({**base, "hook_event_name": "PreToolUse", "tool_name": "apply_patch"})
    time.sleep(0.5)

    # A shell command needs approval — blocked on you, same urgency as
    # Claude Code's PermissionRequest handling
    send({**base, "hook_event_name": "PermissionRequest", "tool_name": "Bash"})
    time.sleep(2.0)

    send({**base, "hook_event_name": "PostToolUse", "tool_name": "apply_patch"})
    time.sleep(0.5)

    send({**base, "hook_event_name": "Stop"})  # -> done
    time.sleep(1.0)

    send({**base, "hook_event_name": "SessionEnd"})


def simulate_grok_build_session(base):
    # Grok Build's own hook payloads are camelCase with snake_case
    # event-name values (e.g. "pre_tool_use") and get translated to
    # this vocabulary by grok/hook.sh before they ever reach the
    # daemon — so, same as the other two simulators, this speaks the
    # already-translated PascalCase shape hook_to_state() expects, not
    # Grok's own wire format. Tool name ("run_terminal_command") and
    # the permission_prompt/idle_prompt Notification subtypes are the
    # real values — confirmed live against actual Grok Build hook
    # payloads (see grok/hook.sh's and hook_to_state()'s docstrings for
    # what's Grok-specific here: no PermissionRequest-style event,
    # Notification:permission_prompt is the reliable "blocked on you"
    # signal instead).
    send({**base, "hook_event_name": "SessionStart"})
    time.sleep(0.5)

    send({**base, "hook_event_name": "UserPromptSubmit"})
    time.sleep(0.5)

    send({**base, "hook_event_name": "PreToolUse", "tool_name": "run_terminal_command"})
    time.sleep(0.5)

    # A permission UI is actually waiting on you — confirmed live and
    # reliable for Grok Build, unlike Claude Code's version of this
    # same Notification subtype
    send({
        **base,
        "hook_event_name": "Notification",
        "notification_type": "permission_prompt",
    })
    time.sleep(2.0)

    send({**base, "hook_event_name": "PostToolUse", "tool_name": "run_terminal_command"})
    time.sleep(0.5)

    # lower-urgency notification — Grok Build's just idle, not blocked
    send({
        **base,
        "hook_event_name": "Notification",
        "notification_type": "idle_prompt",
    })
    time.sleep(1.5)

    send({**base, "hook_event_name": "Stop"})  # -> done
    time.sleep(1.0)

    send({**base, "hook_event_name": "SessionEnd"})


def simulate_cursor_session(base):
    # UNVERIFIED (see cursor/hook.sh's own comments) — no permission-
    # prompt/notification-style hook is documented for Cursor at all,
    # so unlike the other three simulators, this one never lights up
    # "question" or "waiting". Tool call is exercised via the Shell-
    # specific before/after pair rather than generic PreToolUse/
    # PostToolUse, since community reports suggest that pair is more
    # likely to actually fire for the standalone CLI in practice —
    # cursor/hook.sh maps both onto PreToolUse/PostToolUse either way.
    send({**base, "hook_event_name": "SessionStart"})
    time.sleep(0.5)

    send({**base, "hook_event_name": "UserPromptSubmit"})
    time.sleep(0.5)

    send({**base, "hook_event_name": "PreToolUse", "tool_name": "Shell"})
    time.sleep(0.5)

    send({**base, "hook_event_name": "PostToolUse", "tool_name": "Shell"})
    time.sleep(0.5)

    send({**base, "hook_event_name": "Stop"})  # -> done
    time.sleep(1.0)

    send({**base, "hook_event_name": "SessionEnd"})


SIMULATORS = {
    "claude-code": simulate_claude_code_session,
    "codex": simulate_codex_session,
    "grok-build": simulate_grok_build_session,
    "cursor": simulate_cursor_session,
}


def simulate_session(cwd_name, agent):
    session_id = str(uuid.uuid4())
    base = {
        "session_id": session_id,
        "cwd": f"/Users/you/code/{cwd_name}",
        "agent": agent,
    }
    SIMULATORS[agent](base)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=1)
    parser.add_argument(
        "--agent", choices=sorted(SIMULATORS), default="claude-code",
        help="which agent's hook event sequence to simulate (default: claude-code)",
    )
    args = parser.parse_args()

    names = ["api-refactor", "flaky-tests", "docs-pass", "migration"]
    threads = [
        threading.Thread(
            target=simulate_session, args=(names[i % len(names)], args.agent)
        )
        for i in range(args.sessions)
    ]
    for t in threads:
        t.start()
        time.sleep(0.2)  # stagger starts so slot allocation order is visible
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
