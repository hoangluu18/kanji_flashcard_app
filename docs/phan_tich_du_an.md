# Phân tích dự án Kanji Flashcard Telegram Bot

## 📊 Tổng quan

Dự án Telegram bot học Kanji N2 sử dụng phương pháp Spaced Repetition System (SRS) dựa trên thuật toán SM-2 của Anki, được xây dựng bằng FastAPI + python-telegram-bot.

---

## ✅ ĐIỂM MẠNH

### 1. Thuật toán SRS chuẩn xác

- **SM-2 implementation đúng chuẩn Anki** với đầy đủ các giai đoạn: New → Learning → Young → Mature → Lapsed → Leech
- **Fuzz factor** tránh việc thẻ dồn cùng ngày
- **Minimum ease = 1.3** tránh "ease hell"
- **Leech detection** (lapses >= 5) tự động suspend thẻ khó
- Có logging chi tiết từng lần review (ReviewLog) để debug và phân tích

### 2. Thiết kế database hợp lý

- **8 tables** bao quát đầy đủ use cases
- **ActiveSession** lưu session state trong SQLite → giải quyết vấn đề Telegram stateless
- **ProcessedCallback** chống double-click (debounce)
- **File ID caching** cho Telegram images → giảm bandwidth 10x
- SQLAlchemy ORM với session_scope → quản lý transaction an toàn

### 3. UX tốt trên Telegram

- **Inline keyboard** trực quan theo từng phase (front/back/rating)
- **Nhiều chế độ học**: /today, /quick, /review, /morning, /evening
- **Thống kê chi tiết** với `/stats` - phân bố trạng thái, performance 7 ngày
- **Vacation mode** và **backlog management** thông minh
- **Auto-protect**: tự động giảm new cards khi backlog cao (≥40, ≥80, ≥120)

### 4. Code quality cao

- **Type hints** đầy đủ (Mapped[], Mapped[int], v.v.)
- **Separation of concerns** rõ ràng:
  - `models.py` - Data layer
  - `repository.py` - Data access
  - `srs.py` - Business logic thuần
  - `telegram_service.py` - Telegram integration
  - `scheduler_service.py` - Background jobs
- **Pydantic Settings** quản lý cấu hình an toàn
- **ALIAS_ pattern** trong `.env.example` → dễ setup, tránh lỗi config default

### 5. Scheduler thông minh

- **Phân chia buổi sáng/tối** → chia nhỏ daily review limit
- **Weekend ratio** (Saturday 40%, Sunday 60%) → catch-up backlog hợp lý
- **Weekday vs Weekend limits** → linh hoạt theo lịch người dùng
- **Maintenance job** tự dọn callback IDs cũ

### 6. Production-ready

- **Webhook + Polling** đều được hỗ trợ
- **Health endpoints** (`/health`, `/health/deep`) cho monitoring
- **Admin API endpoints** có xác thực (`X-Admin-Key`)
- **Error handling** với unhandled_exception_handler
- **Graceful startup/shutdown** lifecycle

---

## 🔧 ĐIỂM CẢI THIỆN

### 1. Critical - Cần khắc phục sớm

#### a) Thiếu xử lý multi-card kanji đúng cách
**Vấn đề:** 
- Trong `_handle_rate()`, sau khi rate xong, bot chuyển sang card tiếp theo ngay
- Nhưng nếu kanji có nhiều cards, user phải nhấn "Trang tiếp" nhiều lần rồi mới được rate
- Không có logic để đảm bảo tất cả cards của cùng 1 kanji được xem trước khi rate

**Đề xuất:**
```python
# Nên: Hiện rating keyboard chỉ sau khi user đã xem hết cards
if current_card_idx < len(cards) - 1:
    # Hiện nút "Card tiếp →" 
    return
# Chỉ hiện rating khi đã xem hết cards
await self.send_text(user_id, "Đánh giá:", reply_markup=_rating_keyboard())
```

#### b) Thiếu anti-flood rate limiting
**Vấn đề:**
- User có thể spam nhấn nút liên tục → bot gửi tin nhắn liên tục
- Telegram có rate limit ~30 messages/second per bot

**Đề xuất:**
```python
# Thêm debounce 1.5s giữa các lần send
import asyncio
await asyncio.sleep(1.5)  # Trước khi gửi tin tiếp theo
```

#### c) Learning phase logic chưa đúng spec
**Vấn đề:**
- Spec yêu cầu: Learning steps = [evening cùng ngày, next_day]
- Code hiện tại: `learning_step < 1` → due = today, nhưng không phân biệt sáng/tối
- Không có logic "tối nay ôn lại" cho learning step 0

