#!/bin/bash
# AI Agent Macropad — Codex CLI hook adapter.
#
# Translates Codex CLI's own hook JSON into this repo's agent-agnostic
# wire format (see daemon.py's module docstring) and forwards it to
# the daemon's socket. claude/hook.sh is the same idea for Claude
# Code — see it for comparison. The two scripts are nearly identical
# because Codex's hooks reference documents the same event names
# (SessionStart, UserPromptSubmit, PreToolUse, PermissionRequest,
# PostToolUse, Stop, SubagentStop, SessionEnd) and the same core
# payload fields (hook_event_name, session_id, cwd, tool_name) as
# Claude Code's — see hook_to_state()'s docstring in daemon.py for the
# couple of places the two agents' hooks diverge (no Notification or
# PostToolUseFailure equivalent here).
#
# Wired into hooks.json (see codex/example_hooks.json) for the same
# event set daemon.py's handle_hook_event() cares about, minus the two
# Claude-only ones above. Codex already includes "hook_event_name" and
# "session_id" in the JSON it sends on stdin for every event (same
# stdin-JSON delivery as Claude Code — confirmed against Codex's own
# hooks reference), but a couple of fields still need to be injected
# here rather than trusted from Codex itself:
#
# 1. tmux_pane — needed for dispatch (key press -> bring that session's
#    window to the front). Read from this script's own $TMUX_PANE,
#    empty string if not running inside tmux.
#
# 2. controlling_tty — SessionStart only, for the Terminal.app dispatch
#    path. Same approach as claude/hook.sh: read the *parent* process's
#    tty via `ps -o tty= -p "$PPID"`, since a hook command itself is
#    commonly spawned without a controlling terminal of its own. If
#    that assumption doesn't hold for Codex's own hook subprocess model,
#    this just comes back empty — dispatch_bring_to_front() in
#    daemon.py already falls through to VS Code/IntelliJ project-name
#    matching when no tty is known, so this degrades gracefully either
#    way rather than breaking anything.
#
# 3. agent — always "codex" here. Bookkeeping only (see daemon.py's
#    session_agents) — doesn't affect state mapping.
#
# -c is required on jq: its default output is pretty-printed across
# multiple lines, which breaks the newline-delimited-JSON protocol
# daemon.py relies on (one full object per line).
#
# Fails open on purpose: if the daemon isn't running, or the socket
# write times out, this must never block a tool call or a turn.
# PreToolUse/PostToolUse fire on every single tool use, so this also
# needs to stay fast — nc's -w1 caps the connection attempt at 1s, and
# the ps call above is skipped entirely except at SessionStart.

SOCKET="$HOME/.ai-agent-macropad/daemon.sock"

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
  --arg ctty "$ctty" \
  '.tmux_pane = $pane
   | .agent = "codex"
   | (if $ctty != "" then .controlling_tty = $ctty else . end)' \
  | nc -U -w1 "$SOCKET" >/dev/null 2>&1

exit 0
