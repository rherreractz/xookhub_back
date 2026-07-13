# Ruta: src/generation/sm2.py
"""
SM-2 spaced-repetition algorithm (SuperMemo 2).

Kept as pure functions with no DB/ORM dependency so the scheduling math is
unit-testable in isolation and reusable. `FlashcardReview` rows in
`service.py` are updated *from* the `SM2Result` this returns; the algorithm
itself knows nothing about persistence.

Reference behaviour (grade q ∈ [0, 5]):
  - q < 3  → the answer was a lapse: repetitions reset to 0, interval to 1
    day, so the card is seen again tomorrow. Ease is still penalised.
  - q >= 3 → success: repetitions increment and the interval grows
    (1 day → 6 days → previous_interval * ease thereafter).
  - Ease factor is nudged by q every review and floored at 1.3 so a
    chronically-hard card can't collapse to a zero interval.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MIN_EASE = 1.3
FIRST_INTERVAL_DAYS = 1
SECOND_INTERVAL_DAYS = 6


class InvalidGradeError(ValueError):
    """Raised when a review grade is outside the 0–5 range."""


@dataclass(frozen=True, slots=True)
class SM2State:
    """The three SM-2 variables entering a review."""

    ease: float
    interval_days: int
    repetitions: int


@dataclass(frozen=True, slots=True)
class SM2Result:
    """Updated variables plus the computed next-review timestamp."""

    ease: float
    interval_days: int
    repetitions: int
    next_review: datetime


def compute_sm2(state: SM2State, grade: int, *, now: datetime | None = None) -> SM2Result:
    """Apply one SM-2 review and return the updated scheduling state.

    `grade` is the user's self-assessed recall quality, 0 (total blackout)
    to 5 (perfect). `now` is injectable for deterministic testing; it
    defaults to the current UTC time.
    """
    if not 0 <= grade <= 5:
        raise InvalidGradeError("La calificación debe estar entre 0 y 5.")

    now = now or datetime.now(timezone.utc)

    # Ease update — same nudge formula SM-2 uses, floored at MIN_EASE.
    new_ease = state.ease + (0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02))
    new_ease = max(MIN_EASE, new_ease)

    if grade < 3:
        # Lapse: restart the repetition count, review again tomorrow.
        new_repetitions = 0
        new_interval = FIRST_INTERVAL_DAYS
    else:
        new_repetitions = state.repetitions + 1
        if new_repetitions == 1:
            new_interval = FIRST_INTERVAL_DAYS
        elif new_repetitions == 2:
            new_interval = SECOND_INTERVAL_DAYS
        else:
            new_interval = round(state.interval_days * new_ease)

    next_review = now + timedelta(days=new_interval)
    return SM2Result(
        ease=new_ease,
        interval_days=new_interval,
        repetitions=new_repetitions,
        next_review=next_review,
    )