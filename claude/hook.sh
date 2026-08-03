#!/bin/bash
# Claude Code Macropad — hook script.
#
# Wired into settings.json for SessionStart, UserPromptSubmit,
# PreToolUse, PostToolUse, PermissionRequest, Notification, Stop,
# SubagentStop, PostToolUseFailure, and SessionEnd. Claude Code
# already includes "hook_event_name" and "session_id" in the JSON it
# sends on stdin for every event, but a few fields need to be injected
# by this script rather than trusted from Claude Code itself:
#
# 1. notification_type — Notification:permission_prompt was confirmed
#    unreliable (see daemon.py's hook_to_state docstring); the
#    matcher branches in settings.json still set
#    MACROPAD_NOTIFICATION_TYPE for the subtypes that DO work
#    (agent_needs_input, idle_prompt), injected below.
#
# 2. tmux_pane — needed for Phase 5 dispatch (key press -> bring that
#    session's window to the front). Read from this script's own
#    $TMUX_PANE, empty string if not running inside tmux.
#
# 3. controlling_tty — SessionStart only, for the Terminal.app dispatch
#    path. Claude Code runs hook subprocesses in their own session
#    with no controlling terminal (confirmed in the hooks reference:
#    command hooks "run in their own session without a controlling
#    terminal... can't open /dev/tty"), so hook.sh itself has none to
#    report. Its PARENT process — the one Claude Code actually spawned
#    to run this hook command — still does, though; only the detached
#    hook subprocess lacks one. One `ps -o tty=` call against $PPID
#    gets it. Restricted to SessionStart since ps has real cost and
#    a session's controlling tty doesn't change over its lifetime.
#
# -c is required on jq: its default output is pretty-printed across
# multiple lines, which breaks the newline-delimited-JSON protocol
# both code.py and daemon.py rely on (one full object per line) — see
# the incident where this bit permission_prompt debugging earlier.
#
# Fails open on purpose: if the daemon isn't running, or the socket
# write times out, this must never block a tool call or a session.
# PreToolUse/PostToolUse fire on every single tool use, so this also
# needs to stay fast — nc's -w1 caps the connection attempt at 1s, and
# the ps call above is skipped entirely except at SessionStart.

SOCKET="$HOME/.claude-macropad.sock"

input=$(cat)
event=$(printf '%s' "$input" | jq -r '.hook_event_name // empty')

ctty=""
if [ "$event" = "SessionStart" ]; then
  tty=$(ps -o tty= -p "$PPID" 2>/dev/null | tr -d ' ')
  if [ -n "$tty" ] && [ "$tty" != "??" ]; then
    ctty="/dev/$tty"
  fi
fi

printf '%s' "$input" | jq -c \
  --arg pane "$TMUX_PANE" \
  --arg ntype "$MACROPAD_NOTIFICATION_TYPE" \
  --arg ctty "$ctty" \
  '.tmux_pane = $pane
   | (if $ntype != "" then .notification_type = $ntype else . end)
   | (if $ctty  != "" then .controlling_tty = $ctty  else . end)' \
  | nc -U -w1 "$SOCKET" >/dev/null 2>&1

exit 0
