#!/bin/bash
# AI Agent Macropad — Cursor CLI hook adapter.
#
# Translates Cursor's own hook JSON into this repo's agent-agnostic
# wire format (see daemon.py's module docstring) and forwards it to
# the daemon's socket. claude/hook.sh, codex/hook.sh, and grok/hook.sh
# are the same idea for the other three agents — see them for
# comparison.
#
# VERIFIED (2026-08-23) against both real interactive `agent` sessions
# and real `agent -p` runs — session lifecycle, session correlation,
# the Claude-compat cross-firing issue (see claude/hook.sh),
# controlling_tty capture, key-press dispatch (a pad key press
# correctly raises the right window), and every event in EVENT_MAP
# below except beforeMCPExecution/afterMCPExecution (no MCP server was
# available to test — the one genuinely unconfirmed gap left). Two
# events are confirmed to NOT fire, deliberately tested rather than
# assumed: subagentStart/subagentStop, even for a real subagent
# delegation (`agent -p ... "use a subagent to ..."`) — it surfaces
# only as an ordinary PreToolUse/PostToolUse pair for a "Task" tool
# (`tool_input.subagent_type`); and preCompact, even for a genuine
# manual `/compact` (context visibly condensed into a real summary,
# no hook fired at all — auto-triggered compaction from hitting the
# context-window threshold organically is still untested, but the
# deliberate-trigger path now has real negative evidence too). See #3
# below for the rest of what's confirmed live.
#
# 1. Event names on the wire are camelCase ("sessionStart",
#    "preToolUse", "stop", ...), not PascalCase — EVENT_MAP below
#    translates to the PascalCase vocabulary hook_to_state() in
#    daemon.py expects, same idea as grok/hook.sh's translation (though
#    Cursor's own field *names*, unlike Grok's, are already snake_case,
#    so only the hook_event_name *value* needs translating here, not
#    every field).
#
# 2. Session correlation: Cursor's docs claim session_id is only
#    present on sessionStart/sessionEnd, with conversation_id as the
#    only field guaranteed on every event — but that's now contradicted
#    by a live capture (2026-08-23): a real postToolUse payload for the
#    CLI carried a top-level session_id equal to its conversation_id
#    (seen indirectly, via claude/hook.sh's own foreign-payload guard
#    logging the same raw event under its real key names — see that
#    script's comments). So this script now prefers the payload's own
#    session_id when present, falling back to conversation_id only if
#    it's ever actually missing (matching the docs' claim for at least
#    sessionStart/sessionEnd) rather than always overwriting a
#    perfectly good native field with a same-valued substitute.
#    Not yet confirmed: whether session_id and conversation_id ever
#    diverge in practice (e.g. across subagent calls, or a resumed
#    session) — if they do, this preference order is the more correct
#    one either way, since session_id is Cursor's own name for the
#    concept this repo's wire format also calls session_id.
#
# 3. Community reports (Cursor's own forum, mid-2026) warned the
#    standalone CLI might fire only a subset of its documented events
#    in practice. Not borne out here: live testing (2026-08-23, this
#    exact CLI version) confirmed sessionStart, sessionEnd,
#    beforeSubmitPrompt, preToolUse, postToolUse, postToolUseFailure
#    (fires reliably — contrast with Claude Code/Codex/Grok Build,
#    where the equivalent rarely fires in practice), beforeShellExecution/
#    afterShellExecution, beforeReadFile, and afterFileEdit (real
#    `edits: [{old_string, new_string}]` shape, confirmed) all fire as
#    documented. beforeMCPExecution/afterMCPExecution weren't testable
#    (no MCP server configured) — left wired on the "no-op today,
#    harmless if so" theory grok/hook.sh already uses for
#    PreCompact/PostCompact. preCompact turned out testable after all:
#    a manual `/compact` prompt genuinely condensed context (a real,
#    structured summary came back), but no PreCompact event fired at
#    all — confirmed absent for the manual-trigger path, same category
#    as subagentStart/subagentStop below. (Auto-triggered compaction,
#    from hitting the context-window threshold organically, is still
#    untested — impractical to force deliberately — but there's now
#    real negative evidence for the deliberate path at least.)
#    subagentStart/subagentStop are confirmed to NOT fire — a real
#    subagent delegation surfaces only as a generic PreToolUse/
#    PostToolUse pair for a "Task" tool instead (see the banner above).
#    EVENT_MAP below still wires subagentStart/subagentStop and
#    preCompact anyway, on the same "harmless if it's ever added"
#    theory.
#
# 4. No permission-prompt/notification-style hook is documented for
#    Cursor at all — no "question" (blocked on you) or "waiting" (idle)
#    pad state can ever fire for a Cursor session today, a bigger gap
#    than even Codex's (see hook_to_state()'s docstring in daemon.py).
#    Deliberately re-tested (2026-08-23) after learning Cursor DOES
#    have a permission-ask mechanism: a preToolUse/beforeShellExecution
#    hook's own stdout response can include {"permission": "ask", ...}
#    to force Cursor's approval UI. Confirmed live this is a
#    hook-INITIATED control channel, not a Cursor-INITIATED
#    notification one — registered a second test hook that always
#    returned "ask" for beforeShellExecution alongside this one; the
#    resulting denied command reached THIS script's own
#    beforeShellExecution/PreToolUse/PostToolUse events completely
#    unmarked (PostToolUse's tool_output even reported
#    {"output":"","exitCode":0}, indistinguishable from a real
#    successful no-output command) — nothing in the payload this
#    script receives ever indicates another hook is asking, has asked,
#    or denied. A hook can only ever *cause* an ask by returning one
#    itself; it has no visibility into anyone else's. This confirms
#    rather than closes the gap above. It isn't silent in practice,
#    though: a real pending prompt (tested live, an actual `sleep`
#    command gated behind a project hook) leaves that PreToolUse with
#    no matching PostToolUse — exactly what daemon.py's
#    STALL_THRESHOLD_SECONDS backstop already exists to catch (built
#    for Claude Code's unreliable Notification:permission_prompt).
#    Confirmed on real hardware: the pad slot escalated to tool_stalled
#    (blinking purple) ~11s in, no Cursor-specific code involved.
#
# Every point above is now confirmed live per the banner, except the
# one narrow gap called out in #3 (MCP events) that genuinely couldn't
# be exercised in this environment — that one still gets the same
# treatment the README gives the Keychron K1 Pro keymap: documented
# best-effort, not confirmed working.
#
# Fields injected/derived, same "enrich, don't strip" approach as the
# other three adapters — Cursor's own fields (tool_input, tool_output,
# model, ...) are left untouched in the forwarded payload:
#
# 1. session_id — the payload's own session_id if present, else its
#    conversation_id (see #2 above).
# 2. tool_name — Cursor's own field, but absent on the Shell/MCP/file
#    "before*/after*" event pairs (they carry `command`/`file_path`
#    instead) — TOOL_NAME_FALLBACK below fills in a synthetic name
#    ("Shell", "Read", "Edit") for exactly those events, purely so the
#    pad's slot label shows something meaningful during PreToolUse.
# 3. cwd — Cursor's sessionStart payload doesn't document a cwd field
#    at all; falls back to the first entry of workspace_roots (which
#    IS documented as a base field on every event) so the pad's slot
#    label isn't just session_id's first 8 characters.
# 4. tmux_pane — from this script's own $TMUX_PANE.
# 5. controlling_tty — sessionStart only. Same rationale as the other
#    three adapters (the hook subprocess itself has no controlling
#    terminal, but some ancestor does), but Cursor needs one hop
#    further up than they do: a live process-ancestry capture
#    (2026-08-23, real Terminal.app session) showed $PPID here is a
#    zsh "sandbox env restore" wrapper Cursor interposes with no tty
#    of its own (`ps -o tty=` -> "??"); its PARENT — the actual `agent`
#    binary — is what's really attached to the terminal. Claude Code's
#    and Codex's adapters only need `ps -o tty= -p "$PPID"` directly
#    because their hook subprocess's immediate parent already has the
#    tty (see claude/hook.sh's comments) — Cursor's extra wrapper layer
#    means the same one-hop check finds nothing. Walks up to 5 ancestors
#    (matching the depth actually observed: wrapper -> agent -> user's
#    shell -> login -> Terminal.app) rather than hardcoding "always
#    exactly 2 hops", in case a future Cursor version adds or removes a
#    layer.
# 6. agent — always "cursor". Bookkeeping only (see daemon.py's
#    session_agents) — doesn't affect state mapping.
#
# -c is required on jq: its default output is pretty-printed across
# multiple lines, which breaks the newline-delimited-JSON protocol
# daemon.py relies on (one full object per line).
#
# Fails open on purpose: if the daemon isn't running, or the socket
# write times out, this must never block a tool call or a turn. nc's
# -w1 caps the connection attempt at 1s, and the ps call above is
# skipped entirely except at sessionStart.

