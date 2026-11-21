"""NSApplication setup: accessory app, status item, menus, hotkeys, Settings placeholder.

Threading rule: everything in this module runs on the main thread. Work that may
block (Keychain, later capture/OCR/HTTP) runs elsewhere and comes back through
AppHelper.callAfter; the hotkey tap has its own thread (peek.event_tap).
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSApplicationDidChangeScreenParametersNotification,
    NSEventModifierFlagCommand,
    NSEventModifierFlagShift,
    NSImageLeading,
    NSImageOnly,
    NSMenu,
    NSMenuItem,
    NSObject,
    NSRunLoop,
    NSRunLoopCommonModes,
    NSStatusBar,
    NSSwitch,
    NSTimer,
    NSVariableStatusItemLength,
    NSWorkspace,
    NSWorkspaceDidWakeNotification,
)
from Foundation import NSBundle, NSNotificationCenter
from PyObjCTools import AppHelper

from peek import BUNDLE_ID, __version__, debug, permissions, store
from peek.capture import target_displays
from peek.controller import Controller, Effect, Outcome, Phase
from peek.event_tap import HotkeyTap, describe_grant
from peek.geometry import Rect
from peek.hotkeys import Family
from peek.icons import StatusIcon
from peek.layout import LayoutInput, layout, layout_work
from peek.log import entry_point
from peek.menu import StatusMenu
from peek.ocr import Recognizer
from peek.onboarding_ui import OnboardingController
from peek.overlay import Overlay, ink_height, measure
from peek.pipeline import Pipeline, PipelineResult, SolveConfig, analyze
from peek.providers import best_model, effective_model
from peek.providers.base import ProviderError
from peek.providers.catalog import PROVIDERS
from peek.providers.catalog import spec as provider_spec
from peek.providers.oauth_chatgpt import USAGE_URL, ChatGPTCredential, ChatGPTSession
from peek.settings_ui import PROVIDER as PROVIDER_PANE
from peek.settings_ui import SettingsController
from peek.status import DotColor, HeaderStatus, IconState, last_capture_line, ready_status

log = logging.getLogger(__name__)

NOT_CONNECTED = HeaderStatus("Add your Anthropic API key in Settings", DotColor.NEUTRAL)


def not_connected(spec) -> HeaderStatus:
    if spec.key_account is None:
        return HeaderStatus(f"Sign in with {spec.name} in Settings", DotColor.NEUTRAL)
    return HeaderStatus(f"Add your {spec.vendor} API key in Settings", DotColor.NEUTRAL)


NO_QUESTIONS = HeaderStatus("No questions found on that screen", DotColor.NEUTRAL)
NO_QUESTIONS_LABEL_S = 3.0
NOTHING_PLACEABLE = HeaderStatus("The answers couldn't be placed on screen", DotColor.DANGER)
NEEDS_INPUT_MONITORING = HeaderStatus("Hotkeys need Input Monitoring", DotColor.DANGER)
NEEDS_RELAUNCH = HeaderStatus("Reopen Peek to turn on hotkeys", DotColor.DANGER)
NEEDS_SCREEN_RECORDING = HeaderStatus("Capture needs Screen Recording", DotColor.DANGER)
CAPTURE_PROBLEMS = {
    "permission": NEEDS_SCREEN_RECORDING,
    "timeout": HeaderStatus("Capture timed out; try again", DotColor.DANGER),
    "no_display": HeaderStatus("That display could not be captured", DotColor.DANGER),
    "failed": HeaderStatus("Capture failed; see the log", DotColor.DANGER),
}
# Provider errors show their own (safe) message; these kinds keep the red icon until
# the next capture or a settings change.
HOTKEY_SYMBOL = {Family.CONTROL: "⌃⌃", Family.COMMAND: "⌘⌘", Family.SHIFT: "⇧⇧"}
INPUT_MONITORING_POLL_S = 2.0


def _menu_item(title, action, key="", mask=None, target=None):
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
    if mask is not None:
        item.setKeyEquivalentModifierMask_(mask)
    if target is not None:
        item.setTarget_(target)
    return item


def build_main_menu(target) -> NSMenu:
    """A main menu is never visible in an accessory app, but AppKit still routes
    key equivalents through it: without an Edit menu ⌘V does not paste into the
    API-key fields, and ⌘W / ⌘Q do nothing while Settings is focused."""
    cmd = NSEventModifierFlagCommand
    main = NSMenu.alloc().initWithTitle_("MainMenu")

    def submenu(title: str, items: list) -> None:
        holder = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        menu = NSMenu.alloc().initWithTitle_(title)
        for item in items:
            menu.addItem_(item)
        holder.setSubmenu_(menu)
        main.addItem_(holder)

    submenu(
        "Peek",
        [
            _menu_item("About Peek", "orderFrontStandardAboutPanel:"),
            NSMenuItem.separatorItem(),
            _menu_item("Settings…", "openSettings:", ",", cmd, target),
            NSMenuItem.separatorItem(),
            _menu_item("Hide Peek", "hide:", "h", cmd),
            _menu_item("Quit Peek", "terminate:", "q", cmd),
        ],
    )
    submenu(
        "Edit",
        [
            _menu_item("Undo", "undo:", "z", cmd),
            _menu_item("Redo", "redo:", "z", cmd | NSEventModifierFlagShift),
            NSMenuItem.separatorItem(),
            _menu_item("Cut", "cut:", "x", cmd),
            _menu_item("Copy", "copy:", "c", cmd),
            _menu_item("Paste", "paste:", "v", cmd),
            _menu_item("Select All", "selectAll:", "a", cmd),
        ],
    )
    submenu(
        "Window",
        [
            _menu_item("Minimize", "performMiniaturize:", "m", cmd),
            _menu_item("Close", "performClose:", "w", cmd),
        ],
    )
    return main


def _build_identity() -> str:
    """Changes whenever the bundle's executable is rebuilt (alias or release)."""
    exe = NSBundle.mainBundle().executablePath() or sys.executable
    try:
        stamp = os.stat(exe).st_mtime_ns
    except OSError:
        stamp = 0
    return f"{exe}@{stamp}"


