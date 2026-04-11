# CASE STUDY: Triển khai Telegram Bot (FastAPI + SQLite) trên Azure VM với chi phí $0

Tài liệu này ghi lại toàn bộ quy trình và các "trick" để triển khai một ứng dụng Telegram Bot chạy bằng FastAPI và SQLite lên Azure Virtual Machine, tận dụng tối đa gói Azure for Students để đạt được chi phí vận hành $0/tháng.

## I. Phân tích bài toán & Lựa chọn kiến trúc

**Yêu cầu:**
- Chạy liên tục 24/7 (do có tính năng nhắc nhở bằng `APScheduler`).
- Lưu trữ dữ liệu liên tục không bị mất khi restart (dùng `SQLite`).
- Chi phí càng rẻ càng tốt (mục tiêu $0).

**Lựa chọn:** Cơ sở hạ tầng **Azure Virtual Machine (B1s)** kết hợp **Ngrok (Reverse Tunnel)**.

**Tại sao không chọn các Serverless (Render, Vercel...)?**
- Bản Free của Serverless thường bị "ngủ đông" (sleep) nếu không có request, làm chết tiến trình `APScheduler`.
- Ổ cứng thường là ephemeral (tạm thời), làm mất file `SQLite` mỗi lần restart.

**Tại sao dùng Ngrok?**
- Để tránh bị tính phí IP Public tĩnh của Azure (~$3.6/tháng). Ngrok tạo một đường hầm (tunnel) từ bên trong máy ảo ra ngoài Internet, cung cấp 1 domain HTTPS tĩnh miễn phí mà không cần mở port Public.

## II. Quy trình triển khai chi tiết

### Bước 1: Khởi tạo Azure Virtual Machine (Tận dụng gói Student)

1. Đăng nhập Azure Portal, tạo Virtual Machine mới.
2. Cấu hình **Basics**:
   - Region: `Southeast Asia` (hoặc `East Asia`).
   - Image: `Ubuntu Server 24.04 LTS - Gen2`.
   - Size: `Standard_B1s` (Được free 750 giờ/tháng).
   - Authentication: `Password`.
   - Public inbound ports: Chỉ cho phép `SSH (22)` để setup ban đầu.
3. Cấu hình **Disks** (Rất quan trọng):
   - OS disk size: Custom -> `64 GiB`.
   - OS disk type: `Premium SSD LRS`.
   *(Giải thích: Gói Student cho phép dùng 2 ổ P6 64GB Premium SSD miễn phí. Cấu hình này khớp 100% với gói free).*
4. Cấu hình **Networking**:
   - Tích chọn: `Delete public IP and NIC when VM is deleted` (để tránh rác tài nguyên sau này).
5. Review + Create. Sau khi tạo xong, lấy địa chỉ `Public IP` của máy.

### Bước 2: Tối ưu và Cài đặt môi trường trên Máy ảo (Ubuntu)

SSH vào máy ảo: `ssh username@<Public_IP>`

**1. Tạo Swap (Tránh Out of Memory cho máy 1GB RAM):**
```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

**2. Cài đặt Python & Git:**
```bash
sudo apt update
sudo apt install python3-pip python3-venv git -y
```

**3. Cài đặt Ngrok & Thêm Token:**
Cài đặt theo hướng dẫn chính thức từ Ngrok:
```bash
curl -s https://ngrok-agent.s3.amazonaws.com/ngrok.asc | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | sudo tee /etc/apt/sources.list.d/ngrok.list
sudo apt update
sudo apt install ngrok
```
Thêm authtoken lấy từ dashboard Ngrok của bạn:
```bash
ngrok config add-authtoken <YOUR_NGROK_TOKEN>
```

### Bước 3: Chuẩn bị Github & Đưa code lên Server

**1. Tạo `.gitignore` ở local (máy tính cá nhân) để bảo vệ dữ liệu nhạy cảm:**
```text
.env
.venv/
data/
*.sqlite3
__pycache__/
```
Đẩy code lên Github (Tạo repo public/private tùy ý). *Lưu ý*: Phải đảm bảo nhánh chính trên Github là `main` để tránh lỗi pull sau này.

**2. Clone code về máy ảo Ubuntu:**
```bash
cd ~
git clone <URL_GITHUB_REPO>
cd <thu_muc_du_an>
```

**3. Khởi tạo môi trường ảo (.venv) & Cài requirements:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p data # Thư mục chứa sqlite
```

### Bước 4: Thiết lập biến môi trường (.env) trên Server

