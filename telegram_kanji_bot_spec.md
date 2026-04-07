# Đặc tả kỹ thuật: Bot Telegram học Kanji N2 bằng Flashcard + SRS

---

## 1. Tóm tắt bài toán

- **Mục tiêu**: Học Kanji N2 bằng flashcard trên Telegram chatbot.
- **Phương pháp học**: Spaced Repetition System (SRS) theo tư duy Anki — có đánh giá mức độ nhớ sau mỗi thẻ.
- **Ràng buộc giao diện**: Không lật thẻ như app Anki, thay bằng 2 ảnh gửi tuần tự:
  - **Header image**: mặt trước — kanji lớn + hình minh hoạ + câu gợi nhớ
  - **Card image**: mặt sau — bảng từ vựng đầy đủ
- **Lưu trữ**: SQLite local (1 file database).
- **Tự động hoá**: Scheduler gửi bài theo giờ cố định trong ngày.

---

## 2. Giải pháp tổng thể

### 2.1 Đơn vị học chính là Kanji

- 1 Kanji = 1 mục học duy nhất trong SRS.
- 1 Kanji có:
  - 1 header image (mặt trước)
  - 1..n card images (mặt sau — trường hợp kanji có nhiều trang)
- **Không nhân đôi** số lần ôn khi 1 kanji có nhiều card — vẫn tính là 1 lần đánh giá.
- Trường hợp 1 card map nhiều kanji: card đó được tái sử dụng cho từng kanji liên quan.

### 2.2 Luồng học cho 1 thẻ

```
1. Bot gửi HEADER image (mặt trước — kanji + hình + câu gợi nhớ)
2. User tự nhẩm → nhấn [👁 Xem đáp án]
3. Bot gửi CARD image (mặt sau — từ vựng đầy đủ)
   └─ Nếu kanji có nhiều card → gửi lần lượt qua nút [Xem tiếp →]
4. Bot hiển thị 4 nút đánh giá:
   [❌ Again]  [😅 Hard]  [✅ Good]  [⭐ Easy]
5. Bot tính interval mới theo SM-2 → lưu DB → chuyển thẻ tiếp theo
```

---

## 3. Thuật toán SRS — SM-2 theo chuẩn Anki

### 3.1 Vòng đời một thẻ

```
NEW → LEARNING → YOUNG → MATURE
                            ↓ (nếu bị Again)
                         LAPSED → LEARNING lại
                            ↓ (nếu lapses >= 5)
                          LEECH → Suspend
```

| Status | Ý nghĩa | Đơn vị interval |
|--------|---------|----------------|
| New | Chưa học lần nào | — |
| Learning | Đang học lần đầu, chưa ổn định | Phút/giờ (adapt thành buổi) |
| Young | Đã qua learning, interval < 21 ngày | Ngày |
| Mature | Interval ≥ 21 ngày | Ngày/tuần/tháng |
| Lapsed | Đã Mature nhưng bị quên (Again) | Reset về Learning |
| Leech | Lapses ≥ 5 — học mãi không nhớ | Suspend tạm thời |

---

### 3.2 Giai đoạn LEARNING — adapt cho Telegram

Anki dùng learning steps tính bằng phút (1 phút → 10 phút). Telegram không thể chờ trong session nên adapt thành **buổi trong ngày**:

```
Buổi sáng (09:00) — Học lần đầu:
  Good → đánh dấu "cần ôn lại tối nay"
  Again → sáng hôm sau hỏi lại

Buổi tối (20:30) — Ôn lại:
  Good → "tốt nghiệp" → vào Young, interval = 1 ngày
  Again → sáng mai hỏi lại tiếp
```

Learning steps mặc định:

| Bước | Thời gian chờ |
|------|--------------|
| Step 0 | Cùng buổi tối (≈ 8–12 tiếng) |
| Step 1 | Hôm sau |

Sau khi qua hết steps → **Graduating** → status = Young, interval = 1 ngày.

---