SOCKET="$HOME/.ai-agent-macropad/daemon.sock"

input=$(cat)
event=$(printf '%s' "$input" | jq -r '.hook_event_name // empty')

ctty=""
if [ "$event" = "sessionStart" ]; then
  p="$PPID"
  for _ in 1 2 3 4 5; do
    [ -z "$p" ] && break
    tty=$(ps -o tty= -p "$p" 2>/dev/null | tr -d ' ')
    if [ -n "$tty" ] && [ "$tty" != "??" ]; then
      ctty="/dev/$tty"
      break
    fi
    p=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
  done
fi

EVENT_MAP='{
  "sessionStart": "SessionStart",
  "sessionEnd": "SessionEnd",
  "beforeSubmitPrompt": "UserPromptSubmit",
  "preToolUse": "PreToolUse",
  "postToolUse": "PostToolUse",
  "postToolUseFailure": "PostToolUseFailure",
  "beforeShellExecution": "PreToolUse",
  "afterShellExecution": "PostToolUse",
  "beforeMCPExecution": "PreToolUse",
  "afterMCPExecution": "PostToolUse",
  "beforeReadFile": "PreToolUse",
  "afterFileEdit": "PostToolUse",
  "stop": "Stop",
  "subagentStart": "SubagentStart",
  "subagentStop": "SubagentStop",
  "preCompact": "PreCompact"
}'

TOOL_NAME_FALLBACK='{
  "beforeShellExecution": "Shell",
  "afterShellExecution": "Shell",
  "beforeReadFile": "Read",
  "afterFileEdit": "Edit"
}'

printf '%s' "$input" | jq -c \
  --argjson events "$EVENT_MAP" \
  --argjson toolFallback "$TOOL_NAME_FALLBACK" \
  --arg pane "$TMUX_PANE" \
  --arg ctty "$ctty" \
  '.session_id = (.session_id // .conversation_id)
   | .tool_name = (.tool_name // $toolFallback[.hook_event_name])
   | .cwd = (.cwd // (.workspace_roots[0] // ""))
   | .hook_event_name = ($events[.hook_event_name] // .hook_event_name)
   | .tmux_pane = $pane
   | .agent = "cursor"
   | (if $ctty != "" then .controlling_tty = $ctty else . end)' \
  | nc -U -w1 "$SOCKET" >/dev/null 2>&1

exit 0
