#!/bin/bash
# AI Agent Macropad — Grok Build hook adapter.
#
# Translates Grok Build's own hook JSON into this repo's agent-agnostic
# wire format (see daemon.py's module docstring) and forwards it to the
# daemon's socket. claude/hook.sh and codex/hook.sh are the same idea
# for the other two agents — see them for comparison. Unlike those two
# (whose native hook payloads already match this repo's wire format
# almost verbatim), Grok Build's own envelope diverges enough that this
# script does real translation, not just field injection — confirmed
# against a real `grok -p ...` run through this exact script into a
# live daemon.py during development, not guessed from Grok's bundled
# docs alone (the docs turned out to disagree with the real wire values
# in one important way — see below).
#
# Divergences from Claude Code/Codex's hook shape, confirmed live:
#
# 1. Every field is camelCase (hookEventName, sessionId, toolName,
#    notificationType, ...), not snake_case. Renamed below via jq.
#
# 2. hookEventName's *value* is snake_case ("session_start",
#    "pre_tool_use", "post_tool_use", "stop", "session_end",
#    "permission_denied", ...) even though Grok's own hooks reference
#    documents the event names in PascalCase ("SessionStart",
#    "PreToolUse", ...) for hook-file matching purposes. The EVENT_MAP
#    below translates the real wire value to the PascalCase vocabulary
#    hook_to_state() in daemon.py expects. Unrecognized values pass
#    through unchanged, which hook_to_state() safely maps to no state
#    change (same fallback as an unknown PascalCase event).
#
# 3. Grok's permission system has no PermissionRequest-style "waiting
#    on you" event — permission_denied (mapped to PermissionDenied
#    below) fires only *after* a call has already been auto-denied by a
#    rule, with nothing left pending, so it deliberately maps to no
#    state change in hook_to_state() rather than "question". The real
#    "a permission UI is actually waiting" signal is
#    Notification/notificationType=permission_prompt, confirmed
#    reliable by Grok's own docs (unlike Claude Code's own
#    Notification:permission_prompt, confirmed unreliable there — see
#    hook_to_state()'s docstring in daemon.py).
#
# 4. A `stop` with reason "channel_closed" or "shutdown" fires at
#    session teardown, strictly after SessionEnd already freed this
#    session's slot (confirmed live: SessionEnd arrives first, then
#    this second Stop). Forwarding it would resurrect the now-freed
#    slot via handle_hook_event()'s lazy-allocation fallback, leaving a
#    phantom "done" slot for a session that's already gone and will
#    never send another SessionEnd to clear it. Grok's own hooks
#    reference explicitly recommends checking `reason == "end_turn"`
#    for exactly this reason, so a non-end_turn Stop is dropped below
#    instead of forwarded, rather than teaching daemon.py about a
#    "reason" field no other agent sends.
#
# 5. PostToolUseFailure exists in Grok's documented event list, but
#    empirically (a nonzero shell exit code, and a nonexistent-file
#    read, both tried live) an ordinary tool-level failure still comes
#    back as a plain post_tool_use, same gap as Codex — see
#    hook_to_state()'s docstring and the README's Grok Build section.
#    Translated anyway (in case a genuine infra-level failure does use
#    it) since it costs nothing: hook_to_state() already maps
#    PostToolUseFailure to "error" for Claude Code.
#
# Like claude/hook.sh, a few fields aren't part of Grok's own payload
# and need to be injected:
#
# 1. tmux_pane — from this script's own $TMUX_PANE.
# 2. controlling_tty — SessionStart only, via `ps -o tty= -p "$PPID"`,
#    same rationale as the other two adapters.
# 3. agent — always "grok-build". Bookkeeping only (see daemon.py's
#    session_agents) — doesn't affect state mapping.
#
# This script only renames/adds fields; Grok's own camelCase fields
# (toolInput, toolResult, transcriptPath, lastAssistantMessage, ...)
# are left in the forwarded payload untouched, same "enrich, don't
# strip" approach as claude/hook.sh and codex/hook.sh — daemon.py's
# payload.get() calls simply ignore whatever it doesn't look for.
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
event=$(printf '%s' "$input" | jq -r '.hookEventName // empty')

if [ "$event" = "stop" ]; then
  reason=$(printf '%s' "$input" | jq -r '.reason // empty')
  if [ "$reason" != "end_turn" ]; then
    exit 0
  fi
fi

ctty=""
if [ "$event" = "session_start" ]; then
  tty=$(ps -o tty= -p "$PPID" 2>/dev/null | tr -d ' ')
  if [ -n "$tty" ] && [ "$tty" != "??" ]; then
    ctty="/dev/$tty"
  fi
fi

EVENT_MAP='{
  "session_start": "SessionStart",
  "user_prompt_submit": "UserPromptSubmit",
  "pre_tool_use": "PreToolUse",
  "post_tool_use": "PostToolUse",
  "post_tool_use_failure": "PostToolUseFailure",
  "permission_denied": "PermissionDenied",
  "notification": "Notification",
  "stop": "Stop",
  "stop_failure": "StopFailure",
  "stop_cancelled": "StopCancelled",
  "subagent_start": "SubagentStart",
  "subagent_stop": "SubagentStop",
  "pre_compact": "PreCompact",
  "post_compact": "PostCompact",
  "session_end": "SessionEnd"
}'

printf '%s' "$input" | jq -c \
  --argjson events "$EVENT_MAP" \
  --arg pane "$TMUX_PANE" \
  --arg ctty "$ctty" \
  '.hook_event_name = ($events[.hookEventName] // .hookEventName)
   | .session_id = .sessionId
   | .tool_name = .toolName
   | .notification_type = .notificationType
   | .tmux_pane = $pane
   | .agent = "grok-build"
   | (if $ctty != "" then .controlling_tty = $ctty else . end)' \
  | nc -U -w1 "$SOCKET" >/dev/null 2>&1

exit 0