### 3.3 Giai đoạn YOUNG / MATURE — công thức SM-2

```
interval_mới = interval_cũ × ease_factor × modifier
```

**Thay đổi theo từng nút đánh giá:**

| Nút | Ease thay đổi | Interval mới | Ghi chú |
|-----|--------------|-------------|---------|
| ❌ Again | ease − 0.20 | Reset → Learning | lapses += 1 |
| 😅 Hard | ease − 0.15 | interval × 1.2 | Tăng chậm |
| ✅ Good | không đổi | interval × ease | Chuẩn SM-2 |
| ⭐ Easy | ease + 0.15 | interval × ease × 1.3 | Bonus 1.3 |

**Giới hạn ease**: tối thiểu **1.3** — không được xuống thấp hơn (tránh "ease hell": thẻ bị kẹt interval ngắn mãi mãi).

---

### 3.4 Ví dụ cụ thể — thẻ LĂNG (陵)

```
Ngày 0:   Học lần đầu (New → Learning step 0)
Ngày 0:   Tối ôn lại → Good → Learning step 1
Ngày 1:   Good → Tốt nghiệp → Young, interval = 1 ngày
Ngày 2:   Good → interval = 1 × 2.5 = 2 ngày
Ngày 4:   Good → interval = 2 × 2.5 = 5 ngày
Ngày 9:   Good → interval = 5 × 2.5 = 12 ngày
Ngày 21:  Good → interval = 12 × 2.5 = 30 ngày  ← vào Mature
Ngày 51:  Good → interval = 30 × 2.5 = 75 ngày
...

Nếu bị Again ở ngày 21 (đã Mature):
  lapses = 1, ease = 2.5 − 0.20 = 2.30
  → Lapsed, reset về Learning
  Ngày 22: hỏi lại → Good → interval = 1 ngày
  Ngày 23: Good → bắt đầu tăng lại từ đầu (không phải từ 30 ngày)
```

---

### 3.5 Fuzz factor — tránh thẻ dồn cùng ngày

Anki cố tình làm lệch ngày ôn một chút để tránh hàng chục thẻ dồn vào cùng 1 ngày sau 1 tháng học đều đặn:

```python
import random

def apply_fuzz(interval):
    if interval < 7:
        return interval          # ngắn thì không fuzz
    elif interval < 30:
        fuzz = max(1, round(interval * 0.10))
    else:
        fuzz = max(2, round(interval * 0.05))
    return interval + random.randint(-fuzz, fuzz)
```

---

### 3.6 Leech — thẻ học mãi không nhớ

```
lapses >= 5:
  → status = 'leech', suspend thẻ
  → Bot báo user:
    "⚠️ Kanji 陵 bạn đã quên 5 lần.
     Thẻ này tạm ẩn để không cản trở việc học.
     Gõ /leech để xem và học lại thủ công."
```

---

### 3.7 Toàn bộ logic tính interval

```python
LEARNING_STEPS = ['evening', 'next_day']  # adapt cho Telegram

def calculate_next_review(card, rating):
    # rating: 0=Again, 1=Hard, 2=Good, 3=Easy

    # ── LEARNING / LAPSED ──
    if card.status in ('new', 'learning', 'lapsed'):
        if rating == 0:  # Again
            card.learning_step = 0
            card.due = today() + days(1)
        else:  # Hard / Good / Easy
            if card.learning_step < len(LEARNING_STEPS) - 1:
                card.learning_step += 1
                card.due = tonight()   # gửi ôn buổi tối
            else:
                # Tốt nghiệp learning
                card.status = 'young'
                card.interval = 1
                card.due = today() + days(1)
        return card

    # ── YOUNG / MATURE ──
    if rating == 0:  # Again
        card.ease = max(1.3, card.ease - 0.20)
        card.lapses += 1
        card.status = 'lapsed'
        card.learning_step = 0
        card.interval = 1
        card.due = today() + days(1)

    elif rating == 1:  # Hard
        card.ease = max(1.3, card.ease - 0.15)
        card.interval = max(1, round(card.interval * 1.2))

    elif rating == 2:  # Good
        card.interval = max(1, round(card.interval * card.ease))

    elif rating == 3:  # Easy
        card.ease = min(4.0, card.ease + 0.15)
        card.interval = max(1, round(card.interval * card.ease * 1.3))

    card.interval = apply_fuzz(card.interval)
    card.due = today() + days(card.interval)
    card.reps += 1

    if card.interval >= 21:
        card.status = 'mature'
    else:
        card.status = 'young'

    if card.lapses >= 5:
        card.status = 'leech'

    return card
```

