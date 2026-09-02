#!/usr/bin/env python3
"""
AI Agent Macropad daemon.

Listens for hook events on a Unix socket, maintains the
session_id -> pad slot mapping, and mirrors state to a QMK-based pad
over USB HID (e.g. a NuPhy Air75 V2 or Keychron K1 Pro) — see
pad_link.HidPadLink. Also reads key/encoder events back from the pad
and dispatches a key press to bring that session's window to the
front — see Daemon.dispatch_bring_to_front().

Usage:
    python3 daemon.py

The daemon auto-detects the pad (pad_link.discover_hid_pad(), tried
against every board in hid_protocol.KNOWN_HID_PADS), attaching to
whichever one answers a ping/hello handshake first. Runs headless (no
crash, just logs what it *would* send) if no pad answers, so you can
develop the socket/slot-mapping logic before any hardware is plugged
in.

The wire protocol (one JSON object per line on the Unix socket — see
handle_hook_event() below) isn't tied to any one agent: any hook/
notification source that speaks it can drive a slot, as long as its
payload carries at least hook_event_name and session_id. Four
adapters ship in this repo today, one per agent's own hook system —
claude/hook.sh (Claude Code), codex/hook.sh (Codex CLI), grok/hook.sh
(Grok Build), and cursor/hook.sh (Cursor CLI, unverified — see its own
comments) — each translating its agent's native hook JSON into this
shape and forwarding it here; see the "Agents tested" table in the
README for which agents actually have one. Each adapter tags its
payload with an "agent" field (see Daemon.session_agents below) purely
for logging/bookkeeping — it has no effect on how an event maps to a
pad state. Claude Code's and Codex's hook event vocabularies overlap
almost entirely (same event names, same core fields); Grok Build's
overlaps too, but arrives in a different shape (camelCase fields,
snake_case event-name values) that grok/hook.sh translates before
forwarding — see its own comments. Cursor's own event names are
camelCase values ("sessionStart", "preToolUse", ...) that cursor/
hook.sh translates to this same PascalCase vocabulary, plus a handful
of Shell/MCP/file-specific event pairs (beforeShellExecution/
afterShellExecution, ...) folded into the generic PreToolUse/
PostToolUse pair — see cursor/hook.sh's own comments for why, and for
the real uncertainty around which of Cursor's documented events an
actual `agent` (Cursor CLI) session sends in practice. hook_to_state()
below is one shared mapping table across all four rather than forked
per agent; see its docstring for the handful of places they diverge.

The HID handle is only held open while at least one agent session is
active — see Daemon._reconcile_pad(). It's released
IDLE_CLOSE_GRACE_SECONDS after the last session ends (and shortly
after startup, if the daemon starts with none running), and reacquired
lazily on the next SessionStart. This matters because the VIA app
needs exclusive access to the same raw HID interface this daemon
uses — VIA can't connect while the daemon holds it open, so releasing
it whenever there's nothing to display or dispatch a keypress to lets
VIA be used without having to manually stop the daemon first.

Requires pip install hid, plus the native hidapi library (e.g. brew
install hidapi on macOS) — optional: daemon.py runs fine without
either installed, it just can't attach to a pad.

At startup, before the socket starts accepting hook events, the
daemon also seeds slots for any Claude Code or Grok Build sessions
that were already running (e.g. a daemon restart mid-session) — via
`claude agents --json` for the former, Grok Build's own
active_sessions.json registry file for the latter — see
discover_running_sessions(), discover_running_grok_sessions(), and
Daemon.seed_existing_sessions() below. Each is independently
best-effort: an older Claude Code (<2.1.224), no `claude` on PATH, no
Grok Build ever installed/run, or any parse failure for either just
means that source seeds nothing, and its pre-existing sessions fall
back to the lazy-allocation-on-first-hook-event behavior instead.
Codex has neither mechanism, so its pre-existing sessions always start
out via the lazy-allocation fallback in handle_hook_event() instead,
same as any hook event that arrives before this daemon ever saw that
session's SessionStart.
"""
import asyncio
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import hid_protocol
from pad_link import HidPadLink

# --- Config ------------------------------------------------------------

# Shared with every agent's hook adapter (claude/hook.sh, codex/hook.sh
# — each drops its own copy here, see README's "Wire up hooks" steps)
# so everything the daemon owns on disk lives under one directory
# instead of scattered directly in the home directory. Created eagerly
# since a standalone daemon run (e.g. via fake_hooks.py, before any
# hook script has been copied anywhere) is the first thing to need it,
# both for the socket bind below and for the events-log handler
# created at import time further down.
CONFIG_DIR = Path(os.path.expanduser("~/.ai-agent-macropad"))
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

SOCKET_PATH = str(CONFIG_DIR / "daemon.sock")
NUM_SLOTS = 12
EVENTS_LOG_PATH = str(CONFIG_DIR / "events.log")

# asyncio's StreamReader defaults to a 64KiB readline() limit. hook.sh
# forwards whole hook payloads (e.g. PostToolUse for Read/Grep/Bash,
# which embeds tool_response) as a single line, and those routinely
# exceed 64KiB for a large file or command output, so the default
# blows up handle_connection with LimitOverrunError. 8MiB comfortably
# covers even large tool outputs without letting one runaway line
# stall the daemon.
SOCKET_READ_LIMIT = 8 * 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("macropad-daemon")

# Separate logger + rotating file, deliberately independent of the
# console logger above. Purpose: capture the raw bytes of every line
# that hits the socket — valid JSON or not — so a framing/parsing bug
# like the jq-pretty-print issue can be diagnosed from disk after the
# fact instead of requiring you to reproduce it live with the daemon
# open in front of you. propagate=False keeps this out of the console
# log so the two don't duplicate each other.
events_log = logging.getLogger("macropad-events")
events_log.propagate = False
events_log.setLevel(logging.INFO)
_events_handler = logging.handlers.RotatingFileHandler(
    EVENTS_LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3
)
_events_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
events_log.addHandler(_events_handler)


# --- Hook event -> pad state mapping (build brief §5) --------------------

# Claude-Code-specific tool names for its two "blocked on a choice,
# not just running a tool" builtins (plan-mode exit, the AskUserQuestion
# tool). Codex has no matching tool names — a Codex tool call needing
# attention still surfaces as "question" via PermissionRequest below,
# it just won't get this earlier, more specific escalation.
ATTENTION_TOOLS = {"AskUserQuestion", "ExitPlanMode"}


