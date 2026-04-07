from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import asc, func, select
from sqlalchemy.orm import Session

from app.models import (
    ActiveSession,
    Kanji,
    KanjiCard,
    ProcessedCallback,
    ReviewLog,
    ReviewState,
    User,
    UserSettings,
)


@dataclass(frozen=True)
class SessionSnapshot:
    user_id: int
    queue: list[int]
    current_index: int
    phase: str
    card_index: int
    session_type: str


@dataclass(frozen=True)
class QueueInfo:
    queue: list[int]
    due_count: int
    new_count: int


def ensure_user(session: Session, user_id: int, username: str | None, timezone: str) -> User:
    user = session.get(User, user_id)
    if user is None:
        user = User(telegram_id=user_id, username=username, timezone=timezone)
        session.add(user)
    else:
        user.username = username
        user.last_active = datetime.utcnow()
    session.flush()
    return user


def ensure_user_settings(
    session: Session,
    user_id: int,
    default_new_per_day: int,
    default_review_limit: int,
    morning: str,
    noon: str,
    evening: str,
) -> UserSettings:
    settings = session.get(UserSettings, user_id)
    if settings is None:
        settings = UserSettings(
            user_id=user_id,
            new_per_day=default_new_per_day,
            review_limit=default_review_limit,
            notify_morning=morning,
            notify_noon=noon,
            notify_evening=evening,
            vacation_mode=False,
        )
        session.add(settings)
        session.flush()
    return settings


def ensure_review_states(session: Session, user_id: int) -> None:
    existing = set(
        session.scalars(select(ReviewState.kanji_id).where(ReviewState.user_id == user_id)).all()
    )
    all_kanji = session.scalars(select(Kanji.id).order_by(Kanji.id.asc())).all()
    missing = [k for k in all_kanji if k not in existing]

    for kanji_id in missing:
        session.add(
            ReviewState(
                user_id=user_id,
                kanji_id=kanji_id,
                status="new",
                ease=2.5,
                interval=0,
                due_date=None,
                reps=0,
                lapses=0,
                learning_step=0,
                updated_at=datetime.utcnow(),
            )
        )

    if missing:
        session.flush()


def build_queue(session: Session, user_id: int, session_type: str) -> QueueInfo:
    settings = session.get(UserSettings, user_id)
    if settings is None:
        raise RuntimeError("User settings missing")

    today = date.today()

    due_rows = session.scalars(
        select(ReviewState)
        .where(
            ReviewState.user_id == user_id,
            ReviewState.status.in_(["learning", "young", "mature", "lapsed"]),
            ReviewState.due_date.is_not(None),
            ReviewState.due_date <= today,
        )
        .order_by(asc(ReviewState.due_date), asc(ReviewState.kanji_id))
        .limit(settings.review_limit)
    ).all()

    due_queue = [row.kanji_id for row in due_rows]

    new_queue: list[int] = []
    if session_type in ("mixed", "new"):
        new_rows = session.scalars(
            select(ReviewState)
            .where(ReviewState.user_id == user_id, ReviewState.status == "new")
            .order_by(asc(ReviewState.kanji_id))
            .limit(settings.new_per_day)
        ).all()
        new_queue = [row.kanji_id for row in new_rows]

    if session_type == "review":
        queue = due_queue
    elif session_type == "new":
        queue = new_queue
    else:
        queue = due_queue + new_queue

    # Keep unique order if overlaps happen.
    dedup: list[int] = []
    seen = set()
    for kanji_id in queue:
        if kanji_id not in seen:
            dedup.append(kanji_id)
            seen.add(kanji_id)

    return QueueInfo(queue=dedup, due_count=len(due_queue), new_count=len(new_queue))


