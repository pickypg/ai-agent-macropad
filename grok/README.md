# Grok Build hooks

Part of [ai-agent-macropad](../README.md) — see the main README for
hardware setup, running the daemon, and the shared wire protocol. This
page covers wiring up Grok Build specifically.

**Prerequisites:** the daemon's config directory must exist
(`mkdir -p "$HOME/.ai-agent-macropad"` — see
[Setup](../README.md#4-wire-up-hooks)), and `jq` plus a `nc` build that
supports Unix-domain sockets (`-U`, e.g. macOS's built-in `nc`) must be
on `PATH`.

Grok Build's own hooks system (documented at
[docs.x.ai/build](https://docs.x.ai/build/features/hooks)) discovers
hooks from `~/.grok/hooks/*.json` directly — no merging into an
existing config file needed, and global hooks there are always
trusted, no per-project trust prompt to deal with (unlike Codex).
Its hook *payloads*, however, diverge from Claude Code's and Codex's
more than either of those diverge from each other: every field is
camelCase (`hookEventName`, `sessionId`, `toolName`, ...), and —
confirmed against a real `grok -p ...` run, contradicting what Grok
Build's own docs imply — `hookEventName`'s *value* is snake_case
(`"pre_tool_use"`, `"post_tool_use"`, `"stop"`, ...) rather than the
PascalCase (`"PreToolUse"`, ...) its hooks reference uses for
hook-file matching. [`hook.sh`](hook.sh) translates both the
field names and the event-name values before forwarding — see its own
comments for the full list of what it translates and why.

1. Copy the script and make it executable:

   ```
   cp grok/hook.sh "$HOME/.ai-agent-macropad/hook-grok.sh"
   chmod +x "$HOME/.ai-agent-macropad/hook-grok.sh"
   ```

2. Copy [`example_hooks.json`](example_hooks.json) into
   Grok Build's own global hooks directory:

   ```
   mkdir -p "$HOME/.grok/hooks"
   cp grok/example_hooks.json "$HOME/.grok/hooks/ai-agent-macropad.json"
   ```

   It wires every event `handle_hook_event()` cares about
   (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
   `PostToolUseFailure`, `PermissionDenied`, `Notification`, `Stop`,
   `StopFailure`, `StopCancelled`, `SubagentStart`, `SubagentStop`,
   `PreCompact`, `PostCompact`, `SessionEnd`) to
   `$HOME/.ai-agent-macropad/hook-grok.sh` — several of these
   (`PermissionDenied`, the `Subagent*`/`*Compact` pair) currently map
   to no state change, wired up anyway so a future `hook_to_state()`
   change doesn't also require re-editing this file, same reasoning as
   Claude Code's own `SubagentStop` entry.

3. Reload hooks in any already-running Grok Build session (`r` in the
   Hooks tab, opened via `Ctrl+L` or `/hooks`), or just start a new one
   — no restart or trust step needed for a global hook.

Grok Build also reads `~/.claude/settings.json` by default, for
compatibility with Claude Code's own hook files (see its [Hooks
docs](https://docs.x.ai/build/features/hooks#hook-locations)) — so if
you already have `claude/hook.sh` wired up on this machine (see
[Claude Code hooks](../claude/README.md)), a Grok Build session will
*also* invoke it for every event,
confirmed live, passing Grok's own camelCase envelope
(`hookEventName`/`sessionId`, not Claude Code's snake_case). Since
`claude/hook.sh` requires *both* `hook_event_name` and `session_id` to
be present before it forwards anything — every genuine Claude Code
event includes both — it recognizes a Grok-shaped payload as foreign
and drops it immediately, without ever touching the daemon's socket
or the daemon's logs. It's still an extra process spawned per event
for no benefit, though. To stop that, add to `~/.grok/config.toml`:

```toml
[compat.claude]
hooks = false
```

Gaps versus Claude Code, confirmed live during development (see
`hook_to_state`'s docstring in `daemon.py` for the full reasoning):

- Grok Build's docs list `PostToolUseFailure` as a distinct event, but
  in practice — tried both a nonzero shell exit code and a
  nonexistent-file read — an ordinary tool failure still comes back as
  a plain `PostToolUse` (state stays `working`, never `error`), the
  same practical gap as Codex. It's still translated and mapped to
  `error` in case a genuine infra-level failure does use it.
- `PermissionDenied` (Grok Build's rule-based auto-deny, e.g. via
  `--deny`) fires *after* the decision is already made, with nothing
  left pending — it deliberately maps to no state change, unlike
  Claude Code/Codex's `PermissionRequest`. The actual "a permission UI
  is waiting on you" signal for Grok Build is
  `Notification:permission_prompt`, confirmed live and — per Grok
  Build's own docs — reliable, unlike Claude Code's version of that
  same subtype (see [Pad states](../README.md#pad-states)).
- `StopFailure` and `StopCancelled` have no Claude Code/Codex
  equivalent (mapped to `error` and `done` respectively) — see
  [Pad states](../README.md#pad-states).