Tạo file `.env` thủ công trên server (vì đã bị gitignore):
```bash
nano .env
```
Nội dung file `.env` cần lưu ý:
1.  `WEBHOOK_URL`: Trỏ về cái static domain của Ngrok.
2.  Đường dẫn SQLite (`SQLITE_DB_PATH`...) phải dùng **đường dẫn tuyệt đối** để SystemD không bị lỗi.
```env
WEBHOOK_URL=https://<ngrok-static-domain>.ngrok-free.dev/webhook
SQLITE_DB_PATH=/home/<user>/<thu_muc_du_an>/data/kanji_bot.sqlite3
...
```

### Bước 5: Chạy ngầm 24/7 với SystemD

Tạo 2 services: 1 cho Ngrok, 1 cho Bot FastAPI.

**1. Service Ngrok (`/etc/systemd/system/ngrok.service`):**
```ini
[Unit]
Description=Ngrok Static Tunnel Service
After=network.target

[Service]
User=<username>
ExecStart=/usr/local/bin/ngrok http --url=<ngrok-static-domain>.ngrok-free.dev 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

**2. Service Bot (`/etc/systemd/system/kanjibot.service`):**
*(Ghi chú: Đường dẫn `ExecStart` phải trỏ đến `python` trong thư mục ảo `.venv`)*
```ini
[Unit]
Description=Kanji Telegram Bot (FastAPI)
After=network.target ngrok.service

[Service]
User=<username>
WorkingDirectory=/home/<username>/<thu_muc_du_an>
Environment="PATH=/home/<username>/<thu_muc_du_an>/.venv/bin"
ExecStart=/home/<username>/<thu_muc_du_an>/.venv/bin/python run_api.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Kích hoạt các services:
```bash
sudo systemctl daemon-reload
sudo systemctl enable ngrok kanjibot
sudo systemctl start ngrok kanjibot
```

### Bước 6: Cấp Webhook cho Telegram

Gọi API nội bộ để bot đăng ký URL Webhook mới với Telegram Server:
```bash
curl -X POST "https://<ngrok-static-domain>.ngrok-free.dev/admin/telegram/set-webhook"
```
*Lưu ý:* Nếu có dùng Authentication Key bảo vệ, cần truyền thêm Header `-H "x-api-key: <KEY>"`.

---

## III. TUYỆT CHIÊU QUAN TRỌNG NHẤT: XÓA IP (THE $0 TRICK)

Server lúc này đã nhận được request từ Telegram thông qua Ngrok tunnel. Lúc này, **Public IP address** ban đầu cấp cho Azure VM trở nên thừa thãi. 
Hơn nữa, Public IP của Azure bị tính phí (khoảng $3.6/tháng).

**Thực thi:**
1. Trở lại Azure Portal.
2. Tìm đến tài nguyên `Public IP Address` đang dùng cho máy ảo.
3. Chọn **Dissociate** (để gỡ IP ra khỏi máy ảo).
4. Kiểm tra lại bot Telegram: Nếu bot vẫn hoạt động (nghĩa là Azure vẫn cấp Default Outbound Access mạng ra cho phép Ngrok duy trì tunnel), chuyển sang bước 5.
5. Chọn **Delete** cái Public IP address này đi.

**KẾT QUẢ:**
Phí Public IP = $0. Cấu hình máy B1s + SSD 64GB bao trọn trong gói Student.
Tổng bill cuối tháng: **$0.00**.

## IV. Xử lý Lỗi (Troubleshooting) thường gặp

1. **Lỗi `Address already in use` khi start service Kanjibot:**
   - *Nguyên nhân:* Trước đó đã chạy manual `python run_api.py` và chưa tắt, nó chiếm port 8000.
   - *Xử lý:* Chạy `sudo pkill -f python` để giết hết process python rồi restart service.
2. **SystemD đọc sai biến môi trường Admin Key / Webhook:**
   - *Nguyên nhân:* Lỗi định dạng do copy-paste từ Windows sang Linux (dư khoảng trắng, ký tự CR LF ẩn...).
   - *Xử lý:* Xóa tay file `.env` bằng lệnh `rm`, mở lại bằng `nano` gõ hoặc cẩn thận copy các biến string thuần (không có special chars gây escape lỗi).
3. **Mất kết nối hoàn toàn sau khi Dissociate IP:**
   - *Nguyên nhân:* Tài khoản Azure bị khóa chính sách mới không cho Default Outbound IPv4.
   - *Xử lý:* Chấp nhận tạo lại và gắn (Associate) Public IP vào lại Card mạng (NIC). (Tốn $3.6/tháng).

---

## V. HOÀN THIỆN HỆ THỐNG: TỰ ĐỘNG CẬP NHẬT CODE & .ENV (0-TOUCH CI/CD)

