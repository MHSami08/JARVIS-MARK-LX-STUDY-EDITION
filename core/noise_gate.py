"""
core/noise_gate.py — keep room noise away from Gemini's speech detector.

THE PROBLEM
    The microphone callback used to forward every block to the server untouched.
    In a quiet room that is fine. In a noisy one (fan, street, cafe, TV) the
    server's voice-activity detection hears a constant din and either decides you
    are still talking (so your turn never ends and it never answers) or cannot
    tell your voice from the background (so it ignores you).

WHAT THIS DOES
    It learns how loud the room is when nobody is speaking, and only lets a block
    through when it is clearly louder than that. While the gate is closed it sends
    DIGITAL SILENCE of the same length, not nothing: the server's end-of-speech
    detector needs to *see* a quiet stretch to know you finished, and a silent
    stream gives it a clean one. A short hang-over keeps the gate open through
    the gaps between words so sentences are not chopped.

HONEST LIMITS
    A level gate separates "louder than the room" from "the room". It cannot
    separate your voice from another person's voice at a similar loudness. For a
    crowded place, a headset mic or push-to-talk (Ctrl+Space) is still the most
    reliable answer; this makes ordinary steady noise stop breaking things.

TUNING (config/api_keys.json)
    "noise_gate": {"enabled": true, "strength": "medium"}
    strength: "mild" | "medium" | "strong" — how far above the room's noise a
    block must be before it counts as you. Raise it if noise still gets through,
    lower it if the start of your sentences is being cut.
"""

from __future__ import annotations

from collections import deque

import numpy as np

_RATIO = {"mild": 1.8, "medium": 2.6, "strong": 3.8}   # open threshold = floor * ratio
_MARGIN = 110.0          # ...and never less than floor + this (int16 RMS units)
_FLOOR_MIN = 15.0        # a perfectly silent mic still has a little self-noise
_FLOOR_MAX = 1400.0      # cap: speech above this must always be able to open the gate
_HANGOVER_S = 0.55       # stay open this long after the last loud block
_WINDOW_S = 1.6          # noise floor = quietest moment in this window


class NoiseGate:
    def __init__(self, strength: str = "medium"):
        self.ratio = _RATIO.get(str(strength).lower(), _RATIO["medium"])
        self._floor: float | None = None
        self._recent: deque[float] = deque()
        self._open = False
        self._hang = 0.0

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def floor(self) -> float:
        return float(self._floor or 0.0)

    def reset(self) -> None:
        self._floor = None
        self._recent.clear()
        self._open = False
        self._hang = 0.0

    def process(self, block) -> np.ndarray:
        """int16 samples in -> same-shaped int16 out (original, or silence)."""
        arr = np.asarray(block)
        if arr.size == 0:
            return arr
        x = arr.astype(np.float32).reshape(-1)
        # The gate DECIDES on the first-difference signal, not on `x`.
        #
        # `x[1:] - 0.95 * x[:-1]` is a one-pole high-pass: at 16 kHz its corner
        # is ~127 Hz, and a 150 Hz ceiling-fan rumble comes out ~23 dB down,
        # while speech — whose energy sits mostly above 300 Hz — still reads as
        # loud. So steady low-frequency din stops holding the gate open, which
        # is the whole reason a fan made JARVIS either ignore the user or never
        # end its turn.
        #
        # The floor and the threshold are both taken from this same signal, so
        # the RATIO between them — which is what actually opens the gate — is
        # unchanged by the swap. Only the LEVEL moves.
        #
        # Nothing else changes: `arr` is returned untouched at the bottom of
        # this method, so the bytes sent to Gemini are bit-for-bit the
        # microphone's, exactly as before.
        d = x[1:] - 0.95 * x[:-1] if x.size > 1 else x
        rms = float(np.sqrt(np.mean(d * d)))
        dur = x.size / 16000.0
        n_keep = max(4, int(_WINDOW_S / max(dur, 1e-3)))

        self._recent.append(rms)
        while len(self._recent) > n_keep:
            self._recent.popleft()

        # The quietest block of the last ~1.6 s is the room, even mid-sentence:
        # people pause between words. Follow it down quickly, up slowly.
        target = min(self._recent)
        if self._floor is None:
            self._floor = target
        else:
            rate = 0.4 if target < self._floor else 0.06
            self._floor += rate * (target - self._floor)
        self._floor = min(max(self._floor, _FLOOR_MIN), _FLOOR_MAX)

        threshold = max(self._floor * self.ratio, self._floor + _MARGIN)
        if rms >= threshold:
            self._open = True
            self._hang = _HANGOVER_S
        elif self._open:
            self._hang -= dur
            if self._hang <= 0.0:
                self._open = False

        return arr if self._open else np.zeros_like(arr)
