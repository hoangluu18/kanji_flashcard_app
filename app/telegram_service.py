from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import subprocess
import platform

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
    get_backlog_overview,
    get_active_session,
    get_kanji_with_cards,
    get_recent_performance,
    get_review_state,
    get_user_status_details,
    get_user_stats,
    is_callback_processed,
    mark_callback_processed,
    replace_active_session_queue,
    save_review_result,
    update_user_settings_values,
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
from app.gemini_service import GeminiService

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


def _start_morning_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Bắt đầu buổi sáng", callback_data="start_morning")]])


def _start_evening_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Ôn buổi tối", callback_data="start_evening")]])


class TelegramBotService:
    def __init__(self, settings: Settings, session_factory):
        self.settings = settings
        self.session_factory = session_factory
        self.application: Application | None = None
        self.enabled = settings.bot_ready
        self.use_webhook = settings.telegram_use_webhook
        self.gemini = GeminiService(settings)

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
        self.application.add_handler(CommandHandler("quick", self.cmd_quick))
        self.application.add_handler(CommandHandler("review", self.cmd_review))
        self.application.add_handler(CommandHandler("stats", self.cmd_stats))
        self.application.add_handler(CommandHandler("settings", self.cmd_settings))
        self.application.add_handler(CommandHandler("setting", self.cmd_settings))
        self.application.add_handler(CommandHandler("setnew", self.cmd_setnew))
        self.application.add_handler(CommandHandler("setlimit", self.cmd_setlimit))
        self.application.add_handler(CommandHandler("vacation", self.cmd_vacation))
        self.application.add_handler(CommandHandler("backlog", self.cmd_backlog))
        self.application.add_handler(CommandHandler("setkey", self.cmd_setkey))
        self.application.add_handler(CommandHandler("vm_status", self.cmd_vm_status))
        self.application.add_handler(CommandHandler("force_update", self.cmd_force_update))
        self.application.add_handler(CommandHandler("sh", self.cmd_sh))
        self.application.add_handler(CommandHandler("gemini", self.cmd_gemini))
        self.application.add_handler(CommandHandler("h", self.cmd_gemini))
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
            "- /quick: phiên học nhanh ~10 phút\n"
            "- /review: chỉ học thẻ đến hạn\n"
            "- /stats: xem tiến độ học\n"
            "- /settings: xem cấu hình giới hạn & nhắc nhở\n"
            "- /setnew <số>: chỉnh thẻ mới/ngày\n"
            "- /setlimit <số>: chỉnh giới hạn ôn/ngày\n"
            "- /vacation on|off: bật/tắt nghỉ học\n"
            "- /backlog: xem tồn đọng cần xử lý\n"
            "- /gemini (hoặc /h): hỏi AI về Kanji kèm ảnh\n"
            "- /vm_status: xem trạng thái máy chủ (logs, memory)\n"
            "- /force_update: ép máy chủ cập nhật code tức thì\n"
            "- /sh <lệnh>: thao tác vào hệ thống Linux (Dành cho Admin)"
        )
        await context.bot.send_message(chat_id=chat.id, text=text)

    async def cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._start_session_from_command(update, session_type="morning")

    async def cmd_quick(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._start_session_from_command(update, session_type="quick")

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
            details = get_user_status_details(session, user.id, per_status_limit=8)
            perf7 = get_recent_performance(session, user.id, days=7)
            backlog = get_backlog_overview(session, user.id)

        lines = [
            "Thống kê của bạn:",
            f"- Tổng thẻ: {stats['total']}",
            f"- Moi: {stats['new']}",
            f"- Đang học: {stats['learning']}",
            f"- Nhớ ngắn hạn: {stats['young']}",
            f"- Nhớ ổn định: {stats['mature']}",
            f"- Khó nhớ: {stats['leech']}",
            f"- Đến hạn hôm nay: {stats['due']}",
            f"- Quá hạn tồn: {backlog['overdue']}",
            "",
            "Hiệu suất 7 ngày gần nhất:",
            f"- Lượt trả lời: {perf7['total']}",
            f"- Again: {perf7['again']}",
            f"- Tỷ lệ nhớ (không Again): {perf7['accuracy']}%",
            "",
            "Chi tiết theo trạng thái (tối đa 8 thẻ/trạng thái):",
            f"- Mới: {', '.join(details['new']) if details['new'] else 'không có'}",
            f"- Đang học: {', '.join(details['learning']) if details['learning'] else 'không có'}",
            f"- Nhớ ngắn hạn: {', '.join(details['young']) if details['young'] else 'không có'}",
            f"- Nhớ ổn định: {', '.join(details['mature']) if details['mature'] else 'không có'}",
            f"- Quên gần đây: {', '.join(details['lapsed']) if details['lapsed'] else 'không có'}",
            f"- Khó nhớ: {', '.join(details['leech']) if details['leech'] else 'không có'}",
        ]
        await context.bot.send_message(chat_id=chat.id, text="\n".join(lines))

    async def cmd_setnew(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        if user is None or chat is None:
            return

        if not context.args:
            await context.bot.send_message(chat_id=chat.id, text="Dùng: /setnew <0-30>")
            return

        try:
            value = int(context.args[0])
        except ValueError:
            await context.bot.send_message(chat_id=chat.id, text="Giá trị không hợp lệ. Ví dụ: /setnew 8")
            return

        value = max(0, min(30, value))
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
            settings = update_user_settings_values(session, user.id, new_per_day=value)

        await context.bot.send_message(chat_id=chat.id, text=f"Đã cập nhật thẻ mới/ngày = {settings.new_per_day}.")

    async def cmd_setlimit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        if user is None or chat is None:
            return

        if not context.args:
            await context.bot.send_message(chat_id=chat.id, text="Dùng: /setlimit <10-120>")
            return

        try:
            value = int(context.args[0])
        except ValueError:
            await context.bot.send_message(chat_id=chat.id, text="Giá trị không hợp lệ. Ví dụ: /setlimit 30")
            return

        value = max(10, min(120, value))
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
            settings = update_user_settings_values(session, user.id, review_limit=value)

        await context.bot.send_message(chat_id=chat.id, text=f"Đã cập nhật giới hạn ôn/ngày = {settings.review_limit}.")

    async def cmd_vacation(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        if user is None or chat is None:
            return

        if not context.args:
            await context.bot.send_message(chat_id=chat.id, text="Dùng: /vacation on hoặc /vacation off")
            return

        value = context.args[0].strip().lower()
        if value not in ("on", "off"):
            await context.bot.send_message(chat_id=chat.id, text="Chỉ nhận on hoặc off. Ví dụ: /vacation on")
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
            settings = update_user_settings_values(session, user.id, vacation_mode=(value == "on"))

        state = "BẬT" if settings.vacation_mode else "TẮT"
        await context.bot.send_message(chat_id=chat.id, text=f"Chế độ nghỉ học đã {state}.")

    async def cmd_backlog(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            backlog = get_backlog_overview(session, user.id)

        text = (
            "Tồn đọng hiện tại:\n"
            f"- Đến hạn hôm nay: {backlog['due_today']}\n"
            f"- Quá hạn: {backlog['overdue']}\n"
            f"- Tổng cần xử lý: {backlog['total_due']}\n"
            "\n"
            "Gợi ý: Trong tuần ôn nhẹ, cuối tuần dọn backlog theo tỷ lệ 40/60."
        )
        await context.bot.send_message(chat_id=chat.id, text=text)

    async def cmd_sh(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        if user is None or chat is None:
            return

        # KIỂM TRA BẢO MẬT: Đã kích hoạt khóa an toàn. Đổi "xxxpmxx" thành username Telegram thực tế của bạn nếu khác.
        if user.username != "xxxpmxx":
            await context.bot.send_message(chat_id=chat.id, text="⛔ Bạn không có quyền thực thi lệnh này. Sự cố truy cập này đã được ghi nhận!")
            return

        command = " ".join(context.args)
        if not command:
            warning_msg = (
                "⚠️ *CẢNH BÁO QUAN TRỌNG*\n"
                "1. Lệnh này cho phép thực thi trực tiếp trên OS (RẤT NGUY HIỂM).\n"
                "2. Hiện tại *BẤT KỲ AI* chat với bot đều có thể chạy lệnh. Hãy vào `app/telegram_service.py` bỏ comment phần kiểm tra ID/username để khóa lại!\n"
                "3. *TUYỆT ĐỐI KHÔNG* chạy các lệnh tương tác (nano, vim, top, htop...) vì bot sẽ bị treo.\n\n"
                "Cú pháp sử dụng: `/sh <lệnh bash>`\n"
                "Ví dụ: `/sh ls -la`"
            )
            await context.bot.send_message(chat_id=chat.id, text=warning_msg, parse_mode="Markdown")
            return
            
        # Không chạy mấy lệnh mở màn hình tương tác gây treo bot như nano, vim, top...
        if any(cmd in command for cmd in ["nano", "vim", "top", "htop"]):
            await context.bot.send_message(chat_id=chat.id, text="❌ Không hỗ trợ chạy các lệnh tương tác (nano, vim, top...).")
            return

        try:
            if platform.system() == "Windows":
                 result = subprocess.run(["powershell", "-Command", command], capture_output=True, text=True, timeout=15)
            else:
                 import os
                 env = os.environ.copy()
                 # Fix lỗi 'command not found': cấp cho sub-shell biến môi trường PATH với đầy đủ các thư mục chứa lệnh mặc định.
                 env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:" + env.get("PATH", "")
                 result = subprocess.run(["/bin/bash", "-c", command], capture_output=True, text=True, timeout=15, env=env)

            output = result.stdout if result.stdout else result.stderr
            if not output:
                output = "✅ Lệnh chạy thành công, không có output gì."
                
            if len(output) > 3900:
                output = output[-3900:] + "\n... (Đã bị cắt bớt cho vừa tin nhắn Tele)"

            await context.bot.send_message(chat_id=chat.id, text=f"💻 Terminal:\n```text\n{output}\n```", parse_mode="Markdown")

        except subprocess.TimeoutExpired:
            await context.bot.send_message(chat_id=chat.id, text="⏳ Lệnh chạy quá 15 giây. Bị hủy vì Timeout.")
        except Exception as e:
            await context.bot.send_message(chat_id=chat.id, text=f"❌ Lỗi thực thi: {e}")

    async def cmd_force_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        if user is None or chat is None:
            return

        if user.username != "xxxpmxx":
            await context.bot.send_message(chat_id=chat.id, text="⛔ Chỉ Admin mới có thể thực hiện lệnh cập nhật này.")
            return

        await context.bot.send_message(
            chat_id=chat.id,
            text="🔄 Đang gọi script cập nhật ép buộc (force update)...\n"
                 "Nếu có code mới, Bot sẽ tự động khởi động lại và mất kết nối vài giây!"
        )

        try:
            if platform.system() == "Windows":
                await context.bot.send_message(
                    chat_id=chat.id,
                    text="⚠️ Bỏ qua update vì đang chạy trên môi trường giả lập Windows Local."
                )
            else:
                # Kích hoạt script bash chạy ngầm ở background
                import os
                import subprocess
                update_script = "/home/pmshoanghot/kanji_flashcard_app/auto_update.sh"
                
                # Bơm thêm PATH chuẩn của Linux để script hiểu được lệnh 'git' và 'curl' 
                # (Vì mặc định SystemD của kanjibot chỉ có PATH vào thư mục .venv)
                env = os.environ.copy()
                env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:" + env.get("PATH", "")
                
                subprocess.Popen(
                    ["/bin/bash", update_script], 
                    env=env,
                    start_new_session=True, # Tách rời khỏi process của Bot để không bị systemctl giết lây
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                
        except Exception as e:
            await context.bot.send_message(chat_id=chat.id, text=f"❌ Lỗi khi kích hoạt script: {e}")

    async def cmd_vm_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        if user is None or chat is None:
            return

        if user.username != "xxxpmxx":
            await context.bot.send_message(chat_id=chat.id, text="⛔ Bạn không có quyền truy cập thông tin Server.")
            return

        try:
            if platform.system() == "Windows":
                uptime_info = "N/A (Chạy trên Windows Local)"
                mem_usage = "N/A (Chạy trên Windows Local)"
            else:
                # Đọc Uptime trực tiếp từ Linux /proc/uptime để tránh lỗi PATH của subprocess
                with open('/proc/uptime', 'r') as f:
                    uptime_seconds = float(f.readline().split()[0])
                    days = int(uptime_seconds // 86400)
                    hours = int((uptime_seconds % 86400) // 3600)
                    minutes = int((uptime_seconds % 3600) // 60)
                    uptime_parts = []
                    if days > 0: uptime_parts.append(f"{days} days")
                    if hours > 0: uptime_parts.append(f"{hours} hours")
                    if minutes > 0: uptime_parts.append(f"{minutes} minutes")
                    uptime_info = "up " + ", ".join(uptime_parts) if uptime_parts else "up less than a minute"
                
                # Đọc Memory trực tiếp từ Linux /proc/meminfo
                with open('/proc/meminfo', 'r') as f:
                    mem_data = {}
                    for line in f:
                        if line.startswith(("MemTotal:", "MemAvailable:", "Cached:", "Buffers:")):
                            parts = line.split()
                            mem_data[parts[0].strip(":")] = int(parts[1]) # in kB
                    
                    total_mb = mem_data.get("MemTotal", 0) // 1024
                    avail_mb = mem_data.get("MemAvailable", 0) // 1024
                    used_mb = total_mb - avail_mb
                    cache_mb = (mem_data.get("Cached", 0) + mem_data.get("Buffers", 0)) // 1024
                    mem_usage = f"RAM: {used_mb}M / {total_mb}M ({cache_mb}M cache)"

            # Đọc log đa nền tảng (không dùng bash tail)
            log_path = Path("update.log")
            if log_path.exists():
                with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                    log_data = "".join(lines[-10:])
            else:
                log_data = "update.log not found."

            text = (
                f"🖥 VM Status\n\n"
                f"Uptime: {uptime_info}\n"
                f"Memory: {mem_usage}\n\n"
                f"--- Auto Update Log (Last 10 lines) ---\n"
                f"{log_data[-800:]}"
            )
        except Exception as e:
            text = f"Lỗi khi lấy VM status: {e}"

        await context.bot.send_message(chat_id=chat.id, text=text)

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
            f"- Giới hạn ôn ngày thường: {self.settings.weekday_review_limit}\n"
            f"- Giới hạn ôn cuối tuần: {self.settings.weekend_review_limit}\n"
            f"- Giới hạn phiên nhanh: {self.settings.quick_session_limit}\n"
            f"- Ngưỡng leech: {self.settings.leech_threshold}\n"
            f"- Nhắc sáng: {settings.notify_morning}\n"
            f"- Nhắc trưa: {settings.notify_noon}\n"
            f"- Nhắc tối: {settings.notify_evening}\n"
            f"- Vacation mode: {'ON' if settings.vacation_mode else 'OFF'}\n"
            "\n"
            "Lệnh nhanh: /settings (hoặc /setting), /quick, /setnew, /setlimit, /vacation, /backlog"
        )
        await context.bot.send_message(chat_id=chat.id, text=text)

    async def cmd_setkey(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Admin: set bất kỳ env variable nào qua Telegram.
        Cách dùng:
            /setkey KEY_NAME value
        Ví dụ:
            /setkey GEMINI_API_KEY AIzaSy...
            /setkey GEMINI_MODEL gemini-3.1-flash-lite-preview
            /setkey DEFAULT_NEW_PER_DAY 20
        """
        user = update.effective_user
        chat = update.effective_chat
        if user is None or chat is None:
            return

        if user.username != "xxxpmxx":
            await context.bot.send_message(chat_id=chat.id, text="⛔ Chỉ admin mới dùng lệnh này.")
            return

        if not context.args or len(context.args) < 2:
            await context.bot.send_message(
                chat_id=chat.id,
                text="💡 Cách dùng:\n"
                     "`/setkey KEY_NAME value`\n\n"
                     "Ví dụ:\n"
                     "`/setkey GEMINI_API_KEY AIzaSy...`\n"
                     "`/setkey GEMINI_MODEL gemini-3.1-flash-lite-preview`\n"
                     "`/setkey DEFAULT_NEW_PER_DAY 20`",
                parse_mode="Markdown",
            )
            return

        env_key = context.args[0].strip()
        env_value = " ".join(context.args[1:])

        # Đường dẫn file .env (cùng thư mục với project root)
        env_path = Path(__file__).resolve().parent.parent / ".env"

        # Ghi vào .env
        self._update_env_file(env_path, env_key, env_value)

        # Reload runtime settings nếu key liên quan
        reloaded = False
        status = f"✅ Đã ghi `{env_key}={env_value[:10]}{'...' if len(env_value) > 10 else ''}` vào `.env`."

        if env_key == "GEMINI_API_KEY":
            self.settings.gemini_api_key = env_value
            self.gemini = GeminiService(self.settings)
            reloaded = True
            status += f"\n🔄 Đã reload Gemini service."
            if self.gemini.enabled:
                status += f"\n🟢 Gemini sẵn sàng: {env_value[:10]}..."
            else:
                status += "\n🔴 Key chưa hợp lệ, kiểm tra lại."

        elif env_key == "GEMINI_MODEL":
            self.settings.gemini_model = env_value
            self.gemini = GeminiService(self.settings)
            reloaded = True
            status += f"\n🔄 Đã reload Gemini service với model `{env_value}`."

        elif env_key == "TELEGRAM_BOT_TOKEN":
            self.settings.telegram_bot_token = env_value
            status += "\n⚠️ Cần restart bot để áp dụng token mới."

        elif env_key in ("DEFAULT_NEW_PER_DAY", "DEFAULT_REVIEW_LIMIT", "LEECH_THRESHOLD"):
            try:
                val = int(env_value)
                if env_key == "DEFAULT_NEW_PER_DAY":
                    self.settings.default_new_per_day = val
                elif env_key == "DEFAULT_REVIEW_LIMIT":
                    self.settings.default_review_limit = val
                elif env_key == "LEECH_THRESHOLD":
                    self.settings.leech_threshold = val
                reloaded = True
                status += f"\n🔄 Đã cập nhật runtime: `{env_key} = {val}`."
            except ValueError:
                status += f"\n⚠️ Giá trị phải là số, nhưng `{env_value}` không phải số."

        await context.bot.send_message(chat_id=chat.id, text=status, parse_mode="Markdown")

    def _update_env_file(self, env_path: Path, key: str, value: str) -> None:
        """Cập nhật hoặc thêm key=value vào file .env."""
        lines = []
        found = False

        if env_path.exists():
            lines = env_path.read_text(encoding="utf-8").splitlines()

        for i, line in enumerate(lines):
            # Bỏ comment và dòng rỗng
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            if stripped.startswith(key + "="):
                lines[i] = f"{key}={value}"
                found = True
                break

        if not found:
            lines.append(f"{key}={value}")

        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        get_logger(__name__).info("Updated .env: %s=%s", key, value[:10] + "...")

    async def cmd_gemini(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Xử lý lệnh /gemini hoặc /h - hỏi AI kèm ảnh nếu có."""
        user = update.effective_user
        chat = update.effective_chat
        if user is None or chat is None:
            return

        # Kiểm tra Gemini có sẵn sàng không
        if not self.gemini.enabled:
            await context.bot.send_message(
                chat_id=chat.id,
                text="⚠️ Gemini AI hiện chưa được cấu hình. Vui lòng liên hệ admin."
            )
            return

        # Gửi thông báo đang xử lý
        status_msg = await context.bot.send_message(chat_id=chat.id, text="🤖 Đang suy nghĩ...")

        try:
            # Lấy câu hỏi từ user
            question = " ".join(context.args) if context.args else ""

            # Kiểm tra xem có ảnh trong reply message không
            image_bytes = None
            if update.message and update.message.reply_to_message:
                replied_msg = update.message.reply_to_message
                if replied_msg.photo:
                    photo = replied_msg.photo[-1]  # Lấy ảnh độ phân giải cao nhất
                    file = await photo.get_file()
                    image_bytes = await file.download_as_bytearray()
            elif update.message and update.message.photo:
                # Trường hợp user gửi ảnh kèm caption
                photo = update.message.photo[-1]
                file = await photo.get_file()
                image_bytes = await file.download_as_bytearray()

            # Nếu không có câu hỏi và không có ảnh
            if not question and not image_bytes:
                await status_msg.edit_text(
                    "💡 Cách dùng:\n"
                    "• `/gemini câu hỏi của bạn`\n"
                    "• Reply ảnh bằng `/gemini` để hỏi về ảnh\n"
                    "• Gửi ảnh kèm caption: `/gemini Đây là kanji gì?`\n\n"
                    "📝 Ví dụ:\n"
                    "• `/gemini Kanji 陵 nghĩa là gì?`\n"
                    "• `/gemini Giải thích từ vựng trong ảnh này`"
                )
                return

            # Nếu không có question nhưng có ảnh, tạo câu hỏi mặc định
            if not question and image_bytes:
                question = "Hãy phân tích nội dung ảnh này và giải thích chi tiết."

            # Gọi Gemini API
            if image_bytes:
                response_text = await self.gemini.ask_with_image(
                    question=question,
                    image_bytes=bytes(image_bytes)
                )
            else:
                response_text = await self.gemini.ask(question=question)

            # Gemini trả lời có thể rất dài > 4096 chars (Telegram limit)
            if len(response_text) > 4000:
                response_text = response_text[:4000] + "\n\n... (còn tiếp)"

            await status_msg.edit_text(f"🤖 **Gemini AI:**\n\n{response_text}", parse_mode="Markdown")

        except Exception as exc:
            logger.exception("Gemini command failed: %s", exc)
            await status_msg.edit_text(f"❌ Có lỗi xảy ra: {str(exc)}")

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
            elif data == "start_morning":
                await self._start_session_for_user(user.id, session_type="morning")
            elif data == "start_evening":
                await self._start_session_for_user(user.id, session_type="evening")
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
            f"- Ôn từ hôm qua: {due_count}\n"
            f"- Mới: {new_count}\n"
            "Bấm để bắt đầu."
        )
        await self.send_text(user_id, text, reply_markup=_start_morning_keyboard())

    async def send_nudge(self, user_id: int, label: str, due_count: int) -> None:
        if due_count <= 0:
            return
        text = f"{label}: hôm nay bạn vẫn còn {due_count} thẻ đến hạn."
        await self.send_text(user_id, text, reply_markup=_start_evening_keyboard())