def bring_to_front(window) -> None:
    app = NSApplication.sharedApplication()
    if hasattr(app, "activate"):  # macOS 14+
        app.activate()
    else:
        app.activateIgnoringOtherApps_(True)
    window.makeKeyAndOrderFront_(None)


PHASE_ICON = {
    Phase.IDLE: IconState.IDLE,
    Phase.WORKING: IconState.WORKING,
    Phase.READY: IconState.READY,
    Phase.ERROR: IconState.ERROR,
}


class AppDelegate(NSObject):
    # --- setup --------------------------------------------------------------

    @objc.python_method
    def configure(
        self, *, debug: bool, self_test_captures: int = 0, self_test_solve: bool = False
    ) -> None:
        self.debug = debug
        self.self_test_solve = self_test_solve
        self.settings = store.load_settings()
        # One credential cache per provider (an API key in the Keychain); `key_cache`
        # is the current provider's.
        self.key_caches = {
            pid: store.SecretCache(spec.key_account)
            for pid, spec in PROVIDERS.items()
            if spec.key_account
        }
        self.chatgpt = ChatGPTSession()  # Sign in with ChatGPT; tokens in the Keychain
        self.key_caches["chatgpt"] = ChatGPTCredential(self.chatgpt)
        self.has_key: bool | None = None  # current provider; unknown until read
        self.settings_controller: SettingsController | None = None
        self.onboarding: OnboardingController | None = None
        self._onboarding_panes: SettingsController | None = None  # its Provider pane
        self.last_answers: list[str] = []
        self._no_questions_token = 0
        self.overlay: Overlay | None = None
        self.ctl = Controller()  # phase, generation, visibility (peek/controller.py)
        self._last_result: PipelineResult | None = None  # to redraw at a new text size
        # Hard mode as last chosen, while it could not be saved: settings.json on
        # disk still holds the old value, and a reload must not bring it back.
        self._unsaved_hard_mode: bool | None = None
        self.self_test_remaining = self_test_captures
        self.self_test_samples: list[dict] = []
        self.pipeline: Pipeline | None = None
        self.last_ocr: str | None = None
        self._capture_problem: HeaderStatus | None = None
        self.status_item = None
        self.icon = None
        self.menu = None
        self.last_capture = None  # (question_count, time.time()) or None
        self.save_problem: str | None = None  # Save questions: why the last save failed
        self.diag = {"keychain": "not run"}
        self.diag_items = {}
        self.hotkeys: HotkeyTap | None = None
        self.input_monitoring_timer = None
        self.last_hotkey: str | None = None
        self._status = not_connected(self.spec)  # until the key check says otherwise
        self._hotkey_problem: HeaderStatus | None = None

    @entry_point
    def applicationDidFinishLaunching_(self, _notification):
        NSApplication.sharedApplication().setMainMenu_(build_main_menu(self))

        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        button = self.status_item.button()
        button.setToolTip_("Peek")
        self.icon = StatusIcon(button)

        self.menu = StatusMenu(self, delegate=self)
        self._apply_hard_mode()
        self._apply_status()
        if self.debug:
            self.menu.add_diagnostics(self._build_diagnostics())
        self.status_item.setMenu_(self.menu.menu)

        bundle = NSBundle.mainBundle()
        log.info(
            "Peek %s started: bundle id %s (expected %s), executable %s, python %s, macOS %s",
            __version__,
            bundle.bundleIdentifier(),
            BUNDLE_ID,
            bundle.executablePath(),
            sys.executable,
            platform.mac_ver()[0],
        )
        log.info(
            "Permissions at launch: screen recording %s, input monitoring %s (%s)",
            permissions.screen_recording_granted(),
            permissions.input_monitoring_granted(),
            permissions.input_monitoring_state(),
        )
        NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(
            self, "didWake:", NSWorkspaceDidWakeNotification, None
        )
        self.pipeline = Pipeline()
        self.overlay = Overlay()
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, "screensChanged:", NSApplicationDidChangeScreenParametersNotification, None
        )
        self._check_key_in_background()
        if not self.settings.onboarding_done and not (
            self.self_test_solve or self.self_test_remaining
        ):
            AppHelper.callLater(0.3, entry_point(self._show_onboarding))
        if self.self_test_solve:
            AppHelper.callLater(0.5, entry_point(self._self_test_solve))
        elif self.self_test_remaining:
            AppHelper.callLater(1.5, entry_point(self._self_test_next))
        else:
            self._start_hotkeys_when_permitted()

    @entry_point
    def applicationWillTerminate_(self, _notification):
        if self.hotkeys is not None:
            self.hotkeys.stop()
        self._save_hard_mode()  # last chance for a choice that could not be saved
        log.info("Peek quitting")

    # --- status line ----------------------------------------------------------

    @objc.python_method
    def _apply_status(self) -> None:
        self.menu.set_status(self._hotkey_problem or self._capture_problem or self._status)

    # --- provider (Claude) ------------------------------------------------------

    @property
    def provider(self) -> str:
        """The provider chosen in Settings (Claude unless another is set up)."""
        p = self.settings.provider
        return p if p in self.key_caches else "claude"

    @property
    def spec(self):
        return provider_spec(self.provider)

    @property
    def key_cache(self):
        return self.key_caches[self.provider]

    @objc.python_method
    def _selected_model(self, provider: str | None = None) -> str:
        """The model chosen in Settings for *provider* (default: the current one)."""
        provider = provider or self.provider
        spec = provider_spec(provider)
        model = self.settings.model_for(provider)
        listed = self.settings.model_lists.get(provider) or []
        if spec.accepts_model(model) or (model and model in listed):
            return model
        # Nothing valid saved: the provider's default, as its Settings panel shows it.
        # The account's first model only when the default is not one it can use.
        default = spec.default_model
        usable = spec.accepts_model(default) or default in listed
        return listed[0] if listed and not spec.fixed_models and not usable else default

    @objc.python_method
    def _model(self) -> str:
        """The model captures use: the provider's best one while Hard mode is on."""
        return effective_model(
            self.provider,
            self._selected_model(),
            self.settings.hard_mode,
            self.settings.model_lists.get(self.provider),
        )

    @objc.python_method
    def _model_name(self, model: str | None = None) -> str:
        return self.spec.display_name(model or self._model())

    @objc.python_method
    def _apply_hard_mode(self) -> None:
        self.menu.set_hard_mode(
            self.settings.hard_mode,
            self._model_name(
                best_model(self.provider, self.settings.model_lists.get(self.provider))
            ),
        )

    @entry_point
    def chooseProvider_(self, sender):
        self._switch_provider(str(sender.representedObject()))

    @objc.python_method
    def _switch_provider(self, pid: str) -> None:
        """The menu's quick switch: the same as picking *pid* in Settings. Captures
        then use the model chosen for it there (or its best one in Hard mode)."""
        if pid not in self.key_caches or pid == self.provider:
            return
        settings = store.load_settings()  # fresh: Settings may have saved meanwhile
        settings.provider = pid
        try:
            store.save_settings(settings)
        except OSError:
            log.exception("Could not save the provider choice; still using %s", self.spec.name)
            return
        log.info("Provider set to %s from the menu", provider_spec(pid).name)
        self._settings_changed()
        # Windows showing the Provider pane follow (Settings, or the Welcome window).
        for owner in (self.settings_controller, self._onboarding_panes):
            if owner is not None:
                owner.provider_changed()

    @entry_point
    def toggleHardMode_(self, sender):
        """From the switch (its new state) or from the menu row itself, chosen with
        the keyboard (Return flips the current setting)."""
        on = bool(sender.state()) if isinstance(sender, NSSwitch) else not self.settings.hard_mode
        self.settings = store.load_settings()  # fresh: Settings may have saved meanwhile
        self.settings.hard_mode = on
        self._unsaved_hard_mode = on
        self._save_hard_mode()
        log.info("Hard mode %s: captures use %s", "on" if on else "off", self._model_name())
        self._apply_hard_mode()
        self._refresh_provider_status()

    @objc.python_method
    def _save_hard_mode(self) -> None:
        """Write the pending Hard mode choice; on failure it stays pending, applies
        to every capture and is written again the next time settings change."""
        if self._unsaved_hard_mode is None:
            return
        settings = store.load_settings()
        settings.hard_mode = self._unsaved_hard_mode
        try:
            store.save_settings(settings)
        except OSError:
            log.exception(
                "Could not save Hard mode; it stays %s until Peek quits",
                "on" if self._unsaved_hard_mode else "off",
            )
            return
        self._unsaved_hard_mode = None

    @objc.python_method
    def _check_key_in_background(self) -> None:
        """Is an API key stored? Read off the main thread (the Keychain may prompt)."""

        provider, cache = self.provider, self.key_cache

        def work() -> None:
            with objc.autorelease_pool():
                try:
                    signed_in = getattr(cache, "signed_in", None)  # ChatGPT: no network
                    has_key: bool | None = signed_in() if signed_in else bool(cache.get())
                except Exception:
                    # Unknown, not "no key": a capture still tries (and reports the
                    # Keychain problem), instead of every ⌃⌃ opening Settings.
                    log.exception("Could not read the Keychain")
                    has_key = None
            AppHelper.callAfter(entry_point(self._on_key_known), has_key, provider)

        threading.Thread(target=work, name="keychain-check", daemon=True).start()

    @objc.python_method
    def _on_key_known(self, has_key: bool | None, provider: str | None = None) -> None:
        if provider is not None and provider != self.provider:
            return  # an answer about a provider no longer chosen
        self.has_key = has_key
        if self.spec.key_account is None:  # ChatGPT: a sign-in, not a key
            what, words = "sign-in", ("signed in", "signed out")
        else:
            what, words = "API key", ("stored", "not set")
        state = {True: words[0], False: words[1], None: "unknown (Keychain read failed)"}[has_key]
        log.info("%s %s %s; model %s", self.spec.vendor, what, state, self._model())
        self._refresh_provider_status()

    @objc.python_method
    def _refresh_provider_status(self) -> None:
        self._status = (
            ready_status(self._model_name()) if self.has_key else not_connected(self.spec)
        )
        self._apply_status()

    @objc.python_method
    def _settings_changed(self) -> None:
        """Called by Settings after the key or the model changed."""
        old_size, old_provider = self.settings.answer_text_size, self.provider
        old_work = (self.settings.show_work, self.settings.work_box, self.settings.work_rgb)
        self.settings = store.load_settings()
        new_work = (self.settings.show_work, self.settings.work_box, self.settings.work_rgb)
        if self.provider != old_provider:
            log.info("Provider: %s", self.spec.name)
            self.has_key = None  # the new provider's key is read below
            self.menu.set_manage_usage(False)  # that was the old provider's limit
            self._refresh_provider_status()
        if self.hotkeys is not None:
            self.hotkeys.set_gap_ms(self.settings.double_tap_ms)
        restyled = self.settings.answer_text_size != old_size or new_work != old_work
        if restyled and self._last_result and self.ctl.answers:
            # Redraw: new text size, work box or colour (Show work may be off now).
            _placed, work = self._lay_out(self._last_result)
            self._apply(self.ctl.work_changed(work))
        if self._unsaved_hard_mode is not None:
            self.settings.hard_mode = self._unsaved_hard_mode  # the user's last choice
            self._save_hard_mode()
        self._apply_hard_mode()
        self._capture_problem = None
        self._apply_status()  # the old problem goes from the header now
        self._apply(self.ctl.problem_resolved())
        self._check_key_in_background()

    # --- hotkeys --------------------------------------------------------------

    @objc.python_method
    def _start_hotkeys_when_permitted(self) -> None:
        """Create the tap only once Input Monitoring is granted (a tap created
        earlier loses its keyboard events); until then, poll for the grant."""
        if permissions.input_monitoring_granted():
            self._start_hotkeys()
            return
        log.info("Input Monitoring not granted; hotkeys wait for it")
        self._hotkey_problem = NEEDS_INPUT_MONITORING
        self._apply_status()
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            INPUT_MONITORING_POLL_S, self, "pollInputMonitoring:", None, True
        )
        # Common modes: keep polling while a menu is open.
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        self.input_monitoring_timer = timer

    @entry_point
    def pollInputMonitoring_(self, _timer):
        if not permissions.input_monitoring_granted():
            return
        log.info("Input Monitoring granted while running")
        self.input_monitoring_timer.invalidate()
        self.input_monitoring_timer = None
        self._start_hotkeys()

    @objc.python_method
    def _start_hotkeys(self) -> None:
        self.hotkeys = HotkeyTap(
            entry_point(self._on_double_tap),
            entry_point(self._on_hotkeys_started),
            gap_ms=self.settings.double_tap_ms,
        )
        self.hotkeys.start()

    @objc.python_method
    def _on_hotkeys_started(self, created: bool, keyboard_events: bool | None) -> None:
        if not created:
            self._hotkey_problem = NEEDS_RELAUNCH
        elif keyboard_events is False:
            # The grant reached the process only partly: modifier changes arrive
            # but key presses are stripped. A new process gets the full grant.
            log.warning("Hotkey tap has no keyboard events; a relaunch is needed")
            self._hotkey_problem = NEEDS_RELAUNCH
        else:
            self._hotkey_problem = None
        self._apply_status()

    @objc.python_method
    def _on_double_tap(self, family: Family) -> None:
        self.last_hotkey = f"{HOTKEY_SYMBOL[family]} at {time.strftime('%H:%M:%S')}"
        if family is Family.CONTROL:
            log.info("⌃⌃ double-tap: capture")
            self._start_capture()
        elif family is Family.SHIFT:
            self._toggle_work("⇧⇧ double-tap")
        else:
            self._toggle_answers("⌘⌘ double-tap")

    @objc.python_method
    def _toggle_answers(self, source: str) -> None:
        effects = self.ctl.toggle()
        if not effects:
            log.info("%s: no answers to show (no-op)", source)
            return
        self._apply(effects)
        log.info("%s: answers %s", source, "shown" if self.ctl.visible else "hidden")

    @objc.python_method
    def _toggle_work(self, source: str) -> None:
        effects = self.ctl.toggle_work()
        if not effects:
            log.info("%s: no working to show (no-op)", source)
            return
        self._apply(effects)
        log.info("%s: working %s", source, "shown" if self.ctl.work_visible else "hidden")

    @property
    def answers_visible(self) -> bool:
        return self.ctl.visible

    @property
    def generation(self) -> int:
        return self.ctl.generation

    @objc.python_method
    def _apply(self, effects) -> None:
        """Carry out the controller's effects; the icon follows its phase."""
        for effect in effects or ():
            if effect is Effect.CLEAR and self.overlay is not None:
                self.overlay.clear()
            elif effect is Effect.SHOW and self.overlay is not None:
                self.overlay.show()
            elif effect is Effect.HIDE and self.overlay is not None:
                self.overlay.hide()
            elif effect is Effect.SHOW_WORK and self.overlay is not None:
                self.overlay.show_work()
            elif effect is Effect.HIDE_WORK and self.overlay is not None:
                self.overlay.hide_work()
            elif effect is Effect.NO_QUESTIONS:
                self._show_no_questions()
        if self.icon is not None:
            self.icon.set_state(PHASE_ICON[self.ctl.phase])

    # --- capture (milestone 3: capture + OCR; the model call comes in milestone 4) ---

    @objc.python_method
    def _start_capture(self) -> None:
        """⌃⌃ or Capture & solve. A new capture supersedes one in flight."""
        if self.has_key is False and not self.self_test_remaining:
            log.info("Capture requested without an API key; opening Settings")
            self._open_settings(pane=PROVIDER_PANE)
            return
        if not permissions.screen_recording_granted():
            log.warning("Capture requested but Screen Recording is not granted")
            self._clear_no_questions_label()
            self._capture_problem = NEEDS_SCREEN_RECORDING
            self._apply(self.ctl.refuse_capture())
            self._apply_status()
            self._self_test_abort("Screen Recording is not granted")
            return
        targets = target_displays(self.settings.display_mode)
        if not targets:
            log.warning("No display to capture")
            self._clear_no_questions_label()
            self._self_test_abort("no display")
            return
        self._save_hard_mode()  # retry a Hard mode choice that could not be saved
        self._clear_no_questions_label()
        # Spec: answers from the previous screen disappear as soon as a new capture
        # starts. Ordering the panels out first also keeps them out of the screenshot.
        was_visible = self.overlay is not None and (
            self.overlay.visible or self.overlay.work_visible
        )
        self._apply(self.ctl.start_capture(keep_visible=self.settings.keep_answers_visible))
        self.last_answers = []
        self.last_capture = None
        generation = self.generation
        s = self.settings
        solve = (
            None
            if self.self_test_remaining
            else SolveConfig(
                self.provider,
                self._model(),
                self.key_cache,
                show_work=s.show_work,
                save_folder=s.save_questions_folder if s.save_questions else "",
            )
        )

        def submit() -> None:
            if generation != self.generation:
                return  # an even newer capture was started meanwhile
            self.pipeline.submit(
                generation,
                targets,
                entry_point(self._on_pipeline_done),
                save_debug=self.debug and not self.self_test_remaining,
                solve=solve,
            )

        if was_visible:
            # Give the window server a moment to take the answers off screen.
            AppHelper.callLater(0.05, entry_point(submit))
        else:
            submit()

    @objc.python_method
    def _on_pipeline_done(self, generation: int, result: PipelineResult) -> None:
        if generation != self.generation or self.ctl.phase is not Phase.WORKING:
            # Superseded, or the displays changed: never touch the overlay with it.
            log.info("Dropping stale result g%d (latest g%d)", generation, self.generation)
            return
        for d in result.displays:
            log.info(
                "Capture g%d display %d: %dx%d px (%.2f px/pt), %d text blocks, "
                "capture %.0f ms, OCR %.0f ms, own windows excluded by %s",
                generation,
                d.target.display_id,
                d.width,
                d.height,
                d.scale,
                len(d.blocks),
                d.capture_ms,
                d.ocr_ms,
                d.excluded_by,
            )
        if self.self_test_remaining:
            self._self_test_record(result)
            return
        if result.displays:
            d = result.displays[0]
            a = d.analysis
            self.last_ocr = (
                f"{result.block_count} text blocks, capture {d.capture_ms:.0f} ms, "
                f"OCR {a.ocr_ms:.0f} ms, model {a.solve_ms / 1000:.1f} s "
                f"at {time.strftime('%H:%M:%S')}"
            )
        questions = result.questions
        self.save_problem = next(
            (d.analysis.save_error for d in result.displays if d.analysis.save_error), None
        )
        saved = sum(d.analysis.saved for d in result.displays)
        if saved:
            log.info("Capture g%d: %d question picture(s) saved", generation, saved)
        placed, work = self._lay_out(result) if questions else (0, False)
        self._last_result = result if placed else None
        if questions and placed:
            self.last_capture = (placed, time.time())
            self.last_answers = [f"{q.id} · {q.answer}  ({q.target_kind})" for q in questions]
            self._capture_problem = None
            self.menu.set_manage_usage(False)
            self._apply(self.ctl.finish(generation, Outcome.ANSWERS, placed, work=work))
            self._refresh_provider_status()
            log.info(
                "Capture g%d: %d answers placed (%s)",
                generation,
                placed,
                "shown" if self.answers_visible else "hidden",
            )
        elif questions:
            log.warning(
                "Capture g%d: %d answers but none could be placed", generation, len(questions)
            )
            self.last_capture = None
            self._capture_problem = NOTHING_PLACEABLE
            self._apply(self.ctl.finish(generation, Outcome.UNPLACEABLE))
        elif result.errors:
            error = result.errors[0]
            if isinstance(error, ProviderError):
                self._capture_problem = HeaderStatus(str(error), DotColor.DANGER)
                current = result.provider in ("", self.provider)  # not an old provider's
                if current and error.kind in ("not_configured", "signed_out"):
                    self.has_key = False
                    if result.provider == "chatgpt":
                        self._forget_chatgpt_tokens()
                # Spec: on a ChatGPT usage limit, offer "Manage usage".
                self.menu.set_manage_usage(current and error.kind == "usage_limit")
            else:
                kind = getattr(error, "kind", "failed")
                self._capture_problem = CAPTURE_PROBLEMS.get(kind, CAPTURE_PROBLEMS["failed"])
            self.last_capture = None
            self._apply(self.ctl.finish(generation, Outcome.FAILED))
        elif result.dropped:
            # Claude answered, but nothing could be placed (no valid position).
            log.warning(
                "Capture g%d: all %d answers dropped: %s",
                generation,
                result.dropped,
                "; ".join(p for d in result.displays for p in d.analysis.problems)[:300],
            )
            self.last_capture = None
            self._capture_problem = NOTHING_PLACEABLE
            self._apply(self.ctl.finish(generation, Outcome.UNPLACEABLE))
        else:
            self.last_capture = None
            self._capture_problem = None
            self._apply(self.ctl.finish(generation, Outcome.NO_QUESTIONS))
        self._apply_status()

    @objc.python_method
    def _lay_out(self, result: PipelineResult) -> tuple[int, bool]:
        """Place every display's answers (and working) on its overlay panel; returns
        how many answers, and whether a work box was placed."""
        placed, has_work = 0, False
        for d in result.displays:
            if not d.analysis.questions:
                continue
            size = self.overlay.screen_size(d.target.display_id) or (
                d.target.frame.w,
                d.target.frame.h,
            )
            work = None
            if self.settings.show_work:
                bx, by, bw, bh = self.settings.work_box
                box = Rect(bx * size[0], by * size[1], bw * size[0], bh * size[1])
                rgb = tuple(self.settings.work_rgb)
                work = layout_work(d.analysis.questions, box, rgb, measure)
            items = layout(
                LayoutInput(
                    questions=d.analysis.questions,
                    block_rects_px={b.id: b.box for b in d.blocks},
                    block_texts={b.id: b.text for b in d.blocks},
                    styles=d.analysis.styles,
                    scale=d.scale,
                    view_width=size[0],
                    view_height=size[1],
                    fixed_font_size=self.settings.answer_text_size or None,
                    reserved=[work.rect] if work is not None else [],
                ),
                measure,
                ink_height,
            )
            # The working is not an answer: never counted as one. It is shown and
            # hidden on its own (⇧⇧), so it goes to the overlay as a layer of its own.
            work_items = [work] if work is not None and items else []
            if not self.overlay.set_items(d.target.display_id, items, work_items):
                continue  # the display went away during the capture
            placed += len(items)
            has_work = has_work or bool(work_items)
            if d.debug_dir:
                folder, scale = Path(d.debug_dir), d.scale
                threading.Thread(
                    target=_write_layout_safely,
                    args=(folder, items + work_items, scale),
                    daemon=True,
                ).start()
        return placed, has_work

    @entry_point
    def screensChanged_(self, _notification):
        """Displays added, removed or rearranged: answer positions are stale."""
        was_working = self.ctl.phase is Phase.WORKING
        effects = self.ctl.screens_changed()
        if was_working:
            # A capture in flight was taken on the old arrangement: dropped.
            log.info("Screens changed during a capture: dropping it")
            self._self_test_abort("screens changed")
        self.overlay.rebuild()  # new panels, no answers
        if effects:
            log.info("Screens changed: answers cleared")
            self.last_capture = None
            self.last_answers = []
        self._apply(effects)

    @objc.python_method
    def _show_no_questions(self) -> None:
        """Spec: a brief 'No questions found' state on the menu-bar icon, never a popup."""
        self._no_questions_token += 1
        token = self._no_questions_token
        button = self.status_item.button()
        button.setImagePosition_(NSImageLeading)  # text beside the eye, not under it
        button.setTitle_(" No questions")
        self.icon.relayout()
        self._capture_problem = NO_QUESTIONS

        def clear() -> None:
            if token == self._no_questions_token:
                self._clear_no_questions_label()
                self._apply_status()

        AppHelper.callLater(NO_QUESTIONS_LABEL_S, entry_point(clear))

    @objc.python_method
    def _clear_no_questions_label(self) -> None:
        self._no_questions_token += 1
        button = self.status_item.button()
        button.setTitle_("")
        button.setImagePosition_(NSImageOnly)
        self.icon.relayout()
        if self._capture_problem is NO_QUESTIONS:
            self._capture_problem = None

    # --- self-test (--self-test-captures N): latency and memory, no files written ---

    @objc.python_method
    def _self_test_next(self) -> None:
        self._start_capture()

    @objc.python_method
    def _self_test_solve(self) -> None:
        """--self-test-solve: the Figma worksheet, rendered offscreen (no screen
        capture), through OCR and the selected Claude model; logs the answers."""
        try:
            from tests.fixtures.worksheet import render_cgimage
        except ImportError:
            log.error("--self-test-solve needs the test worksheet (tests/fixtures), not included")
            NSApplication.sharedApplication().terminate_(None)
            return
        model = self._model()

        def work() -> None:
            try:
                run()
            except Exception:
                log.exception("Self-test solve crashed")
            finally:
                AppHelper.callAfter(NSApplication.sharedApplication().terminate_, None)

        def run() -> None:
            with objc.autorelease_pool():
                image, w, h = render_cgimage(2)
                folder = debug.new_capture_dir(0) if self.debug else None
                started = time.monotonic()
                analysis = analyze(
                    image,
                    w,
                    h,
                    recognizer=Recognizer(),
                    solve=SolveConfig(self.provider, model, self.key_cache),
                    debug_folder=folder,
                )
                elapsed = time.monotonic() - started
            if analysis.error is not None:
                log.error("Self-test solve failed (%s): %s", analysis.error.kind, analysis.error)
            for q in analysis.questions:
                log.info(
                    "Self-test answer %s: %r (%s, %s, blocks %s, box %s)",
                    q.id,
                    q.answer,
                    q.type,
                    q.target_kind,
                    q.target_block_ids or q.question_block_ids,
                    q.approx_bbox_norm,
                )
            log.info(
                "Self-test solve: %s, %d questions in %.1f s (OCR %.0f ms, model %.1f s)",
                analysis.model_used or model,
                len(analysis.questions),
                elapsed,
                analysis.ocr_ms,
                analysis.solve_ms / 1000,
            )

        threading.Thread(target=work, name="self-test-solve", daemon=True).start()

    @objc.python_method
    def _self_test_abort(self, reason: str) -> None:
        if self.self_test_remaining:
            log.error("Self-test aborted: %s", reason)
            NSApplication.sharedApplication().terminate_(None)

    @objc.python_method
    def _self_test_record(self, result: PipelineResult) -> None:
        sample = {"rss_mb": _rss_mb(), "ok": bool(result.displays)}
        if result.displays:
            d = result.displays[0]
            sample.update(
                blocks=len(d.blocks),
                capture_ms=round(d.capture_ms),
                ocr_ms=round(d.ocr_ms),
                total_ms=round(result.total_ms),
            )
        else:
            sample["error"] = str(result.errors[0]) if result.errors else "unknown"
        self.self_test_samples.append(sample)
        log.info("Self-test %d: %s", len(self.self_test_samples), sample)
        self.self_test_remaining -= 1
        if self.self_test_remaining > 0 and sample["ok"]:
            AppHelper.callLater(0.2, entry_point(self._self_test_next))
            return
        oks = [x for x in self.self_test_samples if x["ok"]]
        if oks:

            def median(key: str) -> float:
                values = sorted(x[key] for x in oks)
                return values[len(values) // 2]

            log.info(
                "Self-test summary: %d/%d ok; median capture %d ms, OCR %d ms, total %d ms; "
                "RSS first %.1f MB, after 5 %.1f MB, last %.1f MB",
                len(oks),
                len(self.self_test_samples),
                median("capture_ms"),
                median("ocr_ms"),
                median("total_ms"),
                self.self_test_samples[0]["rss_mb"],
                self.self_test_samples[min(4, len(self.self_test_samples) - 1)]["rss_mb"],
                self.self_test_samples[-1]["rss_mb"],
            )
        NSApplication.sharedApplication().terminate_(None)

    @entry_point
    def didWake_(self, _notification):
        # Key-ups can be lost across sleep; start the next gesture from scratch.
        if self.hotkeys is not None:
            self.hotkeys.request_reset()

    # --- NSMenuDelegate -----------------------------------------------------

    @entry_point
    def menuNeedsUpdate_(self, _menu):
        self._refresh_menu()

    @objc.python_method
    def _refresh_menu(self) -> None:
        if self.last_capture is None:
            self.menu.set_last_capture(None)
        else:
            # Wall-clock time: time.monotonic() stops while the Mac sleeps.
            count, at = self.last_capture
            line = last_capture_line(count, time.time() - at)
            if self.save_problem:
                line += " · not saved"  # Save questions failed (details in the log)
            self.menu.set_last_capture(line)
        has_answers = self.overlay is not None and self.overlay.has_answers
        self.menu.set_answers(available=has_answers, visible=self.answers_visible)
        self.menu.set_work(
            shown=self.settings.show_work or self.ctl.work,
            available=self.ctl.work,
            visible=self.ctl.work_visible,
        )
        self.menu.set_providers(
            self.provider,
            {
                pid: provider_spec(pid).display_name(self._selected_model(pid))
                for pid in self.key_caches
            },
        )
        if self.diag_items:
            self._refresh_diagnostics()

    # --- menu actions -------------------------------------------------------

    @entry_point
    def captureAndSolve_(self, _sender):
        log.info("Capture & solve chosen from the menu")
        self._start_capture()

    @objc.python_method
    def _show_onboarding(self) -> None:
        if self.onboarding is None:
            pane_owner = SettingsController.alloc().init()
            pane_owner.configure(
                key_caches=self.key_caches,
                on_change=entry_point(self._settings_changed),
                on_key_known=entry_point(self._on_key_known),
            )
            self._onboarding_panes = pane_owner  # keeps the pane's controls alive
            self.onboarding = OnboardingController.alloc().init()
            self.onboarding.configure(
                build_provider_pane=pane_owner.provider_pane,
                on_done=entry_point(self._onboarding_done),
                relaunch=lambda: permissions.relaunch(debug=self.debug),
            )
        log.info("Showing onboarding")
        self.onboarding.show(bring_to_front)

    @objc.python_method
    def _onboarding_done(self) -> None:
        settings = store.load_settings()
        settings.onboarding_done = True
        try:
            store.save_settings(settings)
        except OSError:
            log.exception("Could not save that onboarding is done")
        self.settings = settings
        log.info("Onboarding finished")  # hotkeys already wait for their permission

    @objc.python_method
    def _forget_chatgpt_tokens(self) -> None:
        """OpenAI rejected the token set (401, or a refresh that ended the session):
        clear it, so Settings shows "Continue with ChatGPT" rather than a sign-in
        that no longer works. The registration is kept."""
        session = self.chatgpt

        def work() -> None:
            with objc.autorelease_pool():
                try:
                    session.drop_tokens()
                except Exception:
                    log.exception("Could not clear the ChatGPT tokens")

        threading.Thread(target=work, name="chatgpt-drop", daemon=True).start()

    @entry_point
    def manageUsage_(self, _sender):
        from AppKit import NSURL, NSWorkspace

        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(USAGE_URL))

    @entry_point
    def toggleAnswers_(self, _sender):
        self._toggle_answers("Show/Hide answers menu item")

    @entry_point
    def toggleWork_(self, _sender):
        self._toggle_work("Show/Hide work menu item")

    @entry_point
    def openSettings_(self, _sender):
        self._open_settings()

    @objc.python_method
    def _open_settings(self, pane: int | None = None) -> None:
        if self.settings_controller is None:
            self.settings_controller = SettingsController.alloc().init()
            self.settings_controller.configure(
                key_caches=self.key_caches,
                on_change=entry_point(self._settings_changed),
                on_key_known=entry_point(self._on_key_known),
            )
        self.settings_controller.show(bring_to_front, pane=pane)

    # --- diagnostics (only with --debug) ------------------------------------

    @objc.python_method
    def _build_diagnostics(self) -> NSMenu:
        menu = NSMenu.alloc().initWithTitle_("Diagnostics")
        menu.setAutoenablesItems_(False)

        def info(key: str) -> None:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", None, "")
            item.setEnabled_(False)
            menu.addItem_(item)
            self.diag_items[key] = item

        def action(title: str, selector: str) -> None:
            menu.addItem_(_menu_item(title, selector, target=self))

        info("screen")
        info("input")
        info("hotkeys")
        info("capture")
        info("answers")
        info("keychain")
        menu.addItem_(NSMenuItem.separatorItem())
        action("Request Screen Recording", "diagRequestScreen:")
        action("Request Input Monitoring", "diagRequestInput:")
        action("Open Screen Recording Settings", "diagOpenScreenSettings:")
        action("Open Input Monitoring Settings", "diagOpenInputSettings:")
        action("Keychain Round Trip", "diagKeychain:")
        action("Relaunch Peek", "diagRelaunch:")
        menu.addItem_(NSMenuItem.separatorItem())
        action("Cycle Icon State", "diagCycleIcon:")
        action("Simulate Capture (4 questions)", "diagSimulateCapture:")
        action("Reset to Not Connected", "diagReset:")
        self._refresh_diagnostics()
        return menu

    @objc.python_method
    def _refresh_diagnostics(self) -> None:
        def yn(granted: bool) -> str:
            return "granted" if granted else "not granted"

        items = self.diag_items
        items["screen"].setTitle_(f"Screen Recording: {yn(permissions.screen_recording_granted())}")
        items["input"].setTitle_(
            f"Input Monitoring: {yn(permissions.input_monitoring_granted())}"
            f" (HID: {permissions.input_monitoring_state()})"
        )
        items["hotkeys"].setTitle_(f"Hotkeys: {self._hotkey_summary()}")
        items["capture"].setTitle_(f"Last capture: {self.last_ocr or 'none yet'}")
        answers = "  |  ".join(self.last_answers) if self.last_answers else "none"
        items["answers"].setTitle_(f"Answers: {answers}"[:300])
        items["keychain"].setTitle_(f"Keychain: {self.diag['keychain']}")

    @entry_point
    def diagRequestScreen_(self, _sender):
        log.info("CGRequestScreenCaptureAccess -> %s", permissions.request_screen_recording())

    @entry_point
    def diagRequestInput_(self, _sender):
        permissions.request_input_monitoring()

    @entry_point
    def diagRelaunch_(self, _sender):
        permissions.relaunch(debug=self.debug)

    @objc.python_method
    def _hotkey_summary(self) -> str:
        tap = self.hotkeys
        if tap is None:
            return "waiting for Input Monitoring"
        if not tap.created:
            return "tap could not be created"
        parts = [
            "running" if tap.enabled else "DISABLED",
            f"keys {describe_grant(tap.keyboard_events_granted)}",
            f"{tap.events_seen} events",
        ]
        if tap.disabled_count:
            parts.append(f"re-enabled {tap.disabled_count}x")
        if tap.vetoed_secure_input:
            parts.append(f"{tap.vetoed_secure_input} blocked by secure input")
        if self.last_hotkey:
            parts.append(f"last {self.last_hotkey}")
        return ", ".join(parts)

    @entry_point
    def diagOpenScreenSettings_(self, _sender):
        permissions.open_system_settings(permissions.SCREEN_RECORDING_URL)

    @entry_point
    def diagOpenInputSettings_(self, _sender):
        permissions.open_system_settings(permissions.INPUT_MONITORING_URL)

    @entry_point
    def diagKeychain_(self, _sender):
        """Write, read back and delete a dummy item, off the main thread: after a
        rebuild the Keychain may ask for the login password."""
        if self.diag["keychain"] == "running…":
            return  # the first run may still be waiting on a Keychain prompt
        self.diag["keychain"] = "running…"
        build_id = _build_identity()

        def work() -> None:
            with objc.autorelease_pool():
                account = "diagnostics-roundtrip"
                value = f"peek-{time.time_ns()}"
                started = time.monotonic()
                try:
                    # The persistent item records which build wrote it, so the result
                    # shows whether a *rebuilt* app can still read an older build's item.
                    existing = store.get_secret("diagnostics-persistent")
                    store.set_secret(account, value)
                    ok = store.get_secret(account) == value
                    store.delete_secret(account)
                    store.set_secret("diagnostics-persistent", build_id)
                    if existing is None:
                        origin = "persistent item created"
                    elif existing == build_id:
                        origin = "persistent item from this build read"
                    else:
                        origin = "persistent item from an EARLIER build read"
                    result = (
                        f"{'ok' if ok else 'MISMATCH'}, {origin} "
                        f"({time.monotonic() - started:.1f}s)"
                    )
                except Exception as exc:
                    log.exception("Keychain round trip failed")
                    result = f"failed: {type(exc).__name__}"
            log.info("Keychain round trip: %s", result)
            AppHelper.callAfter(self._set_diag, "keychain", result)

        threading.Thread(target=work, name="keychain-diag", daemon=True).start()

    @objc.python_method
    @entry_point
    def _set_diag(self, key: str, value: str) -> None:
        self.diag[key] = value

    @entry_point
    def diagCycleIcon_(self, _sender):
        order = list(IconState)
        nxt = order[(order.index(self.icon.state) + 1) % len(order)]
        self.icon.set_state(nxt)
        log.info("Icon state -> %s", nxt.value)

    @entry_point
    def diagSimulateCapture_(self, _sender):
        self.last_capture = (4, time.time() - 120)
        self.ctl.phase = Phase.READY  # a pretend result: nothing on screen to toggle
        self.icon.set_state(IconState.READY)
        self._status = ready_status("Claude Sonnet 5.5")
        self._apply_status()

    @entry_point
    def diagReset_(self, _sender):
        self.last_capture = None
        # Generations keep counting up: an old capture's result must stay stale.
        self.ctl = Controller(generation=self.ctl.generation + 1)
        if self.overlay is not None:
            self.overlay.clear()
        self.icon.set_state(IconState.IDLE)
        self._status = not_connected(self.spec)
        self._apply_status()


def _write_layout_safely(folder, items, scale) -> None:
    try:
        debug.write_layout(folder, items, scale)
    except Exception:
        log.exception("Could not write layout.png")


def _rss_mb() -> float:
    """Current resident memory of this process, in MB (for the self-test)."""
    try:
        out = subprocess.run(
            ["/bin/ps", "-o", "rss=", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return int(out.strip()) / 1024
    except (OSError, ValueError, subprocess.CalledProcessError):
        return -1.0


_delegate = None  # NSApplication holds its delegate weakly


def run(*, debug: bool, self_test_captures: int = 0, self_test_solve: bool = False) -> int:
    global _delegate
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    _delegate = AppDelegate.alloc().init()
    _delegate.configure(
        debug=debug, self_test_captures=self_test_captures, self_test_solve=self_test_solve
    )
    app.setDelegate_(_delegate)
    AppHelper.runEventLoop()
    return 0
