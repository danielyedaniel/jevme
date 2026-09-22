"""Jevme entry point: menu-bar app + floating pill + speech → router → actions."""
from __future__ import annotations

import logging
import os

import objc
from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory, NSMenu, NSMenuItem, NSStatusBar,
                    NSVariableStatusItemLength, NSImage)
from Foundation import NSObject, NSTimer
from PyObjCTools import AppHelper

from . import config
from .jev import JevClient
from .overlay import Overlay
from .router import Router
from .speech import SpeechEngine
import time
from . import actions as A
from .generator import Generator
from .agent import Agent, compose_with_claude
from . import learned as L
from . import ax
import threading
from AppKit import NSWorkspace

log = logging.getLogger("jevme")


def dispatch_main(fn, args):
    AppHelper.callAfter(fn, *args)


class AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, note):
        self.overlay = Overlay.alloc().init()
        self.overlay.toggle_handler = self.toggle_listening
        self._status_item()

        try:
            self.jev = JevClient()
        except RuntimeError as e:
            self.overlay.setTranscript_(str(e))
            self.overlay.flashError_("no API key")
            log.error("%s", e)
            return

        # Without Accessibility jevme hears you but can't read or click anything, and nothing says why.
        import ApplicationServices as AS
        if not AS.AXIsProcessTrusted():
            log.warning("Accessibility permission missing; run ./run.sh doctor")
            self.overlay.flashError_("no Accessibility access: run ./run.sh doctor")
        n = L.load_all()
        log.info("loaded %d learned tools", n)
        self.generator = Generator(self.jev)
        log.info("codegen provider: %s", self.generator.provider)
        self.agent = Agent(self.jev, compose=compose_with_claude)
        # Ordered task queue: units run one at a time on a worker, so commands execute in the order
        # spoken even while the user keeps talking. Unit = ("tool", name, args, label) | ("route", clause).
        import collections
        self.task_q = collections.deque()
        self.task_lock = threading.Lock()
        self.task_worker = None
        self.task_running = False
        self.cancel_epoch = 0
        self.router = Router(self.jev, on_preview=self._on_preview, on_action=self._on_action,
                             on_error=self._on_error, dispatch_main=dispatch_main, on_learn=self._on_learn,
                             on_general=self._on_general, on_cancel=self._on_cancel, on_plan=self._on_plan,
                             on_commit=self._on_commit, on_stream=self._on_stream)
        self.router.agent = self.agent
        self.router.learn_now = self._run_learn
        from .watch import Watcher
        self.watcher = Watcher(on_learned=lambda g: self.overlay.flashAction_(f"learned: {g[:40]}"))
        self.router.on_failed = self._on_failed
        self.speech = SpeechEngine(on_partial=self._on_partial, on_level=self.overlay.setLevel_,
                                   on_session_reset=self.router.reset_session, on_status=self._on_status)
        self.listening = False
        self.overlay.setTranscript_("Say something…")
        self.speech.authorize(self._authorized)
        self._warm_all_apps()
        self._warmed_front = ""
        self.tick_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.15, self, "tick:", None, True)

    @objc.python_method
    def _warm_all_apps(self):
        """Switch on Chromium/Electron accessibility for everything already running, in the background."""
        def work():
            for app in NSWorkspace.sharedWorkspace().runningApplications():
                if app.activationPolicy() == 0:
                    ax.warm(int(app.processIdentifier()))
        threading.Thread(target=work, daemon=True, name="warm-all").start()

    # ---------- menu bar ----------

    @objc.python_method
    def _status_item(self):
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_("waveform", "Jevme")
        if img is not None:
            img.setTemplate_(True)
            self.item.button().setImage_(img)
        else:
            self.item.button().setTitle_("jevme")
        menu = NSMenu.alloc().init()
        self.menu_toggle = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Pause listening", "menuToggle:", "")
        self.menu_toggle.setTarget_(self)
        menu.addItem_(self.menu_toggle)
        show = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Show pill", "showPill:", "")
        show.setTarget_(self)
        menu.addItem_(show)
        menu.addItem_(NSMenuItem.separatorItem())
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit Jevme", "terminate:", "q")
        menu.addItem_(quit_item)
        self.item.setMenu_(menu)

    def menuToggle_(self, sender):
        self.toggle_listening()

    def showPill_(self, sender):
        self.overlay.panel.orderFrontRegardless()

    # ---------- lifecycle ----------

    @objc.python_method
    def _authorized(self, ok: bool):
        if not ok:
            self.overlay.setTranscript_("Speech recognition not allowed. System Settings ▸ Privacy ▸ Speech Recognition.")
            self.overlay.flashError_("no permission")
            return
        try:
            self.speech.start()
            self.listening = True
            self.overlay.setListening_(True)
            self.overlay.setTranscript_("")
        except Exception as e:  # noqa: BLE001
            log.exception("speech start failed")
            self.overlay.setTranscript_(f"Mic error: {e}")
            self.overlay.flashError_("mic")

    @objc.python_method
    def toggle_listening(self):
        if not hasattr(self, "speech"):
            return
        if self.listening:
            self.speech.stop()
            self.listening = False
            self.overlay.setListening_(False)
            self.menu_toggle.setTitle_("Resume listening")
        else:
            self.speech.start()
            self.listening = True
            self.overlay.setListening_(True)
            self.overlay.setTranscript_("")
            self.menu_toggle.setTitle_("Pause listening")

    def tick_(self, timer):
        if not getattr(self, "listening", False):
            return
        # Never act while the screen is locked: drop whatever was heard and say so in the pill.
        now = time.monotonic()
        if now - getattr(self, "_lock_checked", 0) > 1.5:
            self._lock_checked = now
            locked = A.screen_locked()
            if locked != getattr(self, "_locked", False):
                self._locked = locked
                self.overlay.setTranscript_("screen locked" if locked else "")
                if locked:
                    self.router.reset_session()
        if getattr(self, "_locked", False):
            self.router.reset_session()
            return
        # Warm the app the moment it comes to front, so its tree is ready before a command lands.
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is not None:
            name = str(front.localizedName() or "")
            if name and name != self._warmed_front:
                self._warmed_front = name
                ax.warm(int(front.processIdentifier()))
        self._refresh_screen_vocab()
        self.router.tick()
        self.watcher.tick()
        self.speech.maybe_roll_session(pending_empty=(self.router.pending_text() == ""))

    # ---------- callbacks ----------

    @objc.python_method
    def _on_partial(self, text: str, is_final: bool):
        self.router.on_partial(text, is_final)
        self.overlay.setTranscript_(self.router.focus_text() or text)

    @objc.python_method
    def _on_preview(self, label):
        if label:
            self.overlay.setPreview_(label)
        else:
            self.overlay.clearPreview()

    @objc.python_method
    def _on_action(self, label: str):
        self.overlay.flashAction_(label)

    @objc.python_method
    def _on_error(self, label: str):
        self.overlay.flashError_(label)

    @objc.python_method
    def _on_learn(self, utterance: str):
        """No tool fits: queue a codegen unit, so the new tool is written and run in its spoken order."""
        self._enqueue(("learn", utterance))

    @objc.python_method
    def _run_learn(self, utterance: str, prog):
        """On the queue worker: have Claude write a tool, gate it, then run it (after a spoken yes if risky).
        Runs synchronously so the next queued command waits and never interleaves with it."""
        prog("writing a tool…")
        try:
            g = self.generator.generate(utterance)
        except PermissionError as e:
            dispatch_main(self._learn_failed, (str(e),))
            return
        except Exception as e:  # noqa: BLE001
            log.exception("codegen failed")
            dispatch_main(self._learn_failed, (f"couldn't write it: {e.__class__.__name__}",))
            return
        spec, args = g.spec, g.args_now
        L.register(spec)
        L.save(spec)
        log.info("learned %s (%s, review %.2f, %d attempt(s), %d ms via %s) args_now=%s",
                 spec.name, spec.kind, g.review, g.attempts, g.latency_ms, g.provider, args)
        dispatch_main(self.router.learning_done, ())
        label = spec.render_label(args)
        if spec.risky:
            def run():
                try:
                    dispatch_main(self._on_action, (spec.run(args) or label,))
                except Exception as e:  # noqa: BLE001
                    log.exception("learned tool failed")
                    dispatch_main(self._on_error, (f"{spec.name}: {str(e)[:60]}",))
            dispatch_main(self.router.ask_confirmation, (label, run))
            return
        try:
            dispatch_main(self._on_action, (spec.run(args) or label,))
        except Exception as e:  # noqa: BLE001
            log.exception("learned tool failed")
            dispatch_main(self._on_error, (f"{spec.name}: {str(e)[:60]}",))

    # ---------- ordered task queue ----------

    @objc.python_method
    def _enqueue(self, unit):
        # "close Messages" while the agent is working in Messages means: stop that task. Without this the
        # quit waited in line while the agent kept typing into the app (seen in the log).
        if (unit[0] == "tool" and unit[1] == "quit_app" and getattr(self.agent, "running", False)
                and (unit[2].get("app") or "").lower() == (self.agent.app or "").lower()):
            log.info("quitting the app the agent is working in: stopping the agent")
            self.agent.cancel()
        # A new command ends any demonstration being watched (it's what the user did before speaking).
        # Synchronously, before the command's own clicks could be recorded as part of it.
        from Foundation import NSThread
        if NSThread.isMainThread():
            self._end_demo(True)
        else:
            dispatch_main(self._end_demo, (True,))
        # Each unit is stamped with the cancel epoch it was queued in. "stop" bumps the epoch, so it drops
        # everything queued so far and nothing that comes after — no flag left behind to eat the next command.
        with self.task_lock:
            self.task_q.append((self.cancel_epoch, unit))
            if not self.task_running:          # decided under the lock: a unit can't be stranded between
                self.task_running = True       # the worker's last check and its thread exiting
                self.task_worker = threading.Thread(target=self._drain, daemon=True, name="taskq")
                self.task_worker.start()

    @objc.python_method
    def _drain(self):
        prog = lambda s: dispatch_main(self.overlay.setPreview_, (s,))
        while True:
            with self.task_lock:
                if not self.task_q:
                    self.task_running = False
                    break
                epoch, unit = self.task_q.popleft()
            if epoch != self.cancel_epoch:
                continue
            try:
                kind = unit[0]
                if kind == "tool":
                    _, name, args, label = unit
                    self.router.run_tool(name, args, label, prog)
                    with self.task_lock:
                        more = bool(self.task_q)
                    if more and self.router.is_settle_tool(name):
                        from . import see
                        see.wait_for_screen(timeout=4.0)
                        time.sleep(0.2)
                elif kind == "learn":
                    self._run_learn(unit[1], prog)
                else:  # ("route", clause)
                    self.router.execute_clause(unit[1], prog)
            except Exception as e:  # noqa: BLE001
                log.exception("task failed")
                dispatch_main(self.overlay.flashError_, (str(e)[:50],))
        dispatch_main(self.overlay.clearPreview, ())

    @objc.python_method
    def _on_commit(self, tool_name, args, label):
        self._enqueue(("tool", tool_name, args, label))

    @objc.python_method
    def _on_stream(self, clause):
        self._enqueue(("route", clause))

    @objc.python_method
    def _on_general(self, goal):
        self._enqueue(("route", goal))

    @objc.python_method
    def _on_plan(self, clauses):
        for c in clauses:
            self._enqueue(("route", c))

    @objc.python_method
    def _on_cancel(self):
        with self.task_lock:
            self.task_q.clear()
            self.cancel_epoch += 1
        self.agent.cancel()          # stops a running agent task; the agent resets this on its next run
        self.watcher.stop(save=False)  # "stop" also means: don't learn what I'm doing now
        self.overlay.flashError_("stopped")

    @objc.python_method
    def _refresh_screen_vocab(self):
        """When the front window changes, teach the recognizer the names on it ("Two Sum", channel names)."""
        now = time.monotonic()
        if getattr(self, "_vocab_busy", False) or now - getattr(self, "_vocab_checked", 0.0) < 0.7:
            return
        self._vocab_checked = now
        key = (A.frontmost_app(), A.front_window_title())
        if key == getattr(self, "_vocab_key", None) or not key[0] or key[0] == "loginwindow":
            return
        self._vocab_key = key
        self._vocab_busy = True

        def work():
            try:
                from . import vocab
                snap = ax.snapshot(budget_s=0.3, app_name=key[0])
                if vocab.set_screen(vocab.screen_labels_from(snap)):
                    log.info("speech vocabulary: %d names from %s", len(vocab._screen), key[0])
            except Exception as e:  # noqa: BLE001
                log.debug("screen vocab failed: %s", e)
            finally:
                self._vocab_busy = False
        threading.Thread(target=work, daemon=True, name="vocab").start()

    @objc.python_method
    def _on_failed(self, goal: str, app0: str, why: str):
        """The agent couldn't do it. Watch the user do it by hand and keep that as the recipe."""
        self.overlay.flashError_(f"{why[:30]} · show me, I'll learn it")
        self.watcher.start(goal, app0)

    @objc.python_method
    def _end_demo(self, save: bool):
        if self.watcher.active:
            self.watcher.stop(save=save)

    @objc.python_method
    def _learn_failed(self, msg: str):
        self.router.learning_done()
        self.overlay.clearPreview()
        self.overlay.flashError_(msg)

    @objc.python_method
    def _on_status(self, s: str):
        log.info("status: %s", s)


def main() -> None:
    logging.basicConfig(level=os.environ.get("JEVME_LOG", "INFO"),
                        format="%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s %(message)s", datefmt="%H:%M:%S")
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
