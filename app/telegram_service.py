from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from app.config import Settings
from app.database import session_scope
from app.logging_setup import get_logger
from app.models import Kanji, KanjiCard, ReviewState
from app.repository import (
    build_queue,
    clear_active_session,
    ensure_review_states,
    ensure_user,
    ensure_user_settings,
    get_active_session,
    get_kanji_with_cards,
    get_review_state,
    get_user_stats,
    is_callback_processed,
    mark_callback_processed,
    replace_active_session_queue,
    save_review_result,
    update_active_session,
    upsert_active_session,
)
from app.srs import (
    RATING_AGAIN,
    RATING_EASY,
    RATING_GOOD,
    RATING_HARD,
    RATING_TEXT,
    ReviewInput,
    calculate_next_review,
)

logger = get_logger(__name__)

RATING_MAP = {
    "again": RATING_AGAIN,
    "hard": RATING_HARD,
    "good": RATING_GOOD,
    "easy": RATING_EASY,
}

RATING_LABEL_VI = {
    "again": "Lại",
    "hard": "Khó",
    "good": "Tốt",
    "easy": "Dễ",
}

STATUS_LABEL_VI = {
    "new": "Mới",
    "learning": "Đang học",
    "young": "Nhớ ngắn hạn",
    "mature": "Nhớ ổn định",
    "lapsed": "Quên",
    "leech": "Khó nhớ",
}


def _front_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Xem đáp án", callback_data="flip")],
            [InlineKeyboardButton("Bỏ qua", callback_data="skip")],
        ]
    )


def _next_card_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Trang tiếp", callback_data="next_card")]])


def _rating_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Lại", callback_data="rate:again"),
                InlineKeyboardButton("Khó", callback_data="rate:hard"),
            ],
            [
                InlineKeyboardButton("Tốt", callback_data="rate:good"),
                InlineKeyboardButton("Dễ", callback_data="rate:easy"),
            ],
        ]
    )


def _start_today_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Bắt đầu hôm nay", callback_data="start_today")]])


