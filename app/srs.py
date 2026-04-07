from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta

RATING_AGAIN = 0
RATING_HARD = 1
RATING_GOOD = 2
RATING_EASY = 3

RATING_TEXT = {
    RATING_AGAIN: "again",
    RATING_HARD: "hard",
    RATING_GOOD: "good",
    RATING_EASY: "easy",
}


@dataclass
class ReviewInput:
    status: str
    ease: float
    interval: int
    reps: int
    lapses: int
    learning_step: int


@dataclass
class ReviewResult:
    status: str
    ease: float
    interval: int
    reps: int
    lapses: int
    learning_step: int
    due_date: date


def apply_fuzz(interval: int) -> int:
    if interval < 7:
        return interval
    if interval < 30:
        fuzz = max(1, round(interval * 0.10))
    else:
        fuzz = max(2, round(interval * 0.05))
    return max(1, interval + random.randint(-fuzz, fuzz))


def calculate_next_review(
    item: ReviewInput,
    rating: int,
    today: date,
    leech_threshold: int,
) -> ReviewResult:
    if rating not in (RATING_AGAIN, RATING_HARD, RATING_GOOD, RATING_EASY):
        raise ValueError(f"Invalid rating value: {rating}")

    status = item.status
    ease = max(1.3, min(4.0, item.ease))
    interval = max(0, item.interval)
    reps = max(0, item.reps)
    lapses = max(0, item.lapses)
    learning_step = max(0, item.learning_step)

    # Learning phase (new, learning, or lapsed).
    if status in ("new", "learning", "lapsed"):
        if rating == RATING_AGAIN:
            status = "learning"
            learning_step = 0
            due = today + timedelta(days=1)
            return ReviewResult(status, ease, 0, reps, lapses, learning_step, due)

        if learning_step < 1:
            status = "learning"
            learning_step += 1
            due = today
            return ReviewResult(status, ease, 0, reps, lapses, learning_step, due)

        # Graduate from learning to young.
        status = "young"
        learning_step = 0
        interval = 1
        reps += 1
        due = today + timedelta(days=1)
        return ReviewResult(status, ease, interval, reps, lapses, learning_step, due)

    # Review phase (young/mature).
    if rating == RATING_AGAIN:
        ease = max(1.3, ease - 0.20)
        lapses += 1
        status = "lapsed"
        learning_step = 0
        interval = 1
        due = today + timedelta(days=1)
    elif rating == RATING_HARD:
        ease = max(1.3, ease - 0.15)
        interval = max(1, round(interval * 1.2))
        interval = apply_fuzz(interval)
        due = today + timedelta(days=interval)
        reps += 1
        status = "mature" if interval >= 21 else "young"
    elif rating == RATING_GOOD:
        interval = max(1, round(interval * ease))
        interval = apply_fuzz(interval)
        due = today + timedelta(days=interval)
        reps += 1
        status = "mature" if interval >= 21 else "young"
    else:  # RATING_EASY
        ease = min(4.0, ease + 0.15)
        interval = max(1, round(interval * ease * 1.3))
        interval = apply_fuzz(interval)
        due = today + timedelta(days=interval)
        reps += 1
        status = "mature" if interval >= 21 else "young"

    if lapses >= leech_threshold:
        status = "leech"

    return ReviewResult(status, ease, interval, reps, lapses, learning_step, due)