**Đề xuất:**
```python
# Check current time để quyết định due_date
from datetime import datetime
now = datetime.now()
if now.hour < 12:  # Buổi sáng
    due = today + timedelta(hours=12)  # Tối nay
else:
    due = today + timedelta(days=1)  # Ngày mai
```

### 2. Architecture & Design

#### a) File telegram_service.py quá lớn (898 dòng)
**Vấn đề:**
- 1 file chứa quá nhiều trách nhiệm: commands, callbacks, photo sending, session management
- Khó test và maintain

**Đề xuất:**
```
telegram_service.py (150 dòng - coordinator)
├── commands/
│   ├── start_command.py
│   ├── stats_command.py
│   └── settings_command.py
├── callbacks/
│   ├── flip_callback.py
│   ├── rate_callback.py
│   └── skip_callback.py
└── message_senders.py
```

#### b) Session management có thể bị stale
**Vấn đề:**
- ActiveSession không có TTL/expiration
- User thoát Telegram giữa chừng → session tồn tại mãi mãi
- Không có cleanup job cho stale sessions

**Đề xuất:**
```python
# Thêm vào maintenance job
def cleanup_stale_sessions(session: Session, hours: int = 24):
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    session.query(ActiveSession).filter(
        ActiveSession.updated_at < cutoff
    ).delete()
```

#### c) Thiếu retry logic cho Telegram API calls
**Vấn đề:**
- Network errors không được retry
- User có thể bị mất tin nhắn nếu Telegram timeout

**Đề xuất:**
```python
from telegram.error import NetworkError, TimedOut
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(stop=stop_after_attempt(3), wait=wait_exponential())
async def send_photo_with_retry(chat_id, photo):
    await bot.send_photo(chat_id, photo)
```

### 3. Performance & Scalability

#### a) N+1 query trong build_queue()
**Vấn đề:**
- `build_queue()` thực hiện ~6 queries riêng biệt cho due/overdue/new
- Mỗi query có limit riêng → có thể gộp thành 1-2 queries

**Đề xuất:**
```python
# Gộp thành 1 query với conditional ordering
due_rows = session.scalars(
    select(ReviewState)
    .where(
        ReviewState.user_id == user_id,
        ReviewState.status.in_(["learning", "young", "mature", "lapsed"]),
        ReviewState.due_date <= today,
    )
    .order_by(
        case((ReviewState.due_date < today, 0), else_=1),
        ReviewState.due_date,
        ReviewState.kanji_id
    )
    .limit(session_review_limit)
).all()
```

#### b) Không có caching cho stats queries
**Vấn đề:**
- `/stats` thực hiện 8+ COUNT queries mỗi lần gọi
- User gọi liên tục → waste resources

**Đề xuất:**
```python
# Cache với TTL 5 phút
from functools import lru_cache
from datetime import timedelta

@cache(ttl=300)  # 5 minutes
def get_user_stats_cached(user_id: int) -> dict:
    ...
```

#### c) SQLite không scale được cho nhiều users
**Vấn đề:**
- SQLite write lock khi có concurrent writes
- Không hỗ trợ connection pooling thật sự

**Đề xuất:**
-短期: WAL mode cho SQLite
```python
engine = create_engine(
    "sqlite:///kanji.db",
    connect_args={"check_same_thread": False},
    execution_options={"isolation_level": "AUTOCOMMIT"}
)
# PRAGMA journal_mode=WAL
```
- Dài hạn: PostgreSQL migration (spec đã đề cập)

### 4. Testing

#### a) Không có tests
**Vấn đề:**
- Không có unit tests, integration tests, hay E2E tests
- Khó refactor hoặc thêm tính năng mới safely

**Đề xuất:**
```
tests/
├── test_srs.py          # Test thuật toán SM-2
├── test_repository.py   # Test DB queries
├── test_commands.py     # Test Telegram commands
├── test_callbacks.py    # Test callback handlers
└── conftest.py          # Fixtures, mock bot
```

**Ví dụ test SRS:**
```python
def test_good_rating_increases_interval():
    result = calculate_next_review(
        ReviewInput(status="young", ease=2.5, interval=5, reps=3, lapses=0, learning_step=0),
        rating=RATING_GOOD,
        today=date.today(),
        leech_threshold=5
    )
    assert result.interval == 13  # 5 * 2.5 = 12.5 → 13 (fuzz)
    assert result.status == "young"
```

### 5. Documentation & DX

#### a) Thiếu README
**Vấn đề:**
- Không có file hướng dẫn setup, run, deploy
- Developer mới sẽ mất nhiều thời gian để onboard

**Đề xuất:**
```markdown
# README.md
- Quick Start (3 bước để chạy local)
- Configuration guide
- Database migration guide
- How to add new kanji
- Deployment guide (Railway/Heroku)
- Troubleshooting
```