---

### 3.8 Bảng tham số cấu hình

| Tham số | Giá trị mặc định | Ghi chú |
|---------|-----------------|---------|
| Learning steps | [tối cùng ngày, hôm sau] | Adapt từ Anki |
| Starting ease | 2.5 | |
| Minimum ease | 1.3 | Không để xuống thấp hơn |
| Graduating interval | 1 ngày | Ngày đầu tiên sau learning |
| Easy bonus | 1.3 | Nhân thêm khi Easy |
| Leech threshold | 5 lapses | Suspend thẻ |
| Max new per day | 10 | Config per user |
| Max review per day | 50 | Giới hạn backlog |

---

## 4. Kiến trúc hệ thống

### 4.1 Stack

| Thành phần | Công nghệ | Ghi chú |
|-----------|-----------|---------|
| Bot framework | python-telegram-bot v20 (async) | |
| Database | SQLite | Đủ cho cá nhân đến vài trăm user |
| Scheduler | APScheduler | Gửi bài theo giờ cố định |
| Deploy | Railway free tier / VPS $5 | |

---

### 4.2 Schema Database đầy đủ

```sql
-- Người dùng
CREATE TABLE users (
    telegram_id     INTEGER PRIMARY KEY,
    username        TEXT,
    timezone        TEXT    DEFAULT 'Asia/Ho_Chi_Minh',
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_active     DATETIME
);

-- Cài đặt per-user
CREATE TABLE user_settings (
    user_id         INTEGER PRIMARY KEY REFERENCES users(telegram_id),
    new_per_day     INTEGER DEFAULT 10,
    review_limit    INTEGER DEFAULT 50,
    notify_morning  TEXT    DEFAULT '09:00',
    notify_noon     TEXT    DEFAULT '13:00',
    notify_evening  TEXT    DEFAULT '20:30',
    vacation_mode   BOOLEAN DEFAULT 0
);

-- Danh sách kanji
CREATE TABLE kanji (
    id              INTEGER PRIMARY KEY,
    page            INTEGER,
    header_img_path TEXT,       -- đường dẫn file local
    header_file_id  TEXT,       -- Telegram file_id (cache sau lần gửi đầu)
    number          INTEGER,    -- số thứ tự kanji trong sách
    viet_name       TEXT        -- tên Hán-Việt, vd: LĂNG
);

-- Card images (1 kanji có thể có nhiều card)
CREATE TABLE kanji_cards (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kanji_id        INTEGER REFERENCES kanji(id),
    card_img_path   TEXT,
    card_file_id    TEXT,       -- Telegram file_id cache
    card_order      INTEGER DEFAULT 1
);

-- Trạng thái học theo từng user × kanji
CREATE TABLE review_state (
    user_id         INTEGER REFERENCES users(telegram_id),
    kanji_id        INTEGER REFERENCES kanji(id),
    status          TEXT    DEFAULT 'new',
        -- 'new' | 'learning' | 'young' | 'mature' | 'lapsed' | 'leech'
    ease            REAL    DEFAULT 2.5,
    interval        INTEGER DEFAULT 0,      -- ngày
    due_date        DATE,
    reps            INTEGER DEFAULT 0,      -- số lần trả lời đúng liên tiếp
    lapses          INTEGER DEFAULT 0,      -- số lần bị Again sau Mature
    learning_step   INTEGER DEFAULT 0,      -- đang ở learning step nào
    PRIMARY KEY (user_id, kanji_id)
);

-- Lịch sử mỗi lần đánh giá
CREATE TABLE review_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER REFERENCES users(telegram_id),
    kanji_id        INTEGER REFERENCES kanji(id),
    rating          TEXT,           -- 'again' | 'hard' | 'good' | 'easy'
    interval_before INTEGER,        -- interval trước khi đánh giá (debug SM-2)
    interval_after  INTEGER,
    ease_before     REAL,
    ease_after      REAL,
    answered_at     DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Session đang học (thay thế in-memory state)
CREATE TABLE active_session (
    user_id         INTEGER PRIMARY KEY REFERENCES users(telegram_id),
    queue           TEXT,           -- JSON array [kanji_id, ...]
    current_index   INTEGER DEFAULT 0,
    phase           TEXT    DEFAULT 'front',
        -- 'front' | 'back'
    card_index      INTEGER DEFAULT 0,  -- đang xem card thứ mấy (nếu nhiều card)
    session_type    TEXT,           -- 'new' | 'review' | 'mixed'
    started_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

### 4.3 Tại sao SQLite là hợp lý

- Dễ dùng local, không cần setup server riêng.
- Nhanh, ổn định, backup chỉ cần copy 1 file.
- Hỗ trợ nhiều user nếu sau này mở cho bạn bè.
- Nâng cấp lên PostgreSQL dễ dàng khi cần scale.

---

## 5. Telegram Session State

### 5.1 Vấn đề

Telegram bot **stateless theo mặc định** — mỗi lần user nhấn nút là một request độc lập. Bot cần biết "user này đang học thẻ nào, đang ở bước nào" giữa các lần nhấn nút.

### 5.2 Giải pháp

Lưu session vào bảng `active_session` trong SQLite. Mỗi callback handler đọc session trước khi xử lý:

```python
async def handle_flip(update, context):
    user_id = update.effective_user.id
    session = db.get_session(user_id)
    if not session:
        await update.message.reply_text("Không có phiên học nào đang mở. Gõ /today")
        return
    kanji_id = session.queue[session.current_index]
    # gửi card image...
