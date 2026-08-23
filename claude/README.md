# Claude Code hooks

Part of [ai-agent-macropad](../README.md) — see the main README for
hardware setup, running the daemon, and the shared wire protocol. This
page covers wiring up Claude Code specifically.

**Prerequisites:** the daemon's config directory must exist
(`mkdir -p "$HOME/.ai-agent-macropad"` — see
[Setup](../README.md#4-wire-up-hooks)), and `jq` plus a `nc` build that
supports Unix-domain sockets (`-U`, e.g. macOS's built-in `nc`) must be
on `PATH`.

1. Copy the script and make it executable:

   ```
   cp claude/hook.sh "$HOME/.ai-agent-macropad/hook-claude.sh"
   chmod +x "$HOME/.ai-agent-macropad/hook-claude.sh"
   ```

2. Merge the `"hooks"` block from
   [`example_hook_settings.json`](example_hook_settings.json)
   into your Claude Code `settings.json` (global `~/.claude/settings.json`
   or a project's `.claude/settings.json`). It registers
   `$HOME/.ai-agent-macropad/hook-claude.sh` as a command hook for every
   event `handle_hook_event()` cares about (`SessionStart`,
   `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `PostToolUse`,
   `PostToolUseFailure`, `Notification`, `Stop`, `SubagentStop`,
   `SessionEnd`). The `Notification` entries split on `matcher`
   (`agent_needs_input` vs. `idle_prompt`) and pass
   `MACROPAD_NOTIFICATION_TYPE` as an env var, since that's the reliable
   way to know which subtype fired for a given invocation
   (`Notification:permission_prompt` itself is not wired up — see
   [`hook_to_state`'s docstring](../daemon.py) for why).

[`hook.sh`](hook.sh) reads the hook's JSON payload from
stdin (Claude Code already includes `hook_event_name` and `session_id`
in it) and forwards it to `~/.ai-agent-macropad/daemon.sock` via `nc -U`,
after using `jq` to fill in a few fields the payload doesn't reliably
carry on its own:

- `agent`, always `"claude-code"` — bookkeeping only (see
  `Daemon.session_agents` in `daemon.py`), doesn't affect state mapping.
- `notification_type`, from the `MACROPAD_NOTIFICATION_TYPE` env var set
  by the matcher branch in `settings.json`.
- `tmux_pane`, from the script's own `$TMUX_PANE` (empty if not running
  inside tmux).
- `controlling_tty`, at `SessionStart` only: Claude Code runs hook
  commands detached with no controlling terminal, so the script instead
  reads the _parent_ process's tty via `ps -o tty= -p "$PPID"` — the
  process Claude Code actually spawned still has one.

It fails open by design (redirects `nc`'s output away, always exits 0)
so a daemon that isn't running never blocks a tool call or a session,
and caps the socket write at one second so `PreToolUse`/`PostToolUse` —
which fire on every tool call — stay fast.