def hook_to_state(event_name, tool_name=None, notification_type=None):
    """Pad state a given hook event maps to, or None if this hook
    doesn't change display state by itself (e.g. SubagentStop).

    Deliberately agent-agnostic: Claude Code and Codex CLI hooks both
    use this same event vocabulary (SessionStart, UserPromptSubmit,
    PreToolUse, PermissionRequest, PostToolUse, Stop, SubagentStop,
    SessionEnd — confirmed against Codex's own hooks reference), so one
    mapping table serves both rather than forking per agent. Two gaps
    where they diverge, neither of which needs special-casing here —
    they just never fire for Codex, and the pad quietly stays at
    whatever state it was already in:
      - PostToolUseFailure doesn't exist for Codex; a failed Codex tool
        call is reported as a normal PostToolUse instead, so it maps to
        "working" rather than "error" (no reliable field to tell success
        from failure apart in that payload).
      - Notification (and its agent_needs_input/idle_prompt subtypes,
        Claude Code's own extension) has no Codex equivalent — Codex
        tool calls needing your input surface only via PermissionRequest.

    Cursor (translated by cursor/hook.sh from its own camelCase event
    names — see its own comments) has neither PermissionRequest nor
    Notification: no hook in Cursor's published reference signals "a
    permission UI is waiting on you" at all, so "question" and
    "waiting" never fire for a Cursor session today — a bigger gap
    than even Codex's.

    AskUserQuestion and ExitPlanMode are special-cased: both are
    PreToolUse events, but both mean Claude is blocked on the user
    making a choice, not just "running a tool" — worth a visually
    distinct (and blinking, on the pad side) state. Claude-Code-only —
    see ATTENTION_TOOLS above.

    Notification is also special-cased by subtype: agent_needs_input
    means Claude is fully stalled until you answer, same urgency as
    AskUserQuestion, so it maps to "question" too. idle_prompt
    (Claude's been idle 60s+) is lower-stakes and stays "waiting".
    permission_prompt was long deliberately NOT handled here for Claude
    Code — confirmed unreliable in practice there (never fired for a
    real "Allow this command?" prompt, even on a current Claude Code
    version) — PermissionRequest, handled separately below, was the
    working replacement for that case. It's handled now because Grok
    Build (confirmed live, and per its own hooks reference) fires
    Notification:permission_prompt reliably, and only when a permission
    UI is genuinely waiting on you, unlike Claude Code's version of the
    same subtype — harmless to also enable for Claude Code since it
    simply may never fire there.

    Grok Build's own three turn-end events — Stop, StopFailure,
    StopCancelled, confirmed live and cross-checked against its hooks
    reference — have no Claude Code/Codex equivalent (those two only
    ever send Stop): StopFailure (a turn ended on an API error) maps to
    "error" like PostToolUseFailure; StopCancelled (a turn ended
    without completing — user interrupt, declined permission, hit
    --max-turns, ...) maps to "done" like a normal Stop, since either
    way the agent isn't actively working anymore. Grok Build also sends
    a Stop with reason "channel_closed"/"shutdown" at session teardown,
    strictly after SessionEnd already freed the slot (confirmed live) —
    grok/hook.sh drops that one before it ever reaches this function
    (see its own comments) rather than teaching this shared function
    about a "reason" field no other agent sends.
    """
    if event_name == "PreToolUse" and tool_name in ATTENTION_TOOLS:
        return "question"

    if event_name == "PermissionRequest":
        # More literal match for "the interactive approval dialog is
        # showing" than Notification:permission_prompt — worth testing
        # as the primary signal, since Notification's reliability is
        # unclear even on a current Claude Code version.
        return "question"

    if event_name == "Notification":
        if notification_type in ("agent_needs_input", "permission_prompt"):
            return "question"
        if notification_type == "idle_prompt":
            return "waiting"
        return None  # other subtypes (auth_success, elicitation_*, task_complete, ...) — no state change

    return {
        "SessionStart": "idle",
        "UserPromptSubmit": "working",
        "PreToolUse": "tool_running",
        "PostToolUse": "working",
        "PostToolUseFailure": "error",
        "Stop": "done",
        "StopFailure": "error",
        "StopCancelled": "done",
    }.get(event_name)


# --- Slot allocation -------------------------------------------------------

class SlotManager:
    """Maps Claude Code session_id -> pad slot index (0..num_slots-1).

    Deliberately naive for Phase 3: first-fit allocation, no eviction.
    §9's open question (LRU eviction vs. OLED paging past num_slots
    concurrent sessions) is intentionally left unanswered here.
    """

    def __init__(self, num_slots):
        self.num_slots = num_slots
        self.session_to_slot = {}
        self.slot_to_session = [None] * num_slots
        self.lock = threading.Lock()

    def allocate(self, session_id):
        with self.lock:
            if session_id in self.session_to_slot:
                return self.session_to_slot[session_id]
            for i in range(self.num_slots):
                if self.slot_to_session[i] is None:
                    self.slot_to_session[i] = session_id
                    self.session_to_slot[session_id] = i
                    return i
            log.warning(
                "no free slots for session %s (>%d concurrent)",
                session_id, self.num_slots,
            )
            return None

    def free(self, session_id):
        with self.lock:
            i = self.session_to_slot.pop(session_id, None)
            if i is not None:
                self.slot_to_session[i] = None
            return i

    def evict(self, index):
        """Index-keyed mirror of free() — clears whatever session
        currently occupies `index` (if any) and returns its session_id,
        or None if the slot was already empty. Used for manual eviction
        (hold-to-clear a key), where the daemon knows the slot the user
        acted on but not, up front, which session_id occupies it.
        """
        with self.lock:
            session_id = self.slot_to_session[index]
            if session_id is not None:
                self.slot_to_session[index] = None
                self.session_to_slot.pop(session_id, None)
            return session_id

    def slot_for(self, session_id):
        with self.lock:
            return self.session_to_slot.get(session_id)


# --- Existing session discovery -----------------------------------------
#
# A restarted daemon (or one started after some agent's sessions are
# already open) otherwise has no slots until each pre-existing session
# happens to fire some hook event — see the lazy-allocation fallback
# in handle_hook_event, added for exactly this case. Two agents have a
# way to discover this at startup, each via a completely different
# mechanism:
#
# - Claude Code: `claude agents --json` (Claude Code >=2.1.224) prints
#   every currently-active session — interactive and background — as a
#   JSON array with at least pid/cwd/sessionId. Best-effort: an older
#   CLI, no `claude` on PATH, or any parse failure just means an empty
#   list, same headless-friendly posture as pad discovery elsewhere in
#   this file. See discover_running_sessions() below.
#
# - Grok Build: no CLI query needed — it maintains its own live
#   registry file, $GROK_HOME/active_sessions.json (default
#   ~/.grok/active_sessions.json), a JSON array of
#   {session_id, pid, cwd, opened_at} for every currently-open session
#   on this machine, confirmed live against a real running `grok`
#   process. Same best-effort posture: a missing file, bad JSON, or an
#   unexpected shape just mean an empty list. See
#   discover_running_grok_sessions() below.
#
# Codex has neither, so a pre-existing Codex session always just waits
# for its first real hook event, same as every other lazy-allocation
# path in this file.

def discover_running_sessions():
    try:
        result = subprocess.run(
            ["claude", "agents", "--json"],
            capture_output=True, timeout=5, text=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.info("could not query running Claude Code sessions: %s", e)
        return []
    if result.returncode != 0:
        log.info(
            "`claude agents --json` exited %d: %s",
            result.returncode, result.stderr.strip(),
        )
        return []
    try:
        sessions = json.loads(result.stdout)
    except ValueError:
        log.warning(
            "bad JSON from `claude agents --json`: %r", result.stdout[:200]
        )
        return []
    if not isinstance(sessions, list):
        log.warning("unexpected `claude agents --json` output shape: %r", sessions)
        return []
    return sessions


def _grok_active_sessions_path():
    # Same override Grok Build itself honors for its config directory
    # (see its headless-mode docs) — read from the right place if the
    # user has ever set this, rather than hardcoding ~/.grok.
    return Path(os.environ.get("GROK_HOME", os.path.expanduser("~/.grok"))) / "active_sessions.json"


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # e.g. PermissionError — process exists, just owned by someone
        # else, which still counts as alive for this check.
        return True
    return True


def discover_running_grok_sessions():
    """Grok Build's equivalent of discover_running_sessions() above,
    for seeding pre-existing Grok Build sessions at daemon startup —
    see Daemon.seed_existing_sessions(). No CLI query exists (or is
    needed): Grok Build maintains its own live registry file,
    $GROK_HOME/active_sessions.json (default
    ~/.grok/active_sessions.json), as a JSON array of
    {session_id, pid, cwd, opened_at} for every currently-open session
    on this machine — confirmed live against a real running `grok`
    process during development.

    Renames session_id -> sessionId so the shared allocation loop in
    seed_existing_sessions() can treat entries from either source
    identically without needing to know which one produced them.

    Best-effort, same posture as discover_running_sessions(): no file
    (Grok Build never installed, or never run), bad JSON, or an
    unexpected shape all just mean an empty list. Unlike `claude agents
    --json` — a live command that only ever reports genuinely running
    sessions — this is a plain file Grok Build's own processes
    maintain themselves, so a session that crashed or was kill -9'd
    before it could remove its own entry would otherwise linger here
    forever; entries are dropped if their pid isn't actually alive.
    """
    try:
        raw = _grok_active_sessions_path().read_text()
    except OSError:
        return []
    try:
        sessions = json.loads(raw)
    except ValueError:
        log.warning("bad JSON in %s: %r", _grok_active_sessions_path(), raw[:200])
        return []
    if not isinstance(sessions, list):
        log.warning("unexpected %s shape: %r", _grok_active_sessions_path(), sessions)
        return []
    return [
        {**s, "sessionId": s.get("session_id")}
        for s in sessions
        if s.get("pid") is None or _pid_alive(s["pid"])
    ]


def _controlling_tty(pid):
    """Best-effort equivalent of hook.sh's SessionStart tty lookup (see
    its docstring), run directly against a session's own pid instead of
    a hook subprocess's parent — there's no hook invocation to piggyback
    on for a session the daemon didn't see start. Returns None for a
    dead pid or one with no controlling terminal (e.g. VS Code's
    integrated terminal, same as hook.sh's "??" case).
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "tty=", "-p", str(pid)],
            capture_output=True, timeout=2, text=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    tty = result.stdout.strip()
    if not tty or tty == "??":
        return None
    return f"/dev/{tty}"


def _tmux_pane_for_tty(tty):
    """Cross-references a controlling tty against every tmux pane on the
    machine to recover the pane id hook.sh would otherwise report via
    $TMUX_PANE — only meaningful when _controlling_tty() above actually
    found one. Best-effort: no tmux on PATH, or no server running, just
    means no match.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_tty} #{pane_id}"],
            capture_output=True, timeout=2, text=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        pane_tty, _, pane_id = line.partition(" ")
        if pane_tty == tty:
            return pane_id
    return None