```

---

### 5.3 Callback data design — giới hạn 64 bytes

Telegram giới hạn `callback_data` tối đa **64 bytes**. Chỉ nhét action vào, context đọc từ session:

```python
# SAI — dễ vượt 64 bytes
callback_data = f"rate:good:kanji_id:142:card:2"

# ĐÚNG — gọn, an toàn
callback_data = "rate:good"
# Đọc kanji_id hiện tại từ session của user
```

---

### 5.4 Anti-flood / Debounce

User double-click nút → bot tính điểm 2 lần. Xử lý bằng cách lưu `last_callback_id`:

```python
async def handle_callback(update, context):
    cb = update.callback_query
    user_id = cb.from_user.id

    # Bỏ qua nếu callback này đã xử lý rồi
    if db.is_callback_processed(user_id, cb.id):
        await cb.answer()
        return

    db.mark_callback_processed(user_id, cb.id)
    # xử lý bình thường...
```

---

## 6. Lập lịch học và ôn tập

### 6.1 Lịch gửi đề xuất

| Giờ | Nội dung | Ghi chú |
|-----|---------|---------|
| 09:00 | Gửi gói học mới (kanji mới + thẻ đến hạn sáng) | Job chính trong ngày |
| 13:00 | Nhắc nhở nếu còn thẻ chưa học | Chỉ gửi nếu user chưa học buổi sáng |
| 20:30 | Ôn tập learning steps buổi tối + nhắc thẻ còn nợ | Quan trọng cho learning phase |

### 6.2 Nguyên tắc quan trọng

- **Idempotent**: Job chạy lại không tạo trùng bài. Kiểm tra `due_date = today` trước khi gửi.
- **Timezone cố định**: `Asia/Ho_Chi_Minh` cho tất cả user (hoặc per-user nếu cần).
- **Catch-up trong ngày**: Nếu bot restart lúc 10h, job 9h phải chạy bù ngay khi khởi động.
- **Không spam**: Chỉ gửi nhắc nhở nếu user thực sự có thẻ đến hạn.

```python
scheduler.add_job(
    send_morning_batch,
    trigger='cron',
    hour=9, minute=0,
    timezone='Asia/Ho_Chi_Minh'
)
```

### 6.3 Xử lý backlog — user bỏ học vài ngày

Không giới hạn → user quay lại sau 1 tuần bị 200 thẻ → nản, bỏ cuộc.

**Policy áp dụng:**

```python
def get_due_cards(user_id):
    all_due = db.get_due_cards(user_id, date=today())
    settings = db.get_settings(user_id)

    # Giới hạn cứng — ưu tiên thẻ overdue lâu nhất
    reviews = sorted(all_due, key=lambda c: c.due_date)[:settings.review_limit]
    new_cards = get_new_cards(user_id)[:settings.new_per_day]
    return reviews + new_cards
