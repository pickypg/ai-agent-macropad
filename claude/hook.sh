#!/bin/bash
# AI Agent Macropad — Claude Code hook adapter.
#
# Translates Claude Code's own hook JSON into this repo's agent-
# agnostic wire format (see daemon.py's module docstring) and forwards
# it to the daemon's socket. codex/hook.sh is the same idea for Codex
# CLI — see it for comparison; the two scripts diverge only where
# their agents' own hook payloads do.
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
# 4. agent — always "claude-code" here. Bookkeeping only (see
#    daemon.py's session_agents) — doesn't affect state mapping.
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
#
# Guard against foreign payloads: some agents also load
# ~/.claude/settings.json for "Claude Code compatibility" and fire
# this hook with their OWN native envelope instead of Claude Code's.
# Two confirmed-live cases, needing two different checks:
#
# 1. Grok Build's envelope is camelCase (hookEventName/sessionId), not
#    this script's expected hook_event_name/session_id — those two
#    fields just come back empty, caught by the emptiness check below.
#
# 2. Cursor CLI (confirmed live 2026-08-23) is a harder case: unlike
#    Grok, it does NOT gate this behind the opt-in "third-party
#    hooks"/skills setting its own docs describe (or that setting
#    defaults to on for the CLI, unconfirmed which) — real Claude hooks
#    fired for real Cursor sessions with no config change on this end.
#    Worse, its payload isn't obviously foreign: it uses the real key
#    names hook_event_name and session_id (not Grok's differently-named
#    ones), so the emptiness check alone doesn't catch it — confirmed
#    by a live daemon.py log showing the SAME session_id logged twice
#    for the same real event, once via this script (event
#    "postToolUse", agent incorrectly claude-code) and once via
#    cursor/hook.sh (event "PostToolUse", agent correctly cursor).
#    What DOES distinguish it: Cursor's hook_event_name *values* are
#    its own camelCase ("postToolUse", "sessionStart", ...), never
#    Claude Code's real PascalCase ("PostToolUse", "SessionStart",
#    ...) — the case statement below drops anything not starting with
#    an uppercase letter for exactly this reason. cursor/hook.sh does
#    its own camelCase -> PascalCase translation on its copy before
#    forwarding, so the real event still reaches the daemon correctly
#    tagged agent=cursor either way — this guard only stops the
#    untranslated, mistagged duplicate from claude/hook.sh.
SOCKET="$HOME/.ai-agent-macropad/daemon.sock"

input=$(cat)
event=$(printf '%s' "$input" | jq -r '.hook_event_name // empty')
session_id=$(printf '%s' "$input" | jq -r '.session_id // empty')
if [ -z "$event" ] || [ -z "$session_id" ]; then
  exit 0
fi
# export (not just set) is required here, not cosmetic: [A-Z] is a
# locale-collated bracket expression, not a hardcoded ASCII range —
# confirmed live (2026-08-23) that under en_US.UTF-8 (apparently what
# Cursor's interactive-mode hook subprocess is spawned with, unlike its
# -p/print mode, which never reproduced this), "beforeSubmitPrompt"/
# "preToolUse"/etc. WRONGLY match [A-Z]*, silently defeating this whole
# guard — confirmed the fix by reproducing the exact false-positive
# match under LC_ALL=en_US.UTF-8 and confirming [A-Z]* behaves
# correctly again once LC_ALL=C is exported. A plain (non-exported)
# LC_ALL=C is not sufficient — bash's own collation reads the real
# process environment, not just shell-local variables.
export LC_ALL=C
case "$event" in
  [A-Z]*) ;;  # looks like Claude Code's real PascalCase vocabulary
  *) exit 0 ;;  # camelCase value under the right key name — still foreign (see above)
esac

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
   | .agent = "claude-code"
   | (if $ntype != "" then .notification_type = $ntype else . end)
   | (if $ctty  != "" then .controlling_tty = $ctty  else . end)' \
  | nc -U -w1 "$SOCKET" >/dev/null 2>&1

exit 0
