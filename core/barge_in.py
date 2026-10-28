"""
Deciding when the user has interrupted JARVIS mid-sentence.

This is the *decision* half of barge-in. The *signal* half already exists in
`core/echo.py`: EchoGuard separates the user's voice from the assistant's own
speaker echo by subtracting what was just played from what the microphone hears.
That module is deliberately quiet about how many blocks of agreement it takes to
believe someone, because the answer depends on how the audio streams are
configured — which is a fact about the caller, not about acoustics.

So the two jobs are split:

    EchoGuard      "is this block a different voice, or our own echo?"
    BargeInDetector "has that been true long enough, and often enough, to act?"

Why the evidence requirement is not a constant
----------------------------------------------
A single loud block is not an interruption. A cough, a chair creak, a keystroke,
a door — all of them produce one frame that looks like speech and then stop. A
person talking holds the same conclusion across many consecutive frames. So the
detector counts *consecutive* positives and requires a run, which is what makes
the difference between an assistant that stops when you speak and one that stops
whenever the room makes a noise.

The run length is also where the room's difficulty is absorbed. EchoGuard already
reports how well it can tell the two voices apart (`reliable`), and demands more
evidence by itself when the acoustics are poor — in a loud room a person's voice
and the echo of ours sit close together, and only time separates them. This class
inherits that and scales it by the user's chosen sensitivity.

Honest limits
-------------
This cannot work in every room. If the speakers are loud and the room is
reverberant, our own echo comes back sounding enough like a voice that no
content test can reliably separate the two, and EchoGuard says so through
`reliable`. The detector answers by requiring much more evidence, which makes
interruption slower but not wrong. There is no setting here that makes a bad
acoustic setup behave like a good one; a headset microphone is still the real
answer for those rooms, which is why the feature is switchable and off by
default.

Threading
---------
`consider()` is called from the sounddevice microphone callback, i.e. a real-time
audio thread. It must be cheap and must never raise: an exception there kills the
microphone stream and takes the session down. Everything is therefore wrapped,
and the only work done is one small FFT inside EchoGuard plus a counter.
"""

from __future__ import annotations

import time

# How many consecutive positive blocks to require, as a multiplier on what
# EchoGuard itself asks for. Low sensitivity is for a room with a television in
# it or a talkative household; high is for headphones or a quiet desk, where the
# echo is nearly absent and waiting costs more than it protects.
_SENSITIVITY = {
    "low":    2.0,
    "medium": 1.0,
    "high":   0.5,
}

# Never fewer than this many blocks, whatever the arithmetic says. Two blocks is
# already ~130 ms of speech at the default 64 ms block, which is about the
# shortest real word; below that a click starts winning.
_MIN_BLOCKS = 2

# After an interruption fires, ignore the microphone for this long. The playback
# queue is still draining and the speakers are still sounding, so the echo the
# guard is comparing against no longer matches what is in the room. Without this
# the same sentence can be "interrupted" several times in the first few hundred
# milliseconds.
_COOLDOWN_S = 0.6