```

**Vacation mode**: Nếu user không hoạt động > 3 ngày, hỏi:

```
"Bạn vắng 3 ngày, hiện có 47 thẻ tồn đọng.
Bật Vacation Mode để freeze due date không?
[✅ Bật] [❌ Không, học hết]"
```

---

## 7. Lưu trữ ảnh — Telegram file_id caching

### 7.1 Tại sao cần cache

- Upload ảnh từ disk mỗi lần → chậm, tốn bandwidth, dễ bị rate-limit.
- Telegram lưu ảnh trên server của họ, cấp `file_id` sau lần upload đầu.
- Từ lần 2 trở đi gửi `file_id` → nhanh hơn 10x, miễn phí.

### 7.2 Cách implement

```python
async def send_kanji_header(user_id, kanji):
    if kanji.header_file_id:
        # Dùng file_id đã cache
        msg = await bot.send_photo(user_id, photo=kanji.header_file_id)
    else:
        # Upload lần đầu, lưu file_id lại
        with open(kanji.header_img_path, 'rb') as f:
            msg = await bot.send_photo(user_id, photo=f)
        file_id = msg.photo[-1].file_id
        db.update_kanji_file_id(kanji.id, header_file_id=file_id)
```

---

## 8. Thiết kế trải nghiệm Telegram

### 8.1 Inline keyboard theo từng phase

**Phase: front (xem mặt trước)**
```
[👁 Xem đáp án]  [⏭ Bỏ qua]
```

**Phase: back (xem mặt sau)**
```
[❌ Again]  [😅 Hard]  [✅ Good]  [⭐ Easy]
```

**Nếu kanji có nhiều card:**
```
Card 1/2 đang hiển thị
[← Card trước]  [Card tiếp →]  ← xem xong rồi mới hiện rating
[❌ Again]  [😅 Hard]  [✅ Good]  [⭐ Easy]
```

### 8.2 Các lệnh cơ bản

| Lệnh | Chức năng |
|------|----------|
| `/start` | Đăng ký, xem hướng dẫn |
| `/today` | Bắt đầu phiên học hôm nay |
| `/review` | Chỉ ôn thẻ đến hạn (không thêm thẻ mới) |
| `/stats` | Thống kê: tổng thẻ, streak, phân bố status |
| `/settings` | Đặt giờ nhắc, số thẻ mới/ngày |
| `/browse` | Duyệt thẻ theo số thứ tự, không tính SRS |
| `/leech` | Xem danh sách thẻ leech, học lại thủ công |
| `/skip` | Bỏ qua thẻ hiện tại, đưa về cuối queue |
| `/pause` | Tạm dừng phiên học |
| `/vacation` | Bật/tắt vacation mode |

### 8.3 Ví dụ luồng tin nhắn thực tế

```
[09:00] Bot:
  "📚 Hôm nay bạn có:
   • 5 thẻ mới
   • 12 thẻ ôn lại
   Bắt đầu? [▶ Học ngay]"