#### b) Thiếu migration system
**Vấn đề:**
- Database schema changes → phải xóa DB và recreate
- Không có version control cho schema

**Đề xuất:**
```bash
# Dùng Alembic
pip install alembic
alembic init alembic
alembic revision --add_column review_state.updated_at
alembic upgrade head
```

#### c) Error messages bằng tiếng Việt nhưng không nhất quán
**Vấn đề:**
- Một số message tiếng Việt, một số tiếng Anh
- Không có i18n system

**Đề xuất:**
```python
# translations/vi.py
MESSAGES = {
    "no_session": "Không có phiên học đang mở. Gửi /today để bắt đầu.",
    "session_done": "Đã hoàn thành phiên học. Làm tốt lắm.",
}

# Usage
await self.send_text(user_id, MESSAGES["no_session"])
```

### 6. Feature Gaps (so với spec)

#### a) Thiếu `/browse` command
**Spec yêu cầu:** Duyệt thẻ theo số thứ tự, không tính SRS
**Hiện tại:** Chưa implement

#### b) Thiếu `/leech` command
**Spec yêu cầu:** Xem danh sách thẻ leech, học lại thủ công
**Hiện tại:** Có detection nhưng không có UI để xem/retry

#### c) Thiếu inline query
**Spec đề xuất:** `@bot LĂNG` tra nhanh bất kỳ lúc nào
**Hiện tại:** Chưa có

#### d) Vacation mode auto-enable
**Spec yêu cầu:** "Nếu user không hoạt động > 3 ngày, hỏi bật Vacation Mode"
**Hiện tại:** User phải bật thủ công bằng `/vacation on`

#### e) Performance-based difficulty adjustment
**Spec đề xuất:** "Nếu > 30% Again thì giảm thẻ mới/ngày"
**Hiện tại:** Có auto-protect theo backlog nhưng không theo accuracy

### 7. Minor Issues

#### a) Hardcoded strings trong telegram_service.py
```python
# Thay vì:
await self.send_text(user_id, "Kanji {kanji.number} ({snapshot.current_index + 1}/{len(snapshot.queue)}). Tự nhớ lại trước...")

# Nên dùng template:
text = TEMPLATES["kanji_front"].format(
    kanji_number=kanji.number,
    current=snapshot.current_index + 1,
    total=len(snapshot.queue)
)
```

#### b) Không có backup tự động SQLite
**Đề xuất:**
```python
# Thêm vào maintenance job
import shutil
def backup_database(db_path: str, backup_dir: str):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{backup_dir}/kanji_backup_{timestamp}.db"
    shutil.copy2(db_path, backup_path)
    # Keep only last 7 days
    cleanup_old_backups(backup_dir, days=7)
```

#### c) Callback data pattern chưa tối ưu
**Hiện tại:** `"rate:again"`, `"rate:good"`, v.v.
**Vấn đề:** Không thể debug dễ dàng từ Telegram callback logs

**Đề xuất:**
```python
# Thêm metadata vào callback data (vẫn < 64 bytes)
callback_data = f"r:again:{kanji_id:04d}"  # 15 chars
# Parse: rating=again, kanji_id=142
```

---

## 📈 MỨC ĐỘ ƯU TIÊN

### 🔴 Cao (nên làm ngay)
1. ✅ Learning phase logic chưa đúng spec (sáng/tối)
2. ✅ Anti-flood rate limiting
3. ✅ Thêm tests cơ bản cho SRS algorithm
4. ✅ README và setup guide

### 🟡 Trung bình (trong 2-4 tuần)
5. Refactor telegram_service.py thành modules nhỏ
6. Thêm `/leech` và `/browse` commands
7. Stale session cleanup
8. Retry logic cho Telegram API calls
9. Backup database tự động

### 🟢 Thấp (khi có thời gian)
10. Caching cho stats queries
11. Migration system (Alembic)
12. i18n system
13. Inline query support
14. PostgreSQL migration

---

## 🎯 KẾT LUẬN

**Điểm mạnh nhất:** 
- Thuật toán SRS được implement đúng chuẩn Anki
- Database design thông minh với session management
- Code quality cao với type hints và separation of concerns

**Điểm cần cải thiện nhất:**
- Testing (hoàn toàn không có tests)
- Documentation (thiếu README)
- Multi-card handling logic
- Performance optimization cho queries

**Đánh giá tổng thể:** 8/10 - Dự án production-ready cho cá nhân, cần thêm work để scale cho nhiều users.

---

*Phân tích ngày: $(date)*
*Người phân tích: Qwen Code AI*