class BargeInDetector:
    """Turns EchoGuard's per-block verdict into a single interrupt decision.

    Usage from the microphone callback:

        if speaking:
            if detector.consider(indata, sr, level):
                ...interrupt...

    and from anywhere the speaking state changes:

        detector.note_speaking(False)     # a new turn starts clean
    """

    def __init__(self, guard, sensitivity: str = "medium",
                 logger=None) -> None:
        self._guard = guard
        self._logger = logger or (lambda _m: None)
        self.sensitivity = sensitivity
        self._run = 0             # consecutive blocks the guard called speech
        self._fired = False       # one interruption per turn, not per block
        self._last_fire = 0.0
        self._blocks_seen = 0     # diagnostics only
        # Tracks the previous speaking state so note_speaking() can spot the
        # rising edge that marks a new reply. Must exist before the first call:
        # a missing attribute here would be swallowed by the guard below and
        # then never assigned, leaving the edge permanently undetectable.
        self._speaking = False

    # ── configuration ───────────────────────────────────────────────────────

    @property
    def sensitivity(self) -> str:
        return self._sensitivity

    @sensitivity.setter
    def sensitivity(self, value: str) -> None:
        v = str(value or "").strip().lower()
        self._sensitivity = v if v in _SENSITIVITY else "medium"

    @property
    def required_blocks(self) -> int:
        """Consecutive positive blocks needed right now.

        Derived rather than fixed, because the guard changes its own answer as it
        learns the room: a reverberant setup is asked for far more evidence than
        a quiet one, and that is a property of the acoustics rather than of the
        user's sensitivity preference.
        """
        try:
            base = int(self._guard.required_blocks)
        except Exception:
            base = 5
        scaled = int(round(base * _SENSITIVITY[self._sensitivity]))
        return max(_MIN_BLOCKS, scaled)

    # ── state ───────────────────────────────────────────────────────────────

    def note_speaking(self, speaking: bool) -> None:
        """Call on every block (or on every change) with the current state.

        A rising edge — JARVIS starting a new reply — clears the latch and the
        run, so evidence gathered during the previous sentence can never count
        towards interrupting this one.
        """
        try:
            if speaking and not self._speaking:
                self._run = 0
                self._fired = False
            self._speaking = bool(speaking)
        except Exception:
            pass

    def reset(self) -> None:
        """Drop all per-turn state. Learning inside the guard is left alone.

        `_speaking` is cleared as well, so the next `note_speaking(True)` reads as
        a rising edge and re-clears the counters. That is idempotent and it keeps
        the object honest: after a reset it knows nothing, including whether a
        reply is in progress, and False is the safe thing to not-know.
        """
        self._run = 0
        self._fired = False
        self._speaking = False

    # ── the decision ────────────────────────────────────────────────────────

    def consider(self, pcm, sr: int, level: float) -> bool:
        """True exactly once per turn, on the block that should interrupt.

        Returning True does not itself stop anything — the caller decides how to
        unwind playback, because that differs between the desktop path (drain an
        asyncio queue) and the phone relay.
        """
        try:
            self._blocks_seen += 1

            if self._fired:
                return False
            if (time.monotonic() - self._last_fire) < _COOLDOWN_S:
                return False

            # Nothing recent was played, so there is nothing to compare the
            # microphone against and every sound would look like a new voice.
            # This is not hypothetical: it is exactly the state in the first
            # block or two after a reply starts, before the playback loop has
            # recorded anything.
            if not self._guard.has_reference:
                self._run = 0
                return False

            if not self._guard.is_user_speech(pcm, sr, level):
                self._run = 0
                return False

            self._run += 1
            if self._run < self.required_blocks:
                return False

            # Sustained speech that the guard is confident is not our echo.
            self._fired = True
            self._last_fire = time.monotonic()
            self._run = 0
            self._logger(
                f"Interrupted after {self.required_blocks} blocks "
                f"(sensitivity {self._sensitivity}, "
                f"room {'reliable' if self._guard.reliable else 'difficult'}, "
                f"echo floor {self._guard.floor:.2f})"
            )
            return True
        except Exception:
            # A failure here must never break the microphone. Staying silent is
            # the safe direction: an assistant that misses an interruption is
            # annoying, one that interrupts itself mid-sentence is unusable.
            return False

    # ── diagnostics ─────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        """Current state, for the log and for a settings screen."""
        try:
            blocks = self.required_blocks
            return {
                "sensitivity":   self._sensitivity,
                "required":      blocks,
                "run":           self._run,
                "fired":         self._fired,
                "calibrated":    self._guard.calibrated,
                "reliable":      self._guard.reliable,
                "echo_floor":    round(self._guard.floor, 3),
                "echo_gain":     round(self._guard.gain, 3),
                "blocks_seen":   self._blocks_seen,
            }
        except Exception:
            return {"sensitivity": self._sensitivity}