Vấn đề: Do máy ảo không có Public IP (để tiết kiệm $0), mỗi khi muốn cập nhật source code mới hoặc đổi cấu hình `.env`, admin sẽ lại phải "gắn và tháo" Public IP đi rất phiền phức.
Giải pháp: Sử dụng **Github Repository (cho Source code)** và **Github Secret Gist (cho file .env)** kết hợp `Cronjob` chạy định kỳ mỗi 2 tiếng. 

### Bước 1: Đẩy biến môi trường (.env) lên Github Secret Gist

Vì `.env` không được để lộ lên Github Public:
1. Đăng nhập [gist.github.com](https://gist.github.com/).
2. Tên file: gõ `.env`. Nội dung: copy dán toàn bộ `.env` từ local vào.
3. Bấm **Create secret gist**.
4. Mở file vừa tạo, bấm chọn nút **Raw**. 
5. Copy đường dẫn tĩnh của file (URL Raw dài dằng dặc).

### Bước 2: Tạo Script `auto_update.sh` tự động cập nhật mọi thứ

Trên máy ảo Ubuntu (chịu khó mở lại Public IP 1 lần cuối để thao tác):
```bash
nano /home/<username>/kanji_flashcard_app/auto_update.sh
```

Dán kịch bản sau, lưu ý thay `<username>` và cài `GIST_ENV_URL` bằng cái link RAW bạn vừa cấu hình:

```bash
#!/bin/bash

# THAY ĐỔI CÁC BIẾN SAU:
PROJECT_DIR="/home/pmshoanghot/kanji_flashcard_app"
GIST_ENV_URL="https://gist.githubusercontent.com/hoangluu18/a756cd09cc07054bd3cf80027c71a8d9/raw/272eb67cd30c26de1f515debbaf601ac9810dee4/gistfile1.txt"
# -------------------------

cd $PROJECT_DIR || exit
RESTART_NEEDED=0
LOG_FILE="$PROJECT_DIR/update.log"

echo "========== START UPDATE CHECK $(date) ==========" >> $LOG_FILE

# 1. Kiểm tra cập nhật .env từ Github Gist ẩn
curl -s -o .env.temp $GIST_ENV_URL
if ! cmp -s .env .env.temp; then
    echo "$(date): Có thay đổi trong file .env. Đang cập nhật..." >> $LOG_FILE
    mv .env.temp .env
    RESTART_NEEDED=1
else
    rm .env.temp
fi

# 2. Kiểm tra cập nhật Code từ Github
git fetch origin main
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse @{u})

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "$(date): Có code mới trên Github. Đang pull về..." >> $LOG_FILE
    git pull origin main
    
    echo "$(date): Cập nhật pip modules (nếu có)..." >> $LOG_FILE
    $PROJECT_DIR/.venv/bin/pip install -r requirements.txt
    
    RESTART_NEEDED=1
fi

# 3. Tự khởi động lại nếu có thay đổi
if [ $RESTART_NEEDED -eq 1 ]; then
    echo "$(date): Đang restart Kanjibot..." >> $LOG_FILE
    sudo systemctl restart kanjibot.service
    echo "$(date): Hoàn tất quá trình cập nhật!" >> $LOG_FILE
else
    echo "$(date): Không có thay đổi nào mới (Code & .env)." >> $LOG_FILE
fi

echo "==========================================================" >> $LOG_FILE
```

Lưu lại (Ctrl+O, Enter) và thoát, sau đó cấp quyền thực thi cho file:
```bash
chmod +x /home/<username>/kanji_flashcard_app/auto_update.sh
```

### Bước 3: Bypass Mật khẩu cho lệnh Restart service

Script chạy ngầm tự động sẽ không thể "nhập mật khẩu sudo" được. Phải bỏ qua bước đòi password khi nó muốn restart bot:
```bash
sudo visudo
```
Cuộn thẳng đến con trỏ dưới cùng, dán dòng này:
```text
<username> ALL=(ALL) NOPASSWD: /bin/systemctl restart kanjibot.service
```
Lưu lại (Ctrl+O, Enter, Ctrl+X).

### Bước 4: Hẹn giờ Cronjob chạy mỗi 2 tiếng

Gõ lệnh:
```bash
crontab -e
```
Thêm dòng này vào cuối cùng lịch trình:
```text
0 */2 * * * /home/<username>/kanji_flashcard_app/auto_update.sh
```
Lưu lại (Ctrl+O, Enter, Ctrl+X).

**TỔNG KẾT:** Giờ đây bạn đã có một hệ thống CI/CD hoàn hảo: 
- Muốn cập nhật tính năng mới? `git push`
- Muốn đổi Key Telegram / đổi địa chỉ Ngrok? Lên Github Gist thay đổi.
- **Không bao giờ cần SSH hay tốn bất cứ 1 Cent nào cho Public IP nữa!** Mọi thứ tự xử lý trong background.