class TelegramBotService:
    def __init__(self, settings: Settings, session_factory):
        self.settings = settings
        self.session_factory = session_factory
        self.application: Application | None = None
        self.enabled = settings.bot_ready
        self.use_webhook = settings.telegram_use_webhook

        if not self.enabled:
            logger.warning(
                "Telegram bot is disabled. Fill TELEGRAM_BOT_TOKEN (ALIAS_ value is still present)."
            )
            return

        builder = Application.builder().token(settings.telegram_bot_token)
        if self.use_webhook:
            builder = builder.updater(None)

        self.application = builder.build()
        self._register_handlers()

    def _register_handlers(self) -> None:
        assert self.application is not None
        self.application.add_handler(CommandHandler("start", self.cmd_start))
        self.application.add_handler(CommandHandler("today", self.cmd_today))
        self.application.add_handler(CommandHandler("review", self.cmd_review))
        self.application.add_handler(CommandHandler("stats", self.cmd_stats))
        self.application.add_handler(CommandHandler("settings", self.cmd_settings))
        self.application.add_handler(CallbackQueryHandler(self.callback_router))

    async def initialize(self) -> None:
        if not self.enabled:
            return
        assert self.application is not None
        await self.application.initialize()
        await self.application.start()

        if self.use_webhook:
            webhook_url = await self.ensure_webhook()
            logger.info("Telegram application initialized in webhook mode: %s", webhook_url)
            return

        await self.remove_webhook()
        if self.application.updater is None:
            raise RuntimeError("Polling mode requires updater, but updater is None")

        await self.application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("Telegram application initialized in polling mode")

    async def shutdown(self) -> None:
        if not self.enabled or self.application is None:
            return

        if not self.use_webhook and self.application.updater and self.application.updater.running:
            await self.application.updater.stop()

        await self.application.stop()
        await self.application.shutdown()
        logger.info("Telegram application shut down")

    async def process_update(self, payload: dict) -> None:
        if not self.enabled or self.application is None:
            raise RuntimeError("Telegram bot is disabled")
        if not self.use_webhook:
            raise RuntimeError("Webhook endpoint is disabled because TELEGRAM_USE_WEBHOOK=false")

        update = Update.de_json(payload, self.application.bot)
        if update is None:
            return
        await self.application.process_update(update)

    async def ensure_webhook(self) -> str:
        if not self.enabled or self.application is None:
            raise RuntimeError("Telegram bot is disabled")
        if not self.use_webhook:
            raise RuntimeError("Webhook mode is disabled because TELEGRAM_USE_WEBHOOK=false")
        if not self.settings.webhook_ready:
            raise RuntimeError("Webhook config is incomplete. Fill TELEGRAM_PUBLIC_BASE_URL and TELEGRAM_WEBHOOK_SECRET.")

        url = (
            self.settings.telegram_public_base_url.rstrip("/")
            + "/telegram/webhook/"
            + self.settings.telegram_webhook_secret
        )
        ok = await self.application.bot.set_webhook(url=url)
        if not ok:
            raise RuntimeError("Telegram refused webhook setup")
        return url

    async def remove_webhook(self) -> None:
        if not self.enabled or self.application is None:
            return
        await self.application.bot.delete_webhook(drop_pending_updates=False)

    async def send_text(self, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        if not self.enabled or self.application is None:
            return
        await self.application.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        if user is None or chat is None:
            return

        with session_scope(self.session_factory) as session:
            ensure_user(session, user.id, user.username, self.settings.app_timezone)
            ensure_user_settings(
                session,
                user.id,
                self.settings.default_new_per_day,
                self.settings.default_review_limit,
                self.settings.schedule_morning,
                self.settings.schedule_noon,
                self.settings.schedule_evening,
            )
            ensure_review_states(session, user.id)

        text = (
            "Bot Kanji SRS đã sẵn sàng.\n"
            "- /today: bắt đầu phiên học hôm nay\n"
            "- /review: chỉ học thẻ đến hạn\n"
            "- /stats: xem tiến độ học"
        )
        await context.bot.send_message(chat_id=chat.id, text=text)

    async def cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._start_session_from_command(update, session_type="mixed")

    async def cmd_review(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._start_session_from_command(update, session_type="review")

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        if user is None or chat is None:
            return

        with session_scope(self.session_factory) as session:
            ensure_user(session, user.id, user.username, self.settings.app_timezone)
            ensure_user_settings(
                session,
                user.id,
                self.settings.default_new_per_day,
                self.settings.default_review_limit,
                self.settings.schedule_morning,
                self.settings.schedule_noon,
                self.settings.schedule_evening,
            )
            ensure_review_states(session, user.id)
            stats = get_user_stats(session, user.id)

        lines = [
            "Thống kê của bạn:",
            f"- Tổng thẻ: {stats['total']}",
            f"- Moi: {stats['new']}",
            f"- Đang học: {stats['learning']}",
            f"- Nhớ ngắn hạn: {stats['young']}",
            f"- Nhớ ổn định: {stats['mature']}",
            f"- Khó nhớ: {stats['leech']}",
            f"- Đến hạn hôm nay: {stats['due']}",
        ]
        await context.bot.send_message(chat_id=chat.id, text="\n".join(lines))

    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        if user is None or chat is None:
            return

        with session_scope(self.session_factory) as session:
            ensure_user(session, user.id, user.username, self.settings.app_timezone)
            settings = ensure_user_settings(
                session,
                user.id,
                self.settings.default_new_per_day,
                self.settings.default_review_limit,
                self.settings.schedule_morning,
                self.settings.schedule_noon,
                self.settings.schedule_evening,
            )

        text = (
            "Cài đặt hiện tại:\n"
            f"- Thẻ mới/ngày: {settings.new_per_day}\n"
            f"- Giới hạn ôn/ngày: {settings.review_limit}\n"
            f"- Nhắc sáng: {settings.notify_morning}\n"
            f"- Nhắc trưa: {settings.notify_noon}\n"
            f"- Nhắc tối: {settings.notify_evening}\n"
            "\n"
            "Lệnh sửa cài đặt sẽ bổ sung sau."
        )
        await context.bot.send_message(chat_id=chat.id, text=text)

    async def callback_router(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return

        user = query.from_user
        if user is None:
            await query.answer()
            return

        data = query.data or ""

        await query.answer()

        with session_scope(self.session_factory) as session:
            ensure_user(session, user.id, user.username, self.settings.app_timezone)
            ensure_user_settings(
                session,
                user.id,
                self.settings.default_new_per_day,
                self.settings.default_review_limit,
                self.settings.schedule_morning,
                self.settings.schedule_noon,
                self.settings.schedule_evening,
            )
            ensure_review_states(session, user.id)

            if is_callback_processed(session, query.id):
                return
            mark_callback_processed(session, user.id, query.id)

        try:
            if data == "start_today":
                await self._start_session_for_user(user.id, session_type="mixed")
            elif data == "flip":
                await self._handle_flip(user.id)
            elif data == "next_card":
                await self._handle_next_card(user.id)
            elif data.startswith("rate:"):
                rating_key = data.split(":", maxsplit=1)[1]
                await self._handle_rate(user.id, rating_key)
            elif data == "skip":
                await self._handle_skip(user.id)
            else:
                await self.send_text(user.id, "Hành động không hợp lệ. Gửi /today để bắt đầu lại.")
        except Exception as exc:
            logger.exception("Callback handling failed: %s", exc)
            await self.send_text(user.id, "Có lỗi xảy ra khi xử lý thao tác. Vui lòng gửi /today để thử lại.")

    async def _start_session_from_command(self, update: Update, session_type: str) -> None:
        user = update.effective_user
        chat = update.effective_chat
        if user is None or chat is None:
            return

        with session_scope(self.session_factory) as session:
            ensure_user(session, user.id, user.username, self.settings.app_timezone)
            ensure_user_settings(
                session,
                user.id,
                self.settings.default_new_per_day,
                self.settings.default_review_limit,
                self.settings.schedule_morning,
                self.settings.schedule_noon,
                self.settings.schedule_evening,
            )
            ensure_review_states(session, user.id)

        await self._start_session_for_user(user.id, session_type=session_type)

    async def _start_session_for_user(self, user_id: int, session_type: str) -> None:
        with session_scope(self.session_factory) as session:
            queue_info = build_queue(session, user_id, session_type=session_type)
            if not queue_info.queue:
                clear_active_session(session, user_id)
                no_work_msg = "Hiện tại không có thẻ đến hạn. Bạn đang làm rất tốt."
                if session_type == "new":
                    no_work_msg = "Hiện tại không còn thẻ mới."
                await self.send_text(user_id, no_work_msg)
                return

            upsert_active_session(session, user_id, queue_info.queue, session_type=session_type)

            await self.send_text(
                user_id,
                (
                    f"Sẵn sàng phiên học. Đến hạn: {queue_info.due_count}, Mới: {queue_info.new_count}, "
                    f"Tổng: {len(queue_info.queue)}"
                ),
            )

        await self._send_front_for_current(user_id)

    async def _send_front_for_current(self, user_id: int) -> None:
        with session_scope(self.session_factory) as session:
            snapshot = get_active_session(session, user_id)
            if snapshot is None:
                await self.send_text(user_id, "Không có phiên học đang mở. Gửi /today để bắt đầu.")
                return

            if snapshot.current_index >= len(snapshot.queue):
                clear_active_session(session, user_id)
                await self.send_text(user_id, "Đã hoàn thành phiên học. Làm tốt lắm.")
                return

            kanji_id = snapshot.queue[snapshot.current_index]
            kanji, _cards = get_kanji_with_cards(session, kanji_id)
            if kanji is None:
                update_active_session(
                    session,
                    user_id,
                    current_index=snapshot.current_index + 1,
                    phase="front",
                    card_index=0,
                )
                await self.send_text(user_id, f"Thiếu dữ liệu kanji ID {kanji_id}. Đã bỏ qua.")
                return

            try:
                await self._send_header_photo(session, user_id, kanji)
            except FileNotFoundError:
                logger.exception("Missing header file for kanji %s", kanji.id)
                update_active_session(
                    session,
                    user_id,
                    current_index=snapshot.current_index + 1,
                    phase="front",
                    card_index=0,
                )
                await self.send_text(user_id, f"Thiếu ảnh header của kanji {kanji.id}. Đã bỏ qua.")
                return

            await self.send_text(
                user_id,
                (
                    f"Kanji {kanji.number} ({snapshot.current_index + 1}/{len(snapshot.queue)}). "
                    "Tự nhớ lại trước, sau đó bấm Xem đáp án."
                ),
                reply_markup=_front_keyboard(),
            )
            update_active_session(
                session,
                user_id,
                current_index=snapshot.current_index,
                phase="front",
                card_index=0,
            )

    async def _handle_flip(self, user_id: int) -> None:
        with session_scope(self.session_factory) as session:
            snapshot = get_active_session(session, user_id)
            if snapshot is None:
                await self.send_text(user_id, "Không có phiên học đang mở. Gửi /today để bắt đầu.")
                return

            update_active_session(
                session,
                user_id,
                current_index=snapshot.current_index,
                phase="back",
                card_index=0,
            )

        await self._send_back_for_current(user_id)

    async def _handle_next_card(self, user_id: int) -> None:
        with session_scope(self.session_factory) as session:
            snapshot = get_active_session(session, user_id)
            if snapshot is None:
                await self.send_text(user_id, "Không có phiên học đang mở. Gửi /today để bắt đầu.")
                return

            update_active_session(
                session,
                user_id,
                current_index=snapshot.current_index,
                phase="back",
                card_index=snapshot.card_index + 1,
            )

        await self._send_back_for_current(user_id)

    async def _send_back_for_current(self, user_id: int) -> None:
        with session_scope(self.session_factory) as session:
            snapshot = get_active_session(session, user_id)
            if snapshot is None:
                await self.send_text(user_id, "Không có phiên học đang mở. Gửi /today để bắt đầu.")
                return

            if snapshot.current_index >= len(snapshot.queue):
                clear_active_session(session, user_id)
                await self.send_text(user_id, "Đã hoàn thành phiên học. Làm tốt lắm.")
                return

            kanji_id = snapshot.queue[snapshot.current_index]
            _kanji, cards = get_kanji_with_cards(session, kanji_id)

            if not cards:
                await self.send_text(user_id, f"Không có mặt sau cho kanji {kanji_id}. Sẽ hiện nút đánh giá.")
                await self.send_text(user_id, "Đánh giá mức độ nhớ của bạn:", reply_markup=_rating_keyboard())
                return

            current_card_idx = snapshot.card_index
            if current_card_idx >= len(cards):
                current_card_idx = len(cards) - 1
                update_active_session(
                    session,
                    user_id,
                    current_index=snapshot.current_index,
                    phase="back",
                    card_index=current_card_idx,
                )

            card = cards[current_card_idx]
            try:
                await self._send_card_photo(session, user_id, card)
            except FileNotFoundError:
                logger.exception("Missing card file for kanji_card %s", card.id)
                await self.send_text(user_id, "Thiếu ảnh mặt sau của thẻ này.")
                await self.send_text(user_id, "Đánh giá mức độ nhớ của bạn:", reply_markup=_rating_keyboard())
                return

            if current_card_idx < len(cards) - 1:
                await self.send_text(
                    user_id,
                    f"Mặt sau {current_card_idx + 1}/{len(cards)}. Bấm Trang tiếp để xem tiếp.",
                    reply_markup=_next_card_keyboard(),
                )
                return

            await self.send_text(
                user_id,
                "Đánh giá mức độ nhớ của bạn:",
                reply_markup=_rating_keyboard(),
            )

    async def _handle_rate(self, user_id: int, rating_key: str) -> None:
        if rating_key not in RATING_MAP:
            await self.send_text(user_id, "Đánh giá không hợp lệ.")
            return

        rating = RATING_MAP[rating_key]
        today = date.today()

        with session_scope(self.session_factory) as session:
            snapshot = get_active_session(session, user_id)
            if snapshot is None:
                await self.send_text(user_id, "Không có phiên học đang mở. Gửi /today để bắt đầu.")
                return

            if snapshot.current_index >= len(snapshot.queue):
                clear_active_session(session, user_id)
                await self.send_text(user_id, "Đã hoàn thành phiên học. Làm tốt lắm.")
                return

            kanji_id = snapshot.queue[snapshot.current_index]
            state = get_review_state(session, user_id, kanji_id)
            if state is None:
                state = ReviewState(
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
                session.add(state)
                session.flush()

            before_interval = state.interval
            before_ease = state.ease

            result = calculate_next_review(
                ReviewInput(
                    status=state.status,
                    ease=state.ease,
                    interval=state.interval,
                    reps=state.reps,
                    lapses=state.lapses,
                    learning_step=state.learning_step,
                ),
                rating=rating,
                today=today,
                leech_threshold=self.settings.leech_threshold,
            )

            state.status = result.status
            state.ease = result.ease
            state.interval = result.interval
            state.reps = result.reps
            state.lapses = result.lapses
            state.learning_step = result.learning_step
            state.due_date = result.due_date
            state.updated_at = datetime.utcnow()

            save_review_result(
                session,
                user_id=user_id,
                kanji_id=kanji_id,
                rating=RATING_TEXT[rating],
                interval_before=before_interval,
                interval_after=result.interval,
                ease_before=before_ease,
                ease_after=result.ease,
            )

            next_index = snapshot.current_index + 1
            finished = next_index >= len(snapshot.queue)
            if finished:
                clear_active_session(session, user_id)
            else:
                update_active_session(
                    session,
                    user_id,
                    current_index=next_index,
                    phase="front",
                    card_index=0,
                )

        await self.send_text(
            user_id,
            (
                f"Đã lưu: {RATING_LABEL_VI[rating_key]} | trạng thái={STATUS_LABEL_VI.get(result.status, result.status)} | "
                f"ôn lại={result.due_date.isoformat()}"
            ),
        )

        if finished:
            await self.send_text(user_id, "Đã hoàn thành phiên học. Bạn đã học rất tốt.")
            return

        await self._send_front_for_current(user_id)

    async def _handle_skip(self, user_id: int) -> None:
        with session_scope(self.session_factory) as session:
            snapshot = get_active_session(session, user_id)
            if snapshot is None:
                await self.send_text(user_id, "Không có phiên học đang mở. Gửi /today để bắt đầu.")
                return

            if snapshot.current_index >= len(snapshot.queue):
                clear_active_session(session, user_id)
                await self.send_text(user_id, "Đã hoàn thành phiên học. Làm tốt lắm.")
                return

            # Move current item to end of queue.
            queue = snapshot.queue[:]
            queue.append(queue.pop(snapshot.current_index))
            next_index = snapshot.current_index
            if next_index >= len(queue):
                next_index = max(0, len(queue) - 1)
            replace_active_session_queue(
                session,
                user_id,
                queue=queue,
                current_index=next_index,
                phase="front",
                card_index=0,
            )
            await self.send_text(user_id, "Đã bỏ qua thẻ hiện tại và đưa xuống cuối phiên học.")

        await self._send_front_for_current(user_id)

    async def _send_header_photo(self, session, chat_id: int, kanji: Kanji) -> None:
        if self.application is None:
            return

        # Reuse Telegram file_id when available to avoid repeated uploads.
        if kanji.header_file_id:
            try:
                await self.application.bot.send_photo(chat_id=chat_id, photo=kanji.header_file_id)
                return
            except TelegramError:
                logger.warning("header file_id invalid for kanji %s, fallback to disk", kanji.id)

        path = Path(kanji.header_img_path)
        if not path.exists():
            raise FileNotFoundError(f"Header image not found: {path}")

        with path.open("rb") as f:
            msg = await self.application.bot.send_photo(chat_id=chat_id, photo=f)

        if msg.photo:
            kanji.header_file_id = msg.photo[-1].file_id
            session.flush()

    async def _send_card_photo(self, session, chat_id: int, card: KanjiCard) -> None:
        if self.application is None:
            return

        if card.card_file_id:
            try:
                await self.application.bot.send_photo(chat_id=chat_id, photo=card.card_file_id)
                return
            except TelegramError:
                logger.warning("card file_id invalid for kanji_card %s, fallback to disk", card.id)

        path = Path(card.card_img_path)
        if not path.exists():
            raise FileNotFoundError(f"Card image not found: {path}")

        with path.open("rb") as f:
            msg = await self.application.bot.send_photo(chat_id=chat_id, photo=f)

        if msg.photo:
            card.card_file_id = msg.photo[-1].file_id
            session.flush()

    async def send_morning_prompt(self, user_id: int, due_count: int, new_count: int) -> None:
        if due_count <= 0 and new_count <= 0:
            return
        text = (
            "Nhắc học buổi sáng\n"
            f"- Đến hạn: {due_count}\n"
            f"- Mới: {new_count}\n"
            "Bấm để bắt đầu."
        )
        await self.send_text(user_id, text, reply_markup=_start_today_keyboard())

    async def send_nudge(self, user_id: int, label: str, due_count: int) -> None:
        if due_count <= 0:
            return
        text = f"{label}: hôm nay bạn vẫn còn {due_count} thẻ đến hạn."
        await self.send_text(user_id, text, reply_markup=_start_today_keyboard())
