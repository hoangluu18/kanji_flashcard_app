# Phan tich giai phap bot Telegram hoc Kanji N2

## 1. Tom tat bai toan
- Muc tieu: hoc Kanji N2 bang flashcard tren Telegram chatbot.
- Kieu hoc mong muon: theo tu duy Anki (spaced repetition), co danh gia muc do nho.
- Rang buoc giao dien: khong lat the nhu app Anki, ma dung 2 anh:
  - Header: mat truoc (goi y)
  - Card: mat sau (day du thong tin)
- Luu tru: local.
- Tu dong gui bai trong ngay: vi du 9h sang gui 10 kanji moi.

## 2. Giai phap tot nhat de chon
Giai phap nen chon la: 
- Don vi hoc chinh la Kanji, khong phai tung file card don le.
- Moi Kanji co 1 header lam mat truoc.
- Mat sau co the co 1 hoac nhieu card (truong hop 1 Kanji nhieu card).
- Lap lich gui hoc va on tap theo ngay, ket hop thuat toan SM-2 (Anki-like).

Ly do:
- Giu duoc logic hoc theo nho dai han.
- Giai quyet duoc truong hop 1 Kanji co nhieu card ma khong nhan doi khoi luong hoc vo ich.
- Van tuong thich voi mapping hien tai cua ban.

## 3. Thiet ke nghiep vu hoc tap
### 3.1 Don vi du lieu
- 1 Kanji = 1 muc hoc.
- 1 Kanji co:
  - 1 header image (mat truoc)
  - 1..n card images (mat sau)

### 3.2 Luong hoc cho 1 the
1. Bot gui header image.
2. Nguoi hoc bam nut Lat the.
3. Bot gui card image:
   - Neu Kanji co nhieu card thi gui lan luot theo nut Xem tiep.
4. Bot hien 4 nut danh gia: Again, Hard, Good, Easy.
5. Bot cap nhat lich on tiep theo thuat toan SRS.

### 3.3 Truong hop du lieu dac biet
- 1 Kanji nhieu card (vi du kanji 76 co card 0078 va 0079):
  - Van la 1 muc hoc, hien thi nhieu mat sau.
- 1 card map nhieu kanji (co xuat hien trong mapping):
  - Van tao lien ket theo mapping.
  - Khi hoc theo Kanji, card do duoc tai su dung cho tung Kanji lien quan.

## 4. Kien truc he thong de xai lau dai
## 4.1 Stack de xuat
- Python
- python-telegram-bot
- SQLite local (1 file database)
- APScheduler cho job gui tu dong

## 4.2 Luu tru local (khuyen nghi SQLite)
Khong nen chi dung JSON cho trang thai hoc, vi se kho mo rong va de loi khi bot dang chay.

Nen co cac bang:
- users: thong tin nguoi dung Telegram.
- kanji: id kanji, duong dan header.
- kanji_cards: lien ket kanji voi card image (1-n).
- review_state: trang thai hoc theo user + kanji (ease, interval, due, reps, lapses).
- review_log: lich su moi lan danh gia.
- user_settings: gio hoc, so kanji moi moi ngay, gio nhac.

## 4.3 Tai sao SQLite la hop ly
- De dung local.
- Nhanh, on dinh, backup de.
- Ho tro nhieu user neu sau nay ban muon cho ban be cung dung.

## 5. Lap lich hoc va on tap trong ngay
Lich de xuat:
- 09:00: gui goi hoc moi (vi du 10 Kanji moi).
- 13:00: nhac neu con the den han chua hoc.
- 20:30: nhac on tap cuoi ngay neu con no.

Nguyen tac quan trong:
- Job can idempotent (chay lap lai khong tao trung bai).
- Dung timezone co dinh (Asia/Ho_Chi_Minh).
- Khi bot restart, can co co che bu job bo lo (catch-up trong ngay).

## 6. SRS theo huong Anki
Dung bien the SM-2:
- Again: quay lai som (trong ngay hoac 1 ngay).
- Hard: tang nhe interval.
- Good: interval chuan.
- Easy: interval tang nhanh hon.

Nen theo doi:
- ease factor
- reps
- interval
- due date
- lapses

Neu muon don gian ban dau:
- New -> 1d
- Good lan 2 -> 3d
- Good lan 3 -> 7d
- Sau do nhan he so ease
Sau khi chay on dinh thi nang cap day du SM-2.

## 7. Thiet ke trai nghiem Telegram
Nen dung inline keyboard:
- Lat the
- Xem card tiep theo (neu co nhieu card)
- Again, Hard, Good, Easy
- Bo qua
- Tam dung phien hoc

Them cac lenh co ban:
- /start
- /today
- /review
- /stats
- /settings

Nen gioi han toc do gui anh de tranh flood.

## 8. Lo trinh trien khai an toan
### Giai doan 1 (MVP)
- Nap du lieu tu mapping card/header.
- Hoc thu cong bang lenh /today.
- Danh gia 4 nut va luu review_state.

### Giai doan 2 (Tu dong hoa)
- Them scheduler 9h, 13h, 20h30.
- Them thong ke co ban: so the da hoc, streak, due count.

### Giai doan 3 (Toi uu)
- Ca nhan hoa so Kanji moi moi ngay.
- Dieu chinh kho theo ti le Again.
- Them backup tu dong file SQLite.

## 9. Ket luan
Huong toi uu cho ban la:
- Dung Telegram bot + SQLite local + SM-2.
- Don vi hoc la Kanji (1 header, nhieu card neu can).
- Scheduler gui bai moi va bai on theo gio co dinh.
- Giu mapping card/header hien tai, khong can doi cau truc du lieu lon.

Neu ban muon, buoc tiep theo minh co the giup ban viet dac ta ky thuat chi tiet (schema database, API handler va flow callback) truoc khi bat dau code.
