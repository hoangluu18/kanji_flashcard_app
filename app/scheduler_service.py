from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import Settings, parse_hour_minute
from app.database import session_scope
from app.logging_setup import get_logger
from app.repository import (
    build_queue,
    ensure_review_states,
    ensure_user_settings,
    get_notifiable_user_ids,
    prune_old_callbacks,
)
from app.telegram_service import TelegramBotService

logger = get_logger(__name__)


class SchedulerService:
    def __init__(self, settings: Settings, session_factory, bot_service: TelegramBotService):
        self.settings = settings
        self.session_factory = session_factory
        self.bot_service = bot_service
        self.scheduler: AsyncIOScheduler | None = None

    def start(self) -> None:
        if not self.bot_service.enabled:
            logger.warning("Scheduler disabled because Telegram bot is disabled.")
            return

        if self.scheduler is not None and self.scheduler.running:
            return

        scheduler = AsyncIOScheduler(timezone=self.settings.app_timezone)

        m_hour, m_min = parse_hour_minute(self.settings.schedule_morning)
        e_hour, e_min = parse_hour_minute(self.settings.schedule_evening)

        scheduler.add_job(self.run_morning_job, "cron", hour=m_hour, minute=m_min, id="morning_job")
        scheduler.add_job(self.run_evening_job, "cron", hour=e_hour, minute=e_min, id="evening_job")
        scheduler.add_job(self.run_maintenance_job, "cron", hour=3, minute=10, id="maintenance_job")

        scheduler.start()
        self.scheduler = scheduler
        logger.info("Scheduler started")

    def shutdown(self) -> None:
        if self.scheduler is None:
            return
        self.scheduler.shutdown(wait=False)
        self.scheduler = None
        logger.info("Scheduler stopped")

    async def run_morning_job(self) -> None:
        logger.info("Running morning job")
        users = self._load_user_ids()
        for user_id in users:
            try:
                due_count, new_count = self._load_morning_due_new(user_id)
                await self.bot_service.send_morning_prompt(user_id, due_count=due_count, new_count=new_count)
            except Exception as exc:
                logger.exception("morning job failed for user %s: %s", user_id, exc)

    async def run_noon_job(self) -> None:
        logger.info("Noon reminder job is disabled")

    async def run_evening_job(self) -> None:
        logger.info("Running evening reminder job")
        users = self._load_user_ids()
        for user_id in users:
            try:
                due_count = self._load_overdue_count(user_id)
                await self.bot_service.send_nudge(user_id, label="Ôn buổi tối", due_count=due_count)
            except Exception as exc:
                logger.exception("evening job failed for user %s: %s", user_id, exc)

    async def run_maintenance_job(self) -> None:
        logger.info("Running maintenance job")
        with session_scope(self.session_factory) as session:
            deleted = prune_old_callbacks(session)
            logger.info("Pruned %s old callback ids", deleted)

    def _load_user_ids(self) -> list[int]:
        with session_scope(self.session_factory) as session:
            return get_notifiable_user_ids(session)

    def _load_morning_due_new(self, user_id: int) -> tuple[int, int]:
        with session_scope(self.session_factory) as session:
            ensure_user_settings(
                session,
                user_id,
                self.settings.default_new_per_day,
                self.settings.default_review_limit,
                self.settings.schedule_morning,
                self.settings.schedule_noon,
                self.settings.schedule_evening,
            )
            ensure_review_states(session, user_id)
            info = build_queue(session, user_id, session_type="morning")
            return info.due_count, info.new_count

    def _load_overdue_count(self, user_id: int) -> int:
        with session_scope(self.session_factory) as session:
            ensure_review_states(session, user_id)
            info = build_queue(session, user_id, session_type="evening")
            return len(info.queue)