User nhấn [▶ Học ngay]

Bot: [Gửi ảnh header — kanji 陵 + hình bus + câu gợi nhớ]
Bot: "Bạn nhớ được gì về kanji này?"
     [👁 Xem đáp án]  [⏭ Bỏ qua]

User nhấn [👁 Xem đáp án]

Bot: [Gửi ảnh card đầy đủ — bảng từ vựng]
Bot: "Bạn nhớ ở mức nào?"
     [❌ Again]  [😅 Hard]  [✅ Good]  [⭐ Easy]

User nhấn [✅ Good]

Bot: "✅ Sẽ ôn lại sau 6 ngày."
     [Thẻ tiếp theo →]  ← tự động chuyển sau 1.5 giây

... (lặp lại)

[Hết session]
Bot: "🎉 Xong rồi! Hôm nay học 17 thẻ.
     🔥 Streak: 5 ngày liên tiếp.
     📅 Ngày mai có 8 thẻ đến hạn."
```

---

## 9. Thống kê `/stats`

```
📊 Tiến độ học của bạn

Tổng thẻ:    381
Đã học:      142  (37%)
━━━━━━━━━━━━━━━━━━━━
🔵 New:      239
🟡 Learning:   8
🟠 Young:     67
🟢 Mature:    67
⚠️ Leech:      3 (gõ /leech để xem)

🔥 Streak: 5 ngày
📅 Đến hạn hôm nay: 12
📅 Đến hạn ngày mai: 7

Tổng lượt ôn: 1,247
Tỉ lệ nhớ:   78% (Good + Easy / tổng)
```

---

## 10. Lộ trình triển khai

### Giai đoạn 1 — MVP (1–2 tuần)

- [ ] Setup bot, kết nối DB, tạo đủ schema.
- [ ] Nạp dữ liệu từ file JSON/mapping vào SQLite.
- [ ] Implement luồng học thủ công qua `/today`.
- [ ] Implement 4 nút đánh giá + tính SM-2 + lưu `review_state`.
- [ ] File_id caching cho ảnh.
- [ ] Anti-flood debounce.

### Giai đoạn 2 — Tự động hoá (1 tuần)

- [ ] Thêm APScheduler: job 9h, 13h, 20h30.
- [ ] Vacation mode.
- [ ] Giới hạn backlog (max review/ngày).
- [ ] Thống kê `/stats` cơ bản.
- [ ] Leech detection + suspend + `/leech`.

### Giai đoạn 3 — Tối ưu (ongoing)

- [ ] Cá nhân hoá số kanji mới/ngày per-user.
- [ ] Điều chỉnh độ khó theo tỉ lệ Again (nếu > 30% Again thì giảm thẻ mới).
- [ ] Backup tự động file SQLite hàng ngày.
- [ ] Inline query: `@bot LĂNG` tra nhanh bất kỳ lúc nào.
- [ ] Nâng cấp PostgreSQL nếu scale lên nhiều user.

---

## 11. Những điểm kỹ thuật cần nhớ

| Vấn đề | Giải pháp |
|--------|----------|
| Bot stateless | Bảng `active_session` trong SQLite |
| Callback data > 64 bytes | Chỉ lưu action, context đọc từ session |
| Double-click nút | Debounce bằng `last_callback_id` |
| Ảnh upload chậm | File_id caching |
| Backlog sau khi bỏ học | Giới hạn cứng max review/ngày + vacation mode |
| Thẻ học mãi không nhớ | Leech detection (lapses ≥ 5) → suspend |
| Thẻ dồn cùng ngày | Fuzz factor khi tính interval |
| Ease xuống quá thấp | Minimum ease = 1.3 |
| Job trùng lặp | Idempotent scheduler, check due_date trước khi gửi |
| Bot restart mất job | Catch-up job khi khởi động |
