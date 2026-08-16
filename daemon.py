#!/usr/bin/env python3
"""
Claude Code Macropad daemon.

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

The HID handle is only held open while at least one Claude Code
session is active — see Daemon._reconcile_pad(). It's released
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
daemon also seeds slots for any Claude Code sessions that were
already running (e.g. a daemon restart mid-session) via `claude
agents --json` — see discover_running_sessions() and
Daemon.seed_existing_sessions() below. Requires Claude Code >=2.1.224;
an older CLI (or no `claude` on PATH) just means seeding finds nothing
and pre-existing sessions fall back to the pre-existing
lazy-allocation-on-first-hook-event behavior instead.
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

from pad_link import HidPadLink

# --- Config ------------------------------------------------------------

# Shared with hook.sh (which also drops itself here, see README's "Wire
# up real Claude Code hooks" step) so everything the daemon owns on
# disk lives under one directory instead of scattered directly in the
# home directory. Created eagerly since a standalone daemon run (e.g.
# via fake_hooks.py, before hook.sh has ever been copied anywhere) is
# the first thing to need it, both for the socket bind below and for
# the events-log handler created at import time further down.
CONFIG_DIR = Path(os.path.expanduser("~/.claude-macropad"))
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

# Tools where Claude is blocked waiting on the user, but nothing has
# gone wrong — distinct from an actual error, and distinct from a
# generic "working" tool call. Notification's "waiting" (permission
# prompts) stays separate too: those are lower-urgency than these.
ATTENTION_TOOLS = {"AskUserQuestion", "ExitPlanMode"}


def hook_to_state(event_name, tool_name=None, notification_type=None):
    """Pad state a given hook event maps to, or None if this hook
    doesn't change display state by itself (e.g. SubagentStop).

    AskUserQuestion and ExitPlanMode are special-cased: both are
    PreToolUse events, but both mean Claude is blocked on the user
    making a choice, not just "running a tool" — worth a visually
    distinct (and blinking, on the pad side) state.

    Notification is also special-cased by subtype: agent_needs_input
    means Claude is fully stalled until you answer, same urgency as
    AskUserQuestion, so it maps to "question" too. idle_prompt
    (Claude's been idle 60s+) is lower-stakes and stays "waiting".
    permission_prompt is deliberately NOT handled here — confirmed
    unreliable in practice (never fired for a real "Allow this
    command?" prompt, even on a current Claude Code version).
    PermissionRequest, handled separately below, is the working
    replacement for that specific case.
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
        if notification_type == "agent_needs_input":
            return "question"
        if notification_type == "idle_prompt":
            return "waiting"
        return None  # other subtypes (auth_success, elicitation_*, ...) — no state change

    return {
        "SessionStart": "idle",
        "UserPromptSubmit": "working",
        "PreToolUse": "tool_running",
        "PostToolUse": "working",
        "PostToolUseFailure": "error",
        "Stop": "done",
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

    def slot_for(self, session_id):
        with self.lock:
            return self.session_to_slot.get(session_id)


# --- Existing session discovery -----------------------------------------
#
# A restarted daemon (or one started after Claude Code sessions are
# already open) otherwise has no slots until each pre-existing session
# happens to fire some hook event — see the lazy-allocation fallback
# in handle_hook_event, added for exactly this case. `claude agents
# --json` (Claude Code >=2.1.224) prints every currently-active
# session — interactive and background — as a JSON array with at
# least pid/cwd/sessionId, letting the daemon seed slots for all of
# them at startup instead of waiting. Best-effort: an older CLI, no
# `claude` on PATH, or any parse failure just means an empty list,
# same headless-friendly posture as pad discovery elsewhere in this
# file.

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
        """
        if delay:
            await asyncio.sleep(delay)
        async with self._pad_lock:
            want_open = len(self.slots.session_to_slot) > 0
            if want_open and not self.pad.attached:
                await asyncio.to_thread(self.pad.open)
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
    # A key press does exactly one thing — bring that session's window
    # to the front. Dispatch mechanisms, tried in order:
    #   1. tmux select-window, if a pane was recorded for this session
    #   2. Terminal.app tab activation, matched by exact controlling
    #      tty — only ever populated at SessionStart (see hook.sh)
    #   3. VS Code window activation via AppleScript, matched by
    #      project folder name
    #   4. IntelliJ IDEA window activation, same project-name match —
    #      tried after VS Code since both are gated on the same
    #      `project` value and either/neither may actually be running
    def on_device_event(self, msg):
        t = msg.get("t")
        if t == "key":
            i = msg.get("i")
            valid_index = i is not None and i < self.slots.num_slots
            session_id = self.slots.slot_to_session[i] if valid_index else None
            log.info("key %s -> session %s", i, session_id)
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
        else:
            log.info("unhandled device event: %s", msg)

    def dispatch_bring_to_front(self, slot, session_id):
        pane = self.session_panes.get(session_id)
        if pane and self._dispatch_tmux(slot, pane):
            return

        tty = self.session_ttys.get(session_id)
        if tty and self._dispatch_terminal(slot, tty):
            return

        project = self.session_projects.get(session_id)
        if project and self._dispatch_vscode(slot, project):
            return
        if project and self._dispatch_intellij(slot, project):
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
    # by hook.sh with no transformation. hook_event_name is provided by
    # Claude Code itself in every event's payload (confirmed in the
    # official hooks reference) — no need to inject it, unlike the
    # brief's original jq one-liner assumed.
    def handle_hook_event(self, payload):
        event_name = payload.get("hook_event_name")
        session_id = payload.get("session_id")

        # Log every event unconditionally, before any mapping logic.
        # Without this, a Notification that arrives but maps to
        # state=None (unrecognized notification_type, or the field
        # missing entirely) is completely silent — you'd see nothing
        # in the logs and have no way to tell "event never arrived"
        # apart from "event arrived but didn't map to a state change".
        log.info(
            "hook event: %s (session=%s tool=%s notification_type=%s)",
            event_name,
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
                    "MAPPED slot=%s state=idle pane=%s tty=%s project=%s (SessionStart)",
                    i, pane or "<none>", tty or "<none>", label,
                )
            return

        if event_name == "SessionEnd":
            i = self.slots.free(session_id)
            self._kick_reconcile(delay=self.IDLE_CLOSE_GRACE_SECONDS)
            self.pending_calls.pop(session_id, None)
            self.session_panes.pop(session_id, None)
            self.session_ttys.pop(session_id, None)
            self.session_projects.pop(session_id, None)
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
        """Pre-populate slots from `claude agents --json` (see
        discover_running_sessions() above) so sessions already running
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
        sessions = discover_running_sessions()
        for s in sessions:
            session_id = s.get("sessionId")
            if not session_id:
                continue
            i = self.slots.allocate(session_id)
            if i is None:
                continue
            label = Path(s.get("cwd", "")).name or session_id[:8]
            self.session_projects[session_id] = label

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
                "MAPPED slot=%s state=idle pane=%s tty=%s project=%s (seeded at startup)",
                i, self.session_panes.get(session_id, "<none>"), tty or "<none>", label,
            )
        if sessions:
            log.info(
                "seeded %d pre-existing session(s) from `claude agents --json`",
                len(sessions),
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
        requirement. Any session mappings already recorded in the old
        SlotManager are intentionally discarded: this only ever runs
        once, before start_unix_server in serve(), so there aren't
        any yet.
        """
        if handshake and handshake.get("slots"):
            self.slots = SlotManager(handshake["slots"])
            log.info(
                "pad handshake reports %d slot(s) — resized SlotManager accordingly",
                handshake["slots"],
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
