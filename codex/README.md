# Codex CLI hooks

Part of [ai-agent-macropad](../README.md) — see the main README for
hardware setup, running the daemon, and the shared wire protocol. This
page covers wiring up Codex CLI specifically.

**Prerequisites:** the daemon's config directory must exist
(`mkdir -p "$HOME/.ai-agent-macropad"` — see
[Setup](../README.md#4-wire-up-hooks)), and `jq` plus a `nc` build that
supports Unix-domain sockets (`-U`, e.g. macOS's built-in `nc`) must be
on `PATH`.

Codex CLI's own hooks system (distinct from its older, more limited
`notify` config key) turns out to use almost the exact same event
vocabulary as Claude Code's — same event names (`SessionStart`,
`UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `PostToolUse`,
`Stop`, `SubagentStop`, `SessionEnd`), same delivery (JSON on stdin),
and even the same `hooks.json` schema shape — so `codex/hook.sh` is
almost identical to `claude/hook.sh`; see its comments for the couple
of places they diverge, and [`hook_to_state`'s
docstring](../daemon.py) for what that means for pad states.

1. Copy the script and make it executable:

   ```
   cp codex/hook.sh "$HOME/.ai-agent-macropad/hook-codex.sh"
   chmod +x "$HOME/.ai-agent-macropad/hook-codex.sh"
   ```

2. Wire up the `"hooks"` block from
   [`example_hooks.json`](example_hooks.json), which points
   every relevant event at `$HOME/.ai-agent-macropad/hook-codex.sh`.
   Codex discovers hooks from `~/.codex/hooks.json`,
   `~/.codex/config.toml`'s inline `[hooks]` tables, or the equivalent
   pair inside a project's own `.codex/` — see [Codex's hooks
   reference](https://developers.openai.com/codex/hooks) for the exact
   `hooks.json` vs. `config.toml` syntax. **What was actually verified
   working here** (real `codex exec` run, `codex-cli 0.149.0`) was the
   global `~/.codex/config.toml` inline-table form — a project-local
   `.codex/hooks.json` did not fire in that same version despite
   matching Codex's documented format, so if hooks silently don't fire
   for you, try `config.toml` before assuming something else is wrong.

3. **New hooks need to be trusted before Codex will run them** — the
   first time, an interactive `codex` session prompts you to review and
   trust them. For non-interactive use (`codex exec`, scripts, CI),
   pass `--dangerously-bypass-hook-trust` instead — as the name warns,
   only do this for hook sources you already trust (i.e. `codex/hook.sh`
   as shipped in this repo, not an arbitrary command).

Known gaps versus Claude Code (see [`hook_to_state`'s docstring](../daemon.py)
for the full reasoning): Codex has no `PostToolUseFailure`
event, so a failed Codex tool call still reports as a normal
`PostToolUse` (state stays `working`, never `error`) — there's no
reliable field in that payload to tell success from failure apart.
Codex also has no `Notification` event, so the `waiting` (idle 60s+)
state never fires for a Codex session — only Claude Code has an
equivalent. Everything else, including `PermissionRequest` -> `question`,
works the same as Claude Code.
