"""What Peek is doing, and what follows from each event: a pure state machine.

The AppDelegate feeds it events (a capture starts, a result arrives, ⌘⌘, the
displays change) and carries out the effects it returns (clear, show or hide the
overlay, flash "No questions"). The menu-bar icon follows `phase`. Nothing here
touches AppKit, so the transition table can be tested directly.

Rules from the spec, plus the owner's choices:
- Answers are hidden when they arrive, unless "Keep answers visible across
  captures" is on and answers were on screen when the capture started (or the
  capture replaces one that was going to show its answers).
- A new capture clears the old answers at once (and they stay out of the
  screenshot) and supersedes any capture in flight; a superseded capture's result
  is dropped by generation.
- ⌘⌘ toggles answers that exist; with no answers it does nothing.
- ⇧⇧ toggles the working (Settings → Show work) on its own, the same way: hidden
  when it arrives unless "Keep visible" is on and it was showing.
- If the displays change, answers (pinned to screen positions) are cleared and a
  capture in flight is dropped.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Phase(enum.Enum):
    IDLE = "idle"  # nothing to show (no capture yet, no questions, or cleared)
    WORKING = "working"  # a capture is in flight
    READY = "ready"  # answers placed, shown or hidden
    ERROR = "error"  # the last capture failed; the app shows why


class Effect(enum.Enum):
    CLEAR = "clear"  # take every answer off screen and forget them
    SHOW = "show"
    HIDE = "hide"
    SHOW_WORK = "show_work"
    HIDE_WORK = "hide_work"
    NO_QUESTIONS = "no_questions"  # the brief "No questions" label by the icon


class Outcome(enum.Enum):
    ANSWERS = "answers"  # at least one answer placed on screen
    UNPLACEABLE = "unplaceable"  # questions came back, none could be placed
    NO_QUESTIONS = "no_questions"
    FAILED = "failed"  # capture or provider error


@dataclass
class Controller:
    generation: int = 0
    phase: Phase = Phase.IDLE
    answers: int = 0  # answers placed on screen (0 = none to toggle)
    visible: bool = False
    show_on_arrival: bool = False
    work: bool = False  # a work box was placed with the answers
    work_visible: bool = False
    work_on_arrival: bool = False

    # --- events -------------------------------------------------------------

    def start_capture(self, *, keep_visible: bool) -> list[Effect]:
        """⌃⌃ or the menu item. Returns CLEAR; the new generation is `generation`."""
        replacing = self.phase is Phase.WORKING
        self.show_on_arrival = keep_visible and (
            self.visible or (replacing and self.show_on_arrival)
        )
        self.work_on_arrival = keep_visible and (
            self.work_visible or (replacing and self.work_on_arrival)
        )
        self.generation += 1
        self.phase = Phase.WORKING
        self.answers = 0
        self.visible = False
        self.work = self.work_visible = False
        return [Effect.CLEAR]

    def refuse_capture(self) -> list[Effect]:
        """A capture could not start (no Screen Recording). Answers on screen stay."""
        if self.phase is not Phase.WORKING:
            self.phase = Phase.ERROR
        return []

    def finish(
        self, generation: int, outcome: Outcome, placed: int = 0, *, work: bool = False
    ) -> list[Effect] | None:
        """A capture's result (*work*: a work box was placed too). None if it is
        stale (a newer capture was started, or the displays changed); then nothing
        changes."""
        if generation != self.generation or self.phase is not Phase.WORKING:
            return None
        show, self.show_on_arrival = self.show_on_arrival, False
        show_work, self.work_on_arrival = self.work_on_arrival, False
        if outcome is Outcome.ANSWERS and placed > 0:
            self.phase = Phase.READY
            self.answers = placed
            self.visible = show
            self.work = work
            self.work_visible = work and show_work
            effects = [Effect.SHOW] if show else []
            return effects + ([Effect.SHOW_WORK] if self.work_visible else [])
        self.answers = 0
        self.visible = False
        self.work = self.work_visible = False
        if outcome is Outcome.NO_QUESTIONS:
            self.phase = Phase.IDLE
            return [Effect.NO_QUESTIONS]
        self.phase = Phase.ERROR
        return []

    def toggle(self) -> list[Effect]:
        """⌘⌘ or Show/Hide answers. No answers: no-op (spec)."""
        if self.answers == 0:
            return []
        self.visible = not self.visible
        return [Effect.SHOW] if self.visible else [Effect.HIDE]

    def toggle_work(self) -> list[Effect]:
        """⇧⇧ or Show/Hide work. No working on screen: no-op."""
        if not self.work:
            return []
        self.work_visible = not self.work_visible
        return [Effect.SHOW_WORK] if self.work_visible else [Effect.HIDE_WORK]

    def work_changed(self, work: bool) -> list[Effect]:
        """The answers were laid out again (Settings changed): is there working now?
        It keeps its visibility while it stays."""
        if not self.answers:
            return []
        self.work = work
        if not work and self.work_visible:
            self.work_visible = False
            return [Effect.HIDE_WORK]
        return []

    def screens_changed(self) -> list[Effect]:
        """Displays added, removed or rearranged: positions are stale."""
        effects: list[Effect] = []
        if self.phase is Phase.WORKING:
            self.generation += 1  # drop the capture taken on the old arrangement
            self.phase = Phase.IDLE
            self.show_on_arrival = False
            self.work_on_arrival = False
        if self.answers:
            effects.append(Effect.CLEAR)
            self.answers = 0
            self.visible = False
            self.work = self.work_visible = False
            if self.phase is Phase.READY:
                self.phase = Phase.IDLE
        return effects

    def problem_resolved(self) -> list[Effect]:
        """Settings changed (a key added, a model picked): leave the error state."""
        if self.phase is Phase.ERROR:
            self.phase = Phase.READY if self.answers else Phase.IDLE
        return []

    # --- for the menu ---------------------------------------------------------

    @property
    def can_toggle(self) -> bool:
        return self.answers > 0

    @property
    def can_toggle_work(self) -> bool:
        return self.work
