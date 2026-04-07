from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Ho_Chi_Minh", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    last_active: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), primary_key=True)
    new_per_day: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    review_limit: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    notify_morning: Mapped[str] = mapped_column(String(5), default="09:00", nullable=False)
    notify_noon: Mapped[str] = mapped_column(String(5), default="13:00", nullable=False)
    notify_evening: Mapped[str] = mapped_column(String(5), default="20:30", nullable=False)
    vacation_mode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Kanji(Base):
    __tablename__ = "kanji"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    page: Mapped[int] = mapped_column(Integer, nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    header_img_path: Mapped[str] = mapped_column(Text, nullable=False)
    header_file_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    viet_name: Mapped[str | None] = mapped_column(String(255), nullable=True)


class KanjiCard(Base):
    __tablename__ = "kanji_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kanji_id: Mapped[int] = mapped_column(ForeignKey("kanji.id"), index=True, nullable=False)
    card_img_path: Mapped[str] = mapped_column(Text, nullable=False)
    card_file_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    card_order: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ReviewState(Base):
    __tablename__ = "review_state"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), primary_key=True)
    kanji_id: Mapped[int] = mapped_column(ForeignKey("kanji.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="new", nullable=False)
    ease: Mapped[float] = mapped_column(Float, default=2.5, nullable=False)
    interval: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    reps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lapses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    learning_step: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ReviewLog(Base):
    __tablename__ = "review_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), nullable=False)
    kanji_id: Mapped[int] = mapped_column(ForeignKey("kanji.id"), nullable=False)
    rating: Mapped[str] = mapped_column(String(16), nullable=False)
    interval_before: Mapped[int] = mapped_column(Integer, nullable=False)
    interval_after: Mapped[int] = mapped_column(Integer, nullable=False)
    ease_before: Mapped[float] = mapped_column(Float, nullable=False)
    ease_after: Mapped[float] = mapped_column(Float, nullable=False)
    answered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ActiveSession(Base):
    __tablename__ = "active_session"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), primary_key=True)
    queue: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    current_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    phase: Mapped[str] = mapped_column(String(16), default="front", nullable=False)
    card_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    session_type: Mapped[str] = mapped_column(String(16), default="mixed", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ProcessedCallback(Base):
    __tablename__ = "processed_callbacks"

    callback_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