def upsert_active_session(session: Session, user_id: int, queue: list[int], session_type: str) -> ActiveSession:
    row = session.get(ActiveSession, user_id)
    now = datetime.utcnow()
    if row is None:
        row = ActiveSession(
            user_id=user_id,
            queue=json.dumps(queue),
            current_index=0,
            phase="front",
            card_index=0,
            session_type=session_type,
            started_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.queue = json.dumps(queue)
        row.current_index = 0
        row.phase = "front"
        row.card_index = 0
        row.session_type = session_type
        row.started_at = now
        row.updated_at = now
    session.flush()
    return row


def get_active_session(session: Session, user_id: int) -> SessionSnapshot | None:
    row = session.get(ActiveSession, user_id)
    if row is None:
        return None

    try:
        queue = json.loads(row.queue)
    except json.JSONDecodeError:
        queue = []

    if not isinstance(queue, list):
        queue = []

    return SessionSnapshot(
        user_id=user_id,
        queue=[int(x) for x in queue],
        current_index=row.current_index,
        phase=row.phase,
        card_index=row.card_index,
        session_type=row.session_type,
    )


def update_active_session(
    session: Session,
    user_id: int,
    current_index: int,
    phase: str,
    card_index: int,
) -> None:
    row = session.get(ActiveSession, user_id)
    if row is None:
        return
    row.current_index = current_index
    row.phase = phase
    row.card_index = card_index
    row.updated_at = datetime.utcnow()
    session.flush()


def replace_active_session_queue(
    session: Session,
    user_id: int,
    queue: list[int],
    current_index: int,
    phase: str = "front",
    card_index: int = 0,
) -> None:
    row = session.get(ActiveSession, user_id)
    if row is None:
        return
    row.queue = json.dumps(queue)
    row.current_index = current_index
    row.phase = phase
    row.card_index = card_index
    row.updated_at = datetime.utcnow()
    session.flush()


def clear_active_session(session: Session, user_id: int) -> None:
    row = session.get(ActiveSession, user_id)
    if row is not None:
        session.delete(row)
        session.flush()


def get_kanji_with_cards(session: Session, kanji_id: int) -> tuple[Kanji | None, list[KanjiCard]]:
    kanji = session.get(Kanji, kanji_id)
    if kanji is None:
        return None, []

    cards = session.scalars(
        select(KanjiCard).where(KanjiCard.kanji_id == kanji_id).order_by(asc(KanjiCard.card_order), asc(KanjiCard.id))
    ).all()
    return kanji, cards


def get_review_state(session: Session, user_id: int, kanji_id: int) -> ReviewState | None:
    return session.get(ReviewState, {"user_id": user_id, "kanji_id": kanji_id})


def save_review_result(
    session: Session,
    user_id: int,
    kanji_id: int,
    rating: str,
    interval_before: int,
    interval_after: int,
    ease_before: float,
    ease_after: float,
) -> None:
    session.add(
        ReviewLog(
            user_id=user_id,
            kanji_id=kanji_id,
            rating=rating,
            interval_before=interval_before,
            interval_after=interval_after,
            ease_before=ease_before,
            ease_after=ease_after,
        )
    )
    session.flush()


def is_callback_processed(session: Session, callback_id: str) -> bool:
    return session.get(ProcessedCallback, callback_id) is not None


def mark_callback_processed(session: Session, user_id: int, callback_id: str) -> None:
    if session.get(ProcessedCallback, callback_id) is None:
        session.add(ProcessedCallback(callback_id=callback_id, user_id=user_id))
        session.flush()


def prune_old_callbacks(session: Session, keep_hours: int = 48) -> int:
    cutoff = datetime.utcnow().timestamp() - keep_hours * 3600
    rows = session.scalars(select(ProcessedCallback)).all()
    deleted = 0
    for row in rows:
        if row.processed_at.timestamp() < cutoff:
            session.delete(row)
            deleted += 1
    if deleted:
        session.flush()
    return deleted


def get_user_stats(session: Session, user_id: int) -> dict[str, int]:
    total = session.scalar(
        select(func.count()).select_from(ReviewState).where(ReviewState.user_id == user_id)
    )
    new_count = session.scalar(
        select(func.count())
        .select_from(ReviewState)
        .where(ReviewState.user_id == user_id, ReviewState.status == "new")
    )
    learning = session.scalar(
        select(func.count())
        .select_from(ReviewState)
        .where(ReviewState.user_id == user_id, ReviewState.status == "learning")
    )
    young = session.scalar(
        select(func.count())
        .select_from(ReviewState)
        .where(ReviewState.user_id == user_id, ReviewState.status == "young")
    )
    mature = session.scalar(
        select(func.count())
        .select_from(ReviewState)
        .where(ReviewState.user_id == user_id, ReviewState.status == "mature")
    )
    leech = session.scalar(
        select(func.count())
        .select_from(ReviewState)
        .where(ReviewState.user_id == user_id, ReviewState.status == "leech")
    )

    today = date.today()
    due = session.scalar(
        select(func.count())
        .select_from(ReviewState)
        .where(
            ReviewState.user_id == user_id,
            ReviewState.status.in_(["learning", "young", "mature", "lapsed"]),
            ReviewState.due_date.is_not(None),
            ReviewState.due_date <= today,
        )
    )

    return {
        "total": int(total or 0),
        "new": int(new_count or 0),
        "learning": int(learning or 0),
        "young": int(young or 0),
        "mature": int(mature or 0),
        "leech": int(leech or 0),
        "due": int(due or 0),
    }


def get_all_users(session: Session) -> list[User]:
    return session.scalars(select(User).order_by(User.telegram_id.asc())).all()