def _pid_on_tty(tty):
    """Returns the pid of some process attached to the given
    controlling tty (the login shell, ordinarily), or None if nothing
    is. Starting point for _gui_app_ancestor_pid() below.
    """
    short = tty.removeprefix("/dev/")
    try:
        result = subprocess.run(
            ["ps", "-t", short, "-o", "pid="],
            capture_output=True, timeout=2, text=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    for line in result.stdout.split():
        if line.isdigit():
            return int(line)
    return None


def _gui_app_ancestor_pid(pid, max_depth=25):
    """Walks a process's ppid chain looking for the nearest ancestor
    that's an installed, bundled macOS app — its `comm` path contains
    both ".app/Contents/MacOS/" and "/Applications/" (covers both
    /Applications and ~/Applications). Returns that ancestor's pid, or
    None if none is found before the chain bottoms out at launchd
    (pid 1), loops past max_depth, or a `ps` call fails.

    The "/Applications/" requirement guards against a false match hit
    during testing: matching on ".app/Contents/MacOS/" alone also
    catches non-GUI tools that happen to ship inside an app bundle
    without being an "Applications" app at all — e.g. Homebrew's
    python3 launcher, which resolves via a Python.framework's
    Python.app under Cellar.

    Deliberately stops at the *nearest* match rather than climbing all
    the way to launchd: also hit during testing, a terminal emulator
    launched by typing its name/`open` in an *existing* terminal
    window is a child of that other terminal's process, so climbing
    further would walk straight past the correct (nearest) app and
    land on the outer one instead — e.g. Kitty launched from a
    Terminal.app tab resolving to Terminal.app rather than Kitty.

    This is what makes _dispatch_generic_gui() below work for *any*
    terminal emulator without app-specific code: rather than a bespoke
    AppleScript per app (its own scripting dictionary, or none at
    all), the owning GUI app is found the same way regardless of which
    one it is, then raised via System Events by pid.
    """
    for _ in range(max_depth):
        try:
            result = subprocess.run(
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                capture_output=True, timeout=2, text=True,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        line = result.stdout.strip()
        if not line:
            return None
        ppid_str, _, comm = line.partition(" ")
        comm = comm.strip()
        if ".app/Contents/MacOS/" in comm and "/Applications/" in comm:
            return pid
        if not ppid_str.isdigit() or int(ppid_str) <= 1:
            return None
        pid = int(ppid_str)
    return None


# --- Daemon: socket server + hook->state logic -------------------------

# Notification:permission_prompt is known to be unreliable — it can
# silently not fire at all (anthropics/claude-code#58909, a 6-second
# idle-gate bug that misses prompts appearing during active thinking).
# As a backstop that doesn't depend on that hook firing, a PreToolUse
# with no matching PostToolUse/PostToolUseFailure within this many
# seconds is treated as probably blocked on the user and escalated to
# "question".
#
# Tradeoff: this can't distinguish "waiting on you" from "just a slow
# command" — a long-running Bash call (npm install, a test suite) will
# false-positive into "question" until it finishes. 10s is a guess at
# a reasonable middle ground; raise it if slow-but-legitimate commands
# are triggering false blinks, lower it if real prompts are taking too
# long to show up. Set to None to disable the heuristic entirely.
STALL_THRESHOLD_SECONDS = 10


class Daemon:
    # Seconds to wait after the last active session ends before
    # releasing the HID handle — see _reconcile_pad() below. Debounces
    # rapid session churn (a session ending and another starting
    # moments later) so the pad isn't closed and reopened for no
    # reason; also gives the VIA app a window to grab the same raw HID
    # interface once nothing here needs it.
    IDLE_CLOSE_GRACE_SECONDS = 5

    def __init__(self):
        # Default sizing — used as-is by tests and any headless run
        # (see recording_daemon in tests/conftest.py). serve() resizes
        # this after the pad's handshake reports its real slot count,
        # so a running daemon with real hardware attached never
        # actually uses NUM_SLOTS unless the handshake fails.
        self.slots = SlotManager(NUM_SLOTS)
        # slot index -> last {"t": "slot"/"clear", ...} message sent for
        # it, kept in sync by _send_pad() below. Replayed by
        # resync_pad() after the transport reattaches — either a real
        # disconnect (HidPadLink._reconnect) or a deliberate idle close/
        # reopen cycle (_reconcile_pad() below) — since the pad's own
        # display has no memory of what it missed while it was gone.
        self.pad_state = {}
        self.pad = HidPadLink(self.on_device_event, on_reattach=self.resync_pad)
        # Serializes open()/close() calls against this pad so a
        # reconcile triggered by a session starting can never run
        # concurrently with one triggered by a session ending — see
        # _reconcile_pad().
        self._pad_lock = asyncio.Lock()
        # session_id -> {"slot": i, "tool_name": ..., "since": monotonic
        # timestamp, "escalated": bool}. Populated on PreToolUse when the
        # initial state is "working" (not already "question"/etc.),
        # cleared on PostToolUse or PostToolUseFailure for that session.
        self.pending_calls = {}
        # session_id -> tmux pane id (e.g. "%37"), as reported by
        # hook.sh reading $TMUX_PANE from its own environment.
        # Populated at SessionStart, cleared at SessionEnd. Empty
        # string (not running inside tmux) is never stored.
        self.session_panes = {}
        # session_id -> project folder name (same string already used
        # for the pad's slot label), used to match a VS Code window by
        # title when no tmux pane is available. This is the primary
        # path when Claude Code runs in VS Code's integrated terminal
        # rather than inside tmux.
        self.session_projects = {}
        # session_id -> controlling tty device (e.g. "/dev/ttys003"),
        # captured by hook.sh at SessionStart via ps against its
        # parent process. Used to find and select the exact
        # Terminal.app tab this session is running in — an exact
        # match, unlike VS Code's fuzzy title substring match.
        self.session_ttys = {}
        # session_id -> agent name (e.g. "claude-code", "codex",
        # "grok-build", "cursor"), from the payload's own "agent" field
        # (each adapter script tags its own — see claude/hook.sh,
        # codex/hook.sh, grok/hook.sh, cursor/hook.sh). Bookkeeping only,
        # for logs — it never affects slot allocation, state mapping, or
        # what's sent to the pad, all of which are already agent-
        # agnostic. Defaults to "claude-code" for payloads that predate
        # this field, and for seed_existing_sessions() below, which is
        # inherently Claude-Code-only (see discover_running_sessions()).
        self.session_agents = {}
        # session_id -> name of whichever _dispatch_* method last
        # successfully raised this session's window (see
        # dispatch_bring_to_front). Tried first on the next keypress,
        # ahead of the rest of the fallback chain — avoids re-probing
        # methods already known not to apply to this session (e.g. two
        # failed osascript round-trips before reaching the one that
        # actually works), which was adding ~750ms of perceptible
        # latency per press for non-Terminal/iTerm sessions. Only ever
        # a speed hint: a cache miss or a cached method that no longer
        # works still falls through to the full chain in the same
        # call, so nothing degrades on failure — it just re-learns.
        self.session_dispatch_method = {}

    def _send_pad(self, msg):
        """write_json(), plus remembering it in self.pad_state so
        resync_pad() can replay it later. Every slot/clear write should
        go through this instead of self.pad.write_json() directly —
        skipping it just means that slot won't be restored after a
        reconnect.

        Merges into the cached entry rather than overwriting it: a
        mid-session update like a plain state change carries no
        "label" key at all, and overwriting wholesale would silently
        drop the label a PreToolUse message set earlier. "clear" pops
        the slot entirely — a freed slot has nothing to resync.
        """
        i = msg.get("i")
        if i is not None:
            if msg.get("t") == "clear":
                self.pad_state.pop(i, None)
            else:
                self.pad_state.setdefault(i, {}).update(msg)
        self.pad.write_json(msg)

    def resync_pad(self):
        """Replays every slot's last-known state after the transport
        reattaches (see HidPadLink._reconnect, or a deliberate
        idle-close/reopen cycle via _reconcile_pad() below) — the pad
        has no memory of what it missed while it was gone, so without
        this it'd come back showing whatever it last displayed before
        going quiet. Runs on the transport's background reconnect
        thread; snapshot the values first since _send_pad() can mutate
        pad_state concurrently from the connection-handling side.
        """
        log.info("pad reattached — resyncing %d slot(s)", len(self.pad_state))
        for msg in list(self.pad_state.values()):
            self.pad.write_json(msg)

    async def _reconcile_pad(self, delay=0):
        """Opens or closes the pad connection to match whether any
        session is currently active — the actual idle-release
        mechanism. Deliberately recomputes "should the pad be open?"
        at execution time (under _pad_lock), not at the moment it was
        scheduled: a session starting right before a delayed close was
        due to fire just makes that close a no-op instead of needing
        explicit cancellation bookkeeping.

        open()/close() do real device I/O (discovery can take ~1.5s),
        so both run off the event loop via asyncio.to_thread — this
        coroutine is only ever invoked as a background task (see
        _kick_reconcile()), never awaited inline from hook handling.

        A reopen here is only ever a *deliberate* one (the session that
        prompted it is why want_open flipped true) — unlike
        HidPadLink's own _reconnect() after a dropped HID handle, open()
        doesn't call on_reattach() itself (see its docstring in
        pad_link.py), so resync_pad() is called explicitly right after,
        to replay whatever _send_pad() already cached. That matters
        because the very SessionStart that triggered this reopen has
        already called _send_pad() for its own slot by the time this
        coroutine actually gets to run open() — that write landed while
        self.pad._dev was still None and was silently dropped (see
        HidPadLink.write_json), so without this resync that session's
        first slot state would never reach the pad at all.
        """
        if delay:
            await asyncio.sleep(delay)
        async with self._pad_lock:
            want_open = len(self.slots.session_to_slot) > 0
            if want_open and not self.pad.attached:
                await asyncio.to_thread(self.pad.open)
                if self.pad.attached:
                    self.resync_pad()
            elif not want_open and self.pad.attached:
                await asyncio.to_thread(self.pad.close)

    def _kick_reconcile(self, delay=0):
        """Fire-and-forget entry point for _reconcile_pad(), called
        after every slot allocate/free. Must tolerate being called
        with no asyncio event loop running: most of
        tests/test_handle_hook_event.py calls handle_hook_event()
        directly and synchronously (no real pad, write_json swapped
        for a recorder), so raising here would break every one of
        those — skipping reconciliation in that case is correct, not
        just convenient, since there's nothing to reconcile without a
        loop actually driving open()/close().
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._reconcile_pad(delay))

    # device -> host: keypress events from the pad.
    #
    # A tap ("key") does exactly one thing — bring that session's window
    # to the front. Dispatch mechanisms, tried in order:
    #   1. tmux select-window, if a pane was recorded for this session
    #   2. Terminal.app tab activation, matched by exact controlling
    #      tty — only ever populated at SessionStart (see hook.sh)
    #   3. VS Code window activation via AppleScript, matched by
    #      project folder name
    #   4. IntelliJ IDEA window activation, same project-name match —
    #      tried after VS Code since both are gated on the same
    #      `project` value and either/neither may actually be running
    #
    # A hold ("key_held") does the opposite — it manually clears
    # whatever session is mapped to that slot (see _evict_slot() below),
    # for a stale mapping nothing else would ever free.
    def on_device_event(self, msg):
        t = msg.get("t")
        if t == "key":
            i = msg.get("i")
            valid_index = i is not None and i < self.slots.num_slots
            session_id = self.slots.slot_to_session[i] if valid_index else None
            agent = self.session_agents.get(session_id, "<unknown>")
            log.info("key %s -> session %s (agent=%s)", i, session_id, agent)
            if session_id is None:
                events_log.info("DISPATCH key=%s result=no_session", i)
                if valid_index:
                    # Slot has no session mapped (stale key press, race
                    # with SessionEnd, or the pad's own state drifted
                    # from ours) — clear its color/OLED rather than
                    # leaving it showing whatever it last displayed.
                    self._send_pad({"t": "clear", "i": i})
                return
            self.dispatch_bring_to_front(i, session_id)
        elif t == "key_held":
            i = msg.get("i")
            if i is not None and i < self.slots.num_slots:
                self._evict_slot(i)
        else:
            log.info("unhandled device event: %s", msg)

    def _evict_slot(self, i):
        """Manually clear slot `i`'s session mapping — fired by a held
        key (see MSG_KEY_HELD in hid_protocol.py), for a slot whose
        session is stale (e.g. a duplicate VS Code session, or one that
        ended without a clean SessionEnd) and stuck occupying a slot no
        session can otherwise reclaim. Mirrors SessionEnd's cleanup
        (handle_hook_event() above) exactly, but keyed off the slot
        index rather than a session_id from a hook payload.
        """
        session_id = self.slots.evict(i)
        if session_id is not None:
            self.pending_calls.pop(session_id, None)
            self.session_panes.pop(session_id, None)
            self.session_ttys.pop(session_id, None)
            self.session_projects.pop(session_id, None)
            self.session_agents.pop(session_id, None)
            self.session_dispatch_method.pop(session_id, None)
        self._send_pad({"t": "clear", "i": i})
        events_log.info(
            "MAPPED slot=%s cleared (ManualEvict session=%s)", i, session_id
        )

    def dispatch_bring_to_front(self, slot, session_id):
        pane = self.session_panes.get(session_id)
        tty = self.session_ttys.get(session_id)
        project = self.session_projects.get(session_id)

        # (name, arg, method) in the fallback order tried on a cache
        # miss — arg is whichever of pane/tty/project that method
        # needs, and a method is skipped outright if its arg is falsy.
        candidates = [
            ("tmux", pane, self._dispatch_tmux),
            ("terminal", tty, self._dispatch_terminal),
            ("iterm", tty, self._dispatch_iterm),
            ("generic_gui", tty, self._dispatch_generic_gui),
            ("vscode", project, self._dispatch_vscode),
            ("intellij", project, self._dispatch_intellij),
        ]

        # Whichever method last worked for this session is tried
        # first, ahead of the fallback order above — see
        # session_dispatch_method's own comment for why. A stable sort
        # moves it to the front without disturbing the fallback order
        # among the rest.
        cached = self.session_dispatch_method.get(session_id)
        if cached:
            candidates.sort(key=lambda c: c[0] != cached)

        for name, arg, method in candidates:
            if arg and method(slot, arg):
                self.session_dispatch_method[session_id] = name
                return

        if not pane and not tty and not project:
            log.warning(
                "no tmux pane, tty, or project name known for slot %s (session=%s)",
                slot, session_id,
            )
            events_log.info(
                "DISPATCH slot=%s session=%s result=no_target_known", slot, session_id
            )
        # else: whichever dispatch(es) were attempted already logged
        # their own specific failure reason.

    def _dispatch_tmux(self, slot, pane):
        """Try tmux select-window. Returns True on success, False on
        any failure (falls through to VS Code dispatch if available).
        Runs on the pad transport's background reader thread (not the
        asyncio event loop), so a blocking subprocess call here is
        fine — it only stalls pad reads briefly, not hook processing.
        """
        try:
            result = subprocess.run(
                ["tmux", "select-window", "-t", pane],
                capture_output=True,
                timeout=2,
                text=True,
            )
        except FileNotFoundError:
            log.warning("tmux not found on PATH — can't dispatch keypress")
            events_log.info("DISPATCH slot=%s pane=%s result=tmux_not_found", slot, pane)
            return False
        except subprocess.TimeoutExpired:
            log.warning("tmux select-window timed out for pane %s", pane)
            events_log.info("DISPATCH slot=%s pane=%s result=timeout", slot, pane)
            return False

        if result.returncode != 0:
            # Most common cause: the pane no longer exists (session
            # ended outside of our SessionEnd hook catching it — e.g.
            # the tmux pane was killed directly) — not a crash, just
            # a stale mapping.
            log.warning(
                "tmux select-window failed for pane %s: %s", pane, result.stderr.strip()
            )
            events_log.info(
                "DISPATCH slot=%s pane=%s result=tmux_error stderr=%r",
                slot, pane, result.stderr.strip(),
            )
            return False

        log.info("selected tmux window for pane %s (slot %s)", pane, slot)
        events_log.info("DISPATCH slot=%s pane=%s result=ok", slot, pane)
        return True

    def _dispatch_terminal(self, slot, tty):
        """Try to select+raise the Terminal.app tab whose tty matches
        exactly. macOS only. Returns True on success, False on any
        failure (falls through to VS Code dispatch if available).

        Uses Terminal.app's own AppleScript dictionary (windows/tabs/
        tty are native properties) rather than System Events GUI
        scripting — only System Events is needed for the up-front
        process-existence check, to avoid accidentally launching
        Terminal.app via `tell application "Terminal"` if it isn't
        already running.

        This naturally never matches when the session is actually
        running inside tmux: the tty captured at SessionStart is
        whatever pty was current at that moment, which inside tmux is
        the pane's own inner pty, not the outer Terminal.app tab
        hosting the tmux client. No special-casing needed to keep the
        two paths from interfering — tmux is tried first anyway.

        Same permission requirement as VS Code dispatch: the terminal
        running this daemon needs Accessibility/Automation access
        granted for "System Events" and, separately, "Terminal" — a
        second permission prompt distinct from the first.
        """
        safe_tty = tty.replace("\\", "\\\\").replace('"', '\\"')
        script = f'''
        tell application "System Events"
            if not (exists process "Terminal") then
                return "no_process"
            end if
        end tell
        tell application "Terminal"
            set foundTab to missing value
            set foundWindow to missing value
            repeat with w in windows
                repeat with t in tabs of w
                    try
                        if (tty of t) is "{safe_tty}" then
                            set foundTab to t
                            set foundWindow to w
                            exit repeat
                        end if
                    end try
                end repeat
                if foundTab is not missing value then exit repeat
            end repeat
            if foundTab is missing value then
                return "no_match"
            end if
            set selected tab of foundWindow to foundTab
            set frontmost of foundWindow to true
            activate
            return "ok"
        end tell
        '''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=3,
                text=True,
            )
        except FileNotFoundError:
            log.warning("osascript not found — Terminal dispatch requires macOS")
            events_log.info(
                "DISPATCH slot=%s tty=%s result=osascript_not_found", slot, tty
            )
            return False
        except subprocess.TimeoutExpired:
            log.warning("osascript timed out activating Terminal tab for %s", tty)
            events_log.info("DISPATCH slot=%s tty=%s result=timeout", slot, tty)
            return False

        output = result.stdout.strip()
        if result.returncode != 0 or output != "ok":
            reason = output or result.stderr.strip() or "unknown_error"
            log.warning("Terminal tab activation failed for tty %r: %s", tty, reason)
            events_log.info(
                "DISPATCH slot=%s tty=%s result=terminal_error reason=%r",
                slot, tty, reason,
            )
            return False

        log.info("activated Terminal tab for tty %r (slot %s)", tty, slot)
        events_log.info("DISPATCH slot=%s tty=%s result=ok", slot, tty)
        return True

    def _dispatch_iterm(self, slot, tty):
        """Try to select+raise the iTerm session whose tty matches
        exactly. macOS only. Returns True on success, False on any
        failure (falls through to VS Code/IntelliJ dispatch if
        available).

        iTerm's AppleScript object model nests tty one level deeper
        than Terminal.app's: windows -> tabs -> sessions, with tty a
        property of the session rather than the tab. The app is
        addressed as "iTerm" (its scripting name) but registers as
        process "iTerm2" with System Events for the up-front
        process-existence check.

        Same permission requirement as Terminal dispatch: the terminal
        running this daemon needs Accessibility/Automation access
        granted for "System Events" and, separately, "iTerm2".
        """
        safe_tty = tty.replace("\\", "\\\\").replace('"', '\\"')
        script = f'''
        tell application "System Events"
            if not (exists process "iTerm2") then
                return "no_process"
            end if
        end tell
        tell application "iTerm"
            set foundSession to missing value
            set foundTab to missing value
            set foundWindow to missing value
            repeat with w in windows
                repeat with t in tabs of w
                    repeat with s in sessions of t
                        try
                            if (tty of s) is "{safe_tty}" then
                                set foundSession to s
                                set foundTab to t
                                set foundWindow to w
                                exit repeat
                            end if
                        end try
                    end repeat
                    if foundSession is not missing value then exit repeat
                end repeat
                if foundSession is not missing value then exit repeat
            end repeat
            if foundSession is missing value then
                return "no_match"
            end if
            select foundSession
            tell foundTab to select
            tell foundWindow to select
            activate
            return "ok"
        end tell
        '''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=3,
                text=True,
            )
        except FileNotFoundError:
            log.warning("osascript not found — iTerm dispatch requires macOS")
            events_log.info(
                "DISPATCH slot=%s tty=%s result=osascript_not_found", slot, tty
            )
            return False
        except subprocess.TimeoutExpired:
            log.warning("osascript timed out activating iTerm session for %s", tty)
            events_log.info("DISPATCH slot=%s tty=%s result=timeout", slot, tty)
            return False

        output = result.stdout.strip()
        if result.returncode != 0 or output != "ok":
            reason = output or result.stderr.strip() or "unknown_error"
            log.warning("iTerm session activation failed for tty %r: %s", tty, reason)
            events_log.info(
                "DISPATCH slot=%s tty=%s result=iterm_error reason=%r",
                slot, tty, reason,
            )
            return False

        log.info("activated iTerm session for tty %r (slot %s)", tty, slot)
        events_log.info("DISPATCH slot=%s tty=%s result=ok", slot, tty)
        return True

    def _dispatch_generic_gui(self, slot, tty):
        """Fallback for any terminal emulator without a bespoke method
        above (Alacritty, Kitty, WezTerm, Hyper, Ghostty, ...): finds
        the GUI app that owns this tty via process-tree ancestry
        (_pid_on_tty + _gui_app_ancestor_pid) and raises it by pid
        through System Events, instead of needing a new per-app
        AppleScript method — the whole point of this method existing.

        Coarser than the tmux/Terminal.app/iTerm dispatches above: it
        raises the app's frontmost window, not necessarily the
        specific tab/window hosting this tty. Most of these apps are
        single-process, multi-window, and there's no app-agnostic way
        to ask System Events "which of your windows has this tty" —
        only apps with their own scripting dictionary (like Terminal
        and iTerm) expose that. Still strictly better than no dispatch
        at all, which is the alternative for anything not on the
        bespoke list above.
        """
        owner_pid = _pid_on_tty(tty)
        if owner_pid is None:
            log.warning("no process found attached to tty %s", tty)
            events_log.info("DISPATCH slot=%s tty=%s result=no_tty_owner", slot, tty)
            return False

        app_pid = _gui_app_ancestor_pid(owner_pid)
        if app_pid is None:
            log.warning("no GUI app ancestor found for tty %s (pid %s)", tty, owner_pid)
            events_log.info("DISPATCH slot=%s tty=%s result=no_app_ancestor", slot, tty)
            return False

        script = f'''
        tell application "System Events"
            set matches to (processes whose unix id is {app_pid})
            if (count of matches) is 0 then return "no_process"
            tell item 1 of matches
                set frontmost to true
                try
                    perform action "AXRaise" of window 1
                end try
            end tell
        end tell
        return "ok"
        '''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=3,
                text=True,
            )
        except FileNotFoundError:
            log.warning("osascript not found — generic dispatch requires macOS")
            events_log.info(
                "DISPATCH slot=%s tty=%s result=osascript_not_found", slot, tty
            )
            return False
        except subprocess.TimeoutExpired:
            log.warning(
                "osascript timed out activating app pid %s for tty %s", app_pid, tty
            )
            events_log.info("DISPATCH slot=%s tty=%s result=timeout", slot, tty)
            return False

        output = result.stdout.strip()
        if result.returncode != 0 or output != "ok":
            reason = output or result.stderr.strip() or "unknown_error"
            log.warning(
                "generic app activation failed for tty %r (pid %s): %s",
                tty, app_pid, reason,
            )
            events_log.info(
                "DISPATCH slot=%s tty=%s result=generic_error reason=%r",
                slot, tty, reason,
            )
            return False

        log.info("activated app pid %s for tty %r (slot %s)", app_pid, tty, slot)
        events_log.info("DISPATCH slot=%s tty=%s result=ok", slot, tty)
        return True

    def _dispatch_vscode(self, slot, project):
        """Try to raise a VS Code window whose title contains the
        project folder name, via AppleScript/System Events. macOS
        only. Returns True on success, False on any failure.

        Known limitations (not fixable without a companion VS Code
        extension):
          - Window-level only, not tab-level. Two sessions running as
            two integrated-terminal tabs in ONE VS Code window can't
            be distinguished — both resolve to the same window.
          - Matches by substring on window title. A project named
            "app" will also match a window titled "my-app — Visual
            Studio Code". Ambiguous matches silently pick the first
            one System Events returns.
          - Requires the terminal running this daemon to have been
            granted Accessibility/Automation permission for "System
            Events" and "Visual Studio Code" in System Settings ->
            Privacy & Security. Without it, osascript fails with a
            permission error, logged like any other failure.
          - Depends on VS Code's default window-title format (folder
            name visible in the title). A customized "window.title"
            setting in VS Code can break the match.
        """
        # Defensive escaping — project names come from folder names,
        # but a stray embedded double-quote would otherwise break out
        # of the AppleScript string literal.
        safe_project = project.replace("\\", "\\\\").replace('"', '\\"')
        script = f'''
        tell application "System Events"
            if not (exists process "Code") then
                return "no_process"
            end if
            tell process "Code"
                set matchingWindows to (windows whose name contains "{safe_project}")
                if (count of matchingWindows) is 0 then
                    return "no_window"
                end if
                perform action "AXRaise" of item 1 of matchingWindows
            end tell
        end tell
        tell application "Visual Studio Code" to activate
        return "ok"
        '''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=3,
                text=True,
            )
        except FileNotFoundError:
            log.warning("osascript not found — VS Code dispatch requires macOS")
            events_log.info(
                "DISPATCH slot=%s project=%s result=osascript_not_found", slot, project
            )
            return False
        except subprocess.TimeoutExpired:
            log.warning("osascript timed out activating VS Code window for %s", project)
            events_log.info("DISPATCH slot=%s project=%s result=timeout", slot, project)
            return False

        output = result.stdout.strip()
        if result.returncode != 0 or output != "ok":
            # returncode != 0 with no clean "no_process"/"no_window"
            # output usually means the Accessibility/Automation
            # permission prompt hasn't been granted yet — stderr will
            # say so explicitly the first time.
            reason = output or result.stderr.strip() or "unknown_error"
            log.warning(
                "VS Code window activation failed for project %r: %s", project, reason
            )
            events_log.info(
                "DISPATCH slot=%s project=%s result=vscode_error reason=%r",
                slot, project, reason,
            )
            return False

        log.info("activated VS Code window for project %r (slot %s)", project, slot)
        events_log.info("DISPATCH slot=%s project=%s result=ok", slot, project)
        return True

    def _dispatch_intellij(self, slot, project):
        """Try to raise an IntelliJ IDEA window whose title contains
        the project folder name, via AppleScript/System Events. macOS
        only. Returns True on success, False on any failure.

        Same approach and the same window-level/substring-match
        limitations as _dispatch_vscode above (see its docstring) —
        including needing its own, separate Accessibility/Automation
        grant for "IntelliJ IDEA", distinct from VS Code's — plus one
        more specific to this app:
          - Assumes the System Events process name is "idea", which
            matches a standalone IntelliJ IDEA install (Community or
            Ultimate). A JetBrains Toolbox install, a differently
            named .app bundle, or another JetBrains IDE (PyCharm,
            WebStorm, ...) can report a different process name and
            won't match — adjust the process/application names below
            if that's your setup.
        """
        safe_project = project.replace("\\", "\\\\").replace('"', '\\"')
        script = f'''
        tell application "System Events"
            if not (exists process "idea") then
                return "no_process"
            end if
            tell process "idea"
                set matchingWindows to (windows whose name contains "{safe_project}")
                if (count of matchingWindows) is 0 then
                    return "no_window"
                end if
                perform action "AXRaise" of item 1 of matchingWindows
            end tell
        end tell
        tell application "IntelliJ IDEA" to activate
        return "ok"
        '''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=3,
                text=True,
            )
        except FileNotFoundError:
            log.warning("osascript not found — IntelliJ dispatch requires macOS")
            events_log.info(
                "DISPATCH slot=%s project=%s result=osascript_not_found", slot, project
            )
            return False
        except subprocess.TimeoutExpired:
            log.warning("osascript timed out activating IntelliJ window for %s", project)
            events_log.info("DISPATCH slot=%s project=%s result=timeout", slot, project)
            return False

        output = result.stdout.strip()
        if result.returncode != 0 or output != "ok":
            reason = output or result.stderr.strip() or "unknown_error"
            log.warning(
                "IntelliJ window activation failed for project %r: %s", project, reason
            )
            events_log.info(
                "DISPATCH slot=%s project=%s result=intellij_error reason=%r",
                slot, project, reason,
            )
            return False

        log.info("activated IntelliJ window for project %r (slot %s)", project, slot)
        events_log.info("DISPATCH slot=%s project=%s result=ok", slot, project)
        return True

    # host -> device: one line of raw hook JSON, piped straight through
    # by an agent's own hook adapter script with no transformation
    # beyond that adapter's own enrichment (tmux_pane, controlling_tty,
    # agent — see claude/hook.sh and codex/hook.sh). hook_event_name and
    # session_id are provided by the agent itself in every event's
    # payload (confirmed against both Claude Code's and Codex's own
    # hooks references) — no need to inject either.
    def handle_hook_event(self, payload):
        event_name = payload.get("hook_event_name")
        session_id = payload.get("session_id")
        # Bookkeeping only (see self.session_agents in __init__) — never
        # used for mapping logic. Defaults to "claude-code" so payloads
        # from before this field existed (or a hand-rolled test payload
        # that omits it) still attribute sensibly.
        agent = payload.get("agent", "claude-code")

        # Log every event unconditionally, before any mapping logic.
        # Without this, a Notification that arrives but maps to
        # state=None (unrecognized notification_type, or the field
        # missing entirely) is completely silent — you'd see nothing
        # in the logs and have no way to tell "event never arrived"
        # apart from "event arrived but didn't map to a state change".
        log.info(
            "hook event: %s (agent=%s session=%s tool=%s notification_type=%s)",
            event_name,
            agent,
            session_id,
            payload.get("tool_name"),
            payload.get("notification_type"),
        )
        # Full payload, persisted — handle_connection's RAW line covers
        # the same bytes, but logging the parsed dict here too means a
        # grep for "PAYLOAD" finds only things that parsed successfully
        # and reached this function, without wading through RAW/
        # PARSE_FAILED noise from other connections.
        events_log.info("PAYLOAD %s", json.dumps(payload))

        if not session_id:
            log.warning("hook event missing session_id: %s", payload)
            events_log.info("DROPPED reason=no_session_id")
            return

        if event_name == "SessionStart":
            i = self.slots.allocate(session_id)
            self._kick_reconcile()
            self.session_agents[session_id] = agent
            pane = payload.get("tmux_pane")
            if pane:
                self.session_panes[session_id] = pane
            tty = payload.get("controlling_tty")
            if tty:
                self.session_ttys[session_id] = tty
            label = Path(payload.get("cwd", "")).name or session_id[:8]
            self.session_projects[session_id] = label
            if i is not None:
                self._send_pad(
                    {"t": "slot", "i": i, "state": "idle", "label": label}
                )
                events_log.info(
                    "MAPPED slot=%s state=idle agent=%s pane=%s tty=%s project=%s (SessionStart)",
                    i, agent, pane or "<none>", tty or "<none>", label,
                )
            return

        if event_name == "SessionEnd":
            i = self.slots.free(session_id)
            self._kick_reconcile(delay=self.IDLE_CLOSE_GRACE_SECONDS)
            self.pending_calls.pop(session_id, None)
            self.session_panes.pop(session_id, None)
            self.session_ttys.pop(session_id, None)
            self.session_projects.pop(session_id, None)
            self.session_agents.pop(session_id, None)
            self.session_dispatch_method.pop(session_id, None)
            if i is not None:
                self._send_pad({"t": "clear", "i": i})
                events_log.info("MAPPED slot=%s cleared (SessionEnd)", i)
            return

        i = self.slots.slot_for(session_id)
        if i is None:
            # Hook arrived before SessionStart, we're past the pad's
            # slot capacity (self.slots.num_slots — sized from the
            # handshake in serve(), see Phase 3), or (confirmed
            # 2026-08-02) the daemon
            # was restarted mid-session so this process never saw that
            # session's SessionStart. Allocate lazily rather than drop
            # the event on the floor, and backfill project/pane here
            # too — cwd and tmux_pane are present on every hook event,
            # not just SessionStart — otherwise dispatch_bring_to_front
            # never learns a target for this session and every keypress
            # logs no_target_known until the session ends.
            i = self.slots.allocate(session_id)
            self._kick_reconcile()
            if i is None:
                events_log.info("DROPPED reason=no_free_slot")
                return
            label = Path(payload.get("cwd", "")).name or session_id[:8]
            self.session_projects[session_id] = label
            self.session_agents[session_id] = agent

        # Backfill tmux_pane from whichever hook event happens to be the
        # first one this daemon process sees for the session, even if a
        # slot was already allocated above — seed_existing_sessions()
        # can allocate a slot for a session at startup without ever
        # learning its pane (claude agents --json has no tmux info), so
        # this can't be folded into the "i is None" branch above without
        # leaving those sessions permanently pane-less.
        if session_id not in self.session_panes:
            pane = payload.get("tmux_pane")
            if pane:
                self.session_panes[session_id] = pane

        state = hook_to_state(
            event_name, payload.get("tool_name"), payload.get("notification_type")
        )

        # Stall-detection bookkeeping — independent of whether this
        # event changes the displayed state. See STALL_THRESHOLD_SECONDS
        # for why this exists: Notification:permission_prompt can't be
        # trusted to fire on its own.
        if event_name == "PreToolUse" and state == "tool_running":
            self.pending_calls[session_id] = {
                "slot": i,
                "tool_name": payload.get("tool_name"),
                "since": time.monotonic(),
                "escalated": False,
            }
        elif event_name in ("PostToolUse", "PostToolUseFailure"):
            self.pending_calls.pop(session_id, None)
        elif state == "question":
            # A definite blocked-on-you signal (PermissionRequest,
            # Notification:agent_needs_input, ...) arrived for a tool
            # call already being tracked as pending — e.g. PreToolUse
            # for Bash, followed by PermissionRequest for that same
            # command. Drop it from pending_calls so watch_stalled_calls
            # doesn't later clobber this "question" with "tool_stalled":
            # that would replace a state we're already certain about
            # (blocked on you) with a guess (maybe blocked, maybe just
            # slow) once STALL_THRESHOLD_SECONDS elapses from the
            # original PreToolUse. PostToolUse still arrives once the
            # call actually resolves either way, so nothing here is
            # left permanently untracked.
            self.pending_calls.pop(session_id, None)

        if state is None:
            # This is the exact case that made the jq bug invisible
            # before: an event arrives and parses fine, but maps to no
            # state change. Logging it explicitly means "no mapping"
            # is now distinguishable from "never arrived" by grepping
            # the events log instead of having to reason about it.
            events_log.info(
                "MAPPED slot=%s state=<none> event=%s tool=%s notification_type=%s",
                i, event_name, payload.get("tool_name"), payload.get("notification_type"),
            )
            return  # e.g. SubagentStop — no direct display change yet

        msg = {"t": "slot", "i": i, "state": state}
        if event_name == "PreToolUse" and payload.get("tool_name"):
            msg["label"] = payload["tool_name"]
        self._send_pad(msg)
        events_log.info("MAPPED slot=%s state=%s event=%s", i, state, event_name)

    async def watch_stalled_calls(self):
        """Backstop for Notification:permission_prompt's unreliability —
        and for tool calls that are just taking a while.

        Polls pending_calls once a second; anything sitting past
        STALL_THRESHOLD_SECONDS without a PostToolUse/PostToolUseFailure
        gets escalated to "tool_stalled" (blinking purple) exactly once.
        This deliberately does NOT claim "question" (blocked, needs your
        input) — we don't actually know whether this is an unreported
        permission prompt or just a slow tool, so "still purple, just
        been a while" is the honest signal. No further action needed on
        the daemon's part after that — the normal PostToolUse handling
        in handle_hook_event already downgrades back to "working"/"done"
        whenever the tool call actually finishes, whether or not it was
        escalated first.
        """
        if STALL_THRESHOLD_SECONDS is None:
            return
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            for session_id, pending in list(self.pending_calls.items()):
                if pending["escalated"]:
                    continue
                if now - pending["since"] < STALL_THRESHOLD_SECONDS:
                    continue
                pending["escalated"] = True
                self._send_pad({"t": "slot", "i": pending["slot"], "state": "tool_stalled"})
                events_log.info(
                    "MAPPED slot=%s state=tool_stalled event=stall_detected tool=%s elapsed=%.1fs",
                    pending["slot"], pending["tool_name"], now - pending["since"],
                )
                log.info(
                    "stall detected: session=%s tool=%s elapsed=%.1fs — escalating to tool_stalled",
                    session_id, pending["tool_name"], now - pending["since"],
                )

    async def handle_connection(self, reader, writer):
        try:
            while True:
                try:
                    line = await reader.readline()
                except ValueError:
                    # Line exceeded SOCKET_READ_LIMIT before a newline
                    # was found (asyncio.LimitOverrunError, re-raised
                    # by StreamReader.readline() as ValueError). The
                    # oversized data is still sitting in the buffer, so
                    # drop this connection instead of looping on the
                    # same unreadable line.
                    log.warning(
                        "hook payload exceeded %d-byte socket read limit; dropping connection",
                        SOCKET_READ_LIMIT,
                    )
                    events_log.info("READ_LIMIT_EXCEEDED limit=%d", SOCKET_READ_LIMIT)
                    break
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue

                # Log the raw bytes exactly as received, before parsing,
                # so a framing bug (e.g. multi-line jq output splitting
                # one object into several "lines") is visible in the
                # persisted log even though json.loads() below never
                # succeeds for it.
                events_log.info("RAW %r", line)

                try:
                    payload = json.loads(line)
                except ValueError:
                    log.warning("bad json on socket: %r", line)
                    events_log.info("PARSE_FAILED %r", line)
                    continue
                self.handle_hook_event(payload)
        finally:
            writer.close()

    def seed_existing_sessions(self):
        """Pre-populate slots from each agent's own pre-existing-session
        discovery (see the "Existing session discovery" comment block
        above discover_running_sessions()) so sessions already running
        when this daemon starts show up on the pad immediately, instead
        of staying blank until they happen to fire a hook event. Called
        once from serve(), before the socket starts accepting hook
        events, so there's no race with handle_hook_event's own
        slot_for()/allocate() calls.

        Seeds state="idle" unconditionally rather than guessing — the
        daemon has no way to know if a pre-existing session is mid-tool-
        call or waiting on you, and the next real hook event corrects it
        momentarily either way.

        Every other slot — anything this loop doesn't allocate to a real
        session — gets explicitly cleared to "off". The pad has no idea
        the old daemon process is gone; without this, a slot left mid-
        color by a now-dead session (or a previous daemon run, e.g. a
        crash or a manual test that never sent SessionEnd) just sits
        there showing stale state forever, since nothing else ever
        revisits an unallocated slot.
        """
        sources = (
            (discover_running_sessions(), "claude-code", "`claude agents --json`"),
            (
                discover_running_grok_sessions(), "grok-build",
                str(_grok_active_sessions_path()),
            ),
        )
        for sessions, agent, source_label in sources:
            for s in sessions:
                session_id = s.get("sessionId")
                if not session_id:
                    continue
                i = self.slots.allocate(session_id)
                if i is None:
                    continue
                label = Path(s.get("cwd", "")).name or session_id[:8]
                self.session_projects[session_id] = label
                self.session_agents[session_id] = agent

                tty = None
                pid = s.get("pid")
                if pid:
                    tty = _controlling_tty(pid)
                if tty:
                    self.session_ttys[session_id] = tty
                    pane = _tmux_pane_for_tty(tty)
                    if pane:
                        self.session_panes[session_id] = pane

                self._send_pad({"t": "slot", "i": i, "state": "idle", "label": label})
                events_log.info(
                    "MAPPED slot=%s state=idle agent=%s pane=%s tty=%s project=%s (seeded at startup)",
                    i, agent, self.session_panes.get(session_id, "<none>"), tty or "<none>", label,
                )
            if sessions:
                log.info(
                    "seeded %d pre-existing session(s) from %s",
                    len(sessions), source_label,
                )

        cleared = 0
        for i in range(self.slots.num_slots):
            if self.slots.slot_to_session[i] is None:
                self._send_pad({"t": "clear", "i": i})
                cleared += 1
        if cleared:
            log.info("cleared %d unclaimed slot(s) to off at startup", cleared)

    def apply_handshake(self, handshake):
        """Size self.slots from a pad's handshake() result (Phase 3),
        so "if it supports 1, fine; if it supports 100, great" — the
        daemon never hardcodes a number, it asks the hardware. Split
        out from serve() so this decision is unit-testable without an
        asyncio event loop.

        Falls back to keeping __init__'s NUM_SLOTS-sized default if
        `handshake` is None (headless, or the pad didn't answer in
        time) — a best-effort capability query, not a hard
        requirement. A protocol version that doesn't match
        hid_protocol.PROTOCOL_VERSION is a warning, not a reject:
        older firmware may lack states this daemon sends (they render
        as the unknown fallback color), newer firmware may expose
        features this daemon doesn't know about yet. Any session
        mappings already recorded in the old SlotManager are
        intentionally discarded: this only ever runs once, before
        start_unix_server in serve(), so there aren't any yet.
        """
        if handshake and handshake.get("slots"):
            protocol = handshake.get("protocol")
            if protocol is not None and protocol != hid_protocol.PROTOCOL_VERSION:
                if protocol > hid_protocol.PROTOCOL_VERSION:
                    log.warning(
                        "pad protocol version %d is newer than daemon's %d — "
                        "update the daemon to support newer keyboard functionality",
                        protocol, hid_protocol.PROTOCOL_VERSION,
                    )
                else:
                    log.warning(
                        "pad protocol version %d is older than daemon's %d — "
                        "keyboard firmware may lack functionality this daemon uses "
                        "(unknown states render as the fallback color)",
                        protocol, hid_protocol.PROTOCOL_VERSION,
                    )
            self.slots = SlotManager(handshake["slots"])
            log.info(
                "pad handshake reports %d slot(s) protocol=%s — resized SlotManager accordingly",
                handshake["slots"], protocol,
            )
        else:
            log.info(
                "no slot-count handshake (headless, or pad didn't answer) — "
                "using default NUM_SLOTS=%d", NUM_SLOTS,
            )

    async def serve(self):
        sock_path = Path(SOCKET_PATH)
        if sock_path.exists():
            sock_path.unlink()

        self.pad.open()
        # Must happen before start_unix_server below so no hook event
        # can arrive and get mapped under the old (possibly wrong)
        # sizing.
        self.apply_handshake(self.pad.handshake())
        # Also before start_unix_server: seeds slots from sessions
        # already running, so there's no race with a hook event for one
        # of them arriving and hitting handle_hook_event's own
        # allocate() first.
        self.seed_existing_sessions()
        # If nothing was seeded, release the handle we just opened for
        # the handshake above rather than holding it for however long
        # it takes the first real session to start — same debounced
        # path _kick_reconcile() uses everywhere else, so VIA can be
        # used in the meantime.
        self._kick_reconcile(delay=self.IDLE_CLOSE_GRACE_SECONDS)

        server = await asyncio.start_unix_server(
            self.handle_connection, path=str(sock_path), limit=SOCKET_READ_LIMIT
        )
        log.info("listening on %s", SOCKET_PATH)

        watcher_task = asyncio.create_task(self.watch_stalled_calls())

        async with server:
            await server.serve_forever()

        watcher_task.cancel()


def main():
    daemon = Daemon()

    def shutdown(*_):
        daemon.pad.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    asyncio.run(daemon.serve())


if __name__ == "__main__":
    main()
