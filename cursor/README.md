# Cursor CLI hooks

Part of [ai-agent-macropad](../README.md) — see the main README for
hardware setup, running the daemon, and the shared wire protocol. This
page covers wiring up Cursor CLI specifically, plus a fairly detailed
verification log — Cursor's own docs and community reports disagreed
with each other (and with live testing) on several points that
mattered here, so this page tracks what was actually confirmed and how.

**Prerequisites:** the daemon's config directory must exist
(`mkdir -p "$HOME/.ai-agent-macropad"` — see
[Setup](../README.md#4-wire-up-hooks)), and `jq` plus a `nc` build that
supports Unix-domain sockets (`-U`, e.g. macOS's built-in `nc`) must be
on `PATH`.

**Verified end to end (2026-08-23)** against real interactive `agent`
sessions and `agent -p` runs — session lifecycle, session correlation,
key-press dispatch, and nearly every documented event. The one
remaining gap is MCP events (untestable here, no MCP server available)
— see [`hook.sh`](hook.sh)'s own comments for the full reasoning,
summarized below.

## Setup

1. Copy the script and make it executable:

   ```
   cp cursor/hook.sh "$HOME/.ai-agent-macropad/hook-cursor.sh"
   chmod +x "$HOME/.ai-agent-macropad/hook-cursor.sh"
   ```

2. Copy [`example_hooks.json`](example_hooks.json) into
   Cursor's own hooks config — `~/.cursor/hooks.json` for a user-level
   hook (applies to every project), or `<project>/.cursor/hooks.json`
   for one project only:

   ```
   mkdir -p "$HOME/.cursor"
   cp cursor/example_hooks.json "$HOME/.cursor/hooks.json"
   ```

   It wires every plausible event to `$HOME/.ai-agent-macropad/hook-cursor.sh`:
   `sessionStart`, `sessionEnd`, `beforeSubmitPrompt`, `preToolUse`,
   `postToolUse`, `postToolUseFailure`, `beforeShellExecution`,
   `afterShellExecution`, `beforeMCPExecution`, `afterMCPExecution`,
   `beforeReadFile`, `afterFileEdit`, `stop`, `subagentStart`,
   `subagentStop`, `preCompact`. If you already have a `hooks.json`
   with other hooks configured, merge the `"hooks"` block instead of
   overwriting the file.

## Cursor CLI also fires Claude Code's hooks

**Confirmed live, and NOT opt-in the way it appeared from the docs.**
Cursor's own docs describe loading `~/.claude/settings.json` for
"third-party" compatibility as gated behind Cursor Settings → Rules,
Skills, Subagents → *Include third-party Plugins, Skills, and other
configs* — but that setting lives in the IDE's UI, and the standalone
CLI has no such panel. Live testing on 2026-08-23 confirmed the CLI
fires real Claude Code hooks regardless: the same `postToolUse` event
showed up twice in `~/.ai-agent-macropad/events.log` for the same
`session_id`, once via `claude/hook.sh` (mistagged `agent=claude-code`,
event name left as Cursor's own untranslated `"postToolUse"`) and once
via this script (correctly `agent=cursor`, translated to
`"PostToolUse"`). Unlike Grok Build's version of this same problem
(different field *names* entirely, already guarded against), Cursor's
compat payload uses Claude Code's real field *names* (`hook_event_name`,
`session_id`) with Cursor's own camelCase *values* —
[`../claude/hook.sh`](../claude/hook.sh) now also rejects any
`hook_event_name` value that isn't PascalCase for exactly this reason.

That guard itself had a second, subtler bug, also confirmed live the
same day: its `case "$event" in [A-Z]*)` check worked correctly when
tested manually (a normal interactive shell's `C.UTF-8` locale), but
silently passed *everything* — including Cursor's own lowercase-first
values — when Cursor's **interactive** `agent` REPL invoked it (its
`-p`/print/non-interactive mode never reproduced this). Root cause:
`[A-Z]` in a shell `case`/glob pattern is a **locale-collated bracket
expression**, not a hardcoded ASCII range — under `LC_ALL=en_US.UTF-8`
(reproduced directly, matching whatever locale Cursor's interactive
hook subprocess is spawned with), `[A-Z]*` wrongly matches
lowercase-first strings too, silently defeating the whole guard. Fixed
by exporting `LC_ALL=C` immediately before that one comparison — a
plain (non-exported) shell variable assignment is not sufficient, since
locale-sensitive collation reads the real process environment. If you
already have `claude/hook.sh` wired up from a real Claude Code setup,
**update it** — both bugs together meant every Cursor interactive
session was silently polluting Claude Code's slots with mistagged,
untranslated duplicates. **If you have Claude Code hooks configured at
all**, wiring up Cursor will exercise this path — worth confirming
your `claude/hook.sh` copy is current before relying on either.

## Event coverage — now mostly confirmed live (2026-08-23)

Community reports (Cursor's own forum, mid-2026) warned the standalone
CLI might fire only a subset of its documented events in practice.
That wasn't borne out here: `sessionStart`, `sessionEnd`,
`beforeSubmitPrompt`, `preToolUse`, `postToolUse`, `postToolUseFailure`,
`beforeShellExecution`/`afterShellExecution`, `beforeReadFile`, and
`afterFileEdit` (real `edits: [{old_string, new_string}]` shape) all
fired exactly as documented, across both real interactive sessions and
`agent -p` runs. One pleasant surprise: `postToolUseFailure` fires
*reliably* for Cursor — unlike Claude Code, Codex, and Grok Build,
where the equivalent event rarely fires in practice (see their own
setup pages).

One thing remains genuinely unconfirmed: **`beforeMCPExecution`/
`afterMCPExecution`** — no MCP server was available to test against.
Stays wired in `hook.sh` anyway (harmless no-op if it never
fires, same posture as Grok Build's `PreCompact`/`PostCompact`).

**Two confirmed real gaps, both deliberately tested rather than
assumed:**

- **`subagentStart`/`subagentStop` never fire.** A real subagent
  delegation (`agent -p ... "use a subagent to..."`, confirmed live)
  surfaces only as an ordinary `PreToolUse`/`PostToolUse` pair for a
  `Task` tool (`tool_input.subagent_type`), not as either of the
  documented dedicated events — still fully handled by the existing
  generic-tool-call mapping, just not distinguishable from any other
  tool call on the pad.
- **`preCompact` never fires either — even for a genuine, manually
  triggered compaction.** A plain `/compact` prompt visibly condensed
  the session's context into a real structured summary (it correctly
  referenced prior turns), but no `PreCompact` event reached
  `hook.sh` at all. Auto-triggered compaction (hitting the
  context-window threshold organically) is still untested — impractical
  to force deliberately — but the deliberate-trigger path now has real
  negative evidence too, not just an untested assumption.

## No permission-prompt/notification hook

**Deliberately re-tested, confirmed rather than closed.** Cursor
*does* have a permission-ask mechanism: a `preToolUse`/
`beforeShellExecution` hook's own stdout response can include
`{"permission": "ask", ...}` to force Cursor's approval UI (used, for
example, by a project-local hook that gates network commands). But
live testing (2026-08-23 — registering a second test hook that always
asked, alongside this script) confirmed this is a hook-*initiated*
control channel, not a Cursor-*initiated* notification one: the denied
command reached this script's own `beforeShellExecution`/`PreToolUse`/
`PostToolUse` events completely unmarked — `PostToolUse`'s
`tool_output` even reported `{"output":"","exitCode":0}`,
indistinguishable from a real successful no-output command. A hook can
only ever *cause* an ask by returning one itself; it has no visibility
into anyone else's (another hook's, or Cursor's own native approval
logic's) decision. So `question` and `waiting` (see [Pad
states](../README.md#pad-states)) genuinely never fire for a Cursor
session today — a bigger gap than even Codex's — confirmed live, not
just undocumented.

**But it isn't silent in practice.** A real permission prompt (tested
live 2026-08-23, an actual `sleep` command gated behind a project hook
asking for approval in the interactive UI) leaves that slot's
`PreToolUse` with no matching `PostToolUse` — exactly the case
`STALL_THRESHOLD_SECONDS`'s stall-detection backstop (see
[Protocol](../README.md#protocol)) already exists to catch, originally
built for Claude Code's unreliable `Notification:permission_prompt`.
It fired correctly, unmodified, with no Cursor-specific code involved:
the pad slot escalated to `tool_stalled` (blinking purple) ~11 seconds
in, confirmed on real hardware. Not as sharp as a dedicated `question`
state — it can't tell "waiting on you" apart from "just a slow
command" — but a pending Cursor permission prompt does visibly surface
on the pad within about 10 seconds, via a mechanism that already
existed for exactly this class of problem.

## Session correlation

Previously an open question, now resolved: live testing showed a real
`session_id` field present on a `postToolUse` event (not just
`sessionStart`/`sessionEnd`, contradicting Cursor's own docs) —
`hook.sh` now prefers that field when present, falling back to
`conversation_id` only if it's ever actually absent. Confirmed working
end to end (2026-08-23): session lifecycle allocates and frees a pad
slot correctly across a real interactive `agent` session, with no more
mistagged Claude Code duplicates once the `LC_ALL=C` fix above was
deployed.

## Key-press dispatch (bring window to front)

This needed a fix too. `controlling_tty` capture (see
[`hook.sh`](hook.sh)'s own comments) came back empty for every real
session at first — a live process-ancestry capture showed Cursor
interposes one more layer than Claude Code/Codex do before reaching a
tty-attached process: the hook subprocess's immediate parent is a zsh
"sandbox env restore" wrapper with no tty of its own, and it's *that*
wrapper's parent (the real `agent` binary) that's actually attached to
the terminal. Fixed by walking up to 5 ancestors looking for the first
real tty, instead of checking only `$PPID` directly like the other
three adapters can. Confirmed end to end (2026-08-23): pressing a pad
key for a real interactive Cursor session correctly raises its
Terminal.app window.

---

If you wire this up against a real `agent` session, please note what
actually fires (and in what shape) so `hook.sh` and this page can be
corrected from observation rather than documentation.
