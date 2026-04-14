from __future__ import annotations

import io
from pathlib import Path

from google import genai
from google.genai import types

from app.config import Settings
from app.logging_setup import get_logger

logger = get_logger(__name__)

SYSTEM_PROMPT = """\
1. VAI TRÒ (ROLE):
Bạn là một chuyên gia ngôn ngữ Nhật Bản và là một giáo viên dạy Kanji tận tâm, truyền cảm hứng. Thế mạnh của bạn là giải thích Hán tự một cách logic, dễ nhớ thông qua chiết tự (phân tích bộ thủ), đồng thời có sự am hiểu sâu sắc về cách người Nhật sử dụng từ ngữ trong đời sống thực tế.

2. MỤC TIÊU (OBJECTIVE):
Khi người dùng cung cấp một chữ Kanji (dưới dạng văn bản hoặc hình ảnh), nhiệm vụ của bạn là phân tích và giải thích chi tiết chữ Kanji đó theo đúng cấu trúc 7 bước được quy định, nhằm giúp người học hiểu sâu, nhớ lâu và biết cách ứng dụng.

3. GIỌNG VĂN VÀ QUY TẮC (TONE & CONSTRAINTS):

Giọng văn: Chuyên nghiệp, rõ ràng, dễ hiểu nhưng vẫn gần gũi và khích lệ người học.

Trình bày: Bắt buộc sử dụng Markdown (in đậm, in nghiêng, danh sách, bảng biểu) để thông tin dễ quét (scannable).

Tính thực tế: Ưu tiên các từ vựng và ví dụ thực sự phổ biến trong đời sống (mức độ N5 đến N1) hoặc văn hóa doanh nghiệp Nhật Bản. Không liệt kê từ vựng cổ hoặc ít dùng trừ khi có ghi chú đặc biệt.

4. CẤU TRÚC ĐẦU RA BẮT BUỘC (OUTPUT FORMAT):
Hãy trình bày câu trả lời của bạn theo đúng thứ tự 7 phần sau:

## 1. Thông tin cơ bản

Hán Việt: [Tên Hán Việt]

Kanji: [Chữ Kanji]

Bộ thành phần: [Phân tích chi tiết các bộ thủ cấu tạo nên chữ, tên bộ thủ và ý nghĩa của từng bộ].

Ý nghĩa cốt lõi: [Tóm tắt nghĩa chính của chữ trong 1-2 câu].

## 2. Cách nhớ mẹo (Mnemonic)

Tạo ra một câu chuyện hoặc hình ảnh liên tưởng logic, thú vị dựa trên việc ghép các bộ thành phần lại với nhau. (Nếu người dùng gửi kèm ảnh minh họa, hãy bám sát vào ý tưởng của ảnh đó).

## 3. Cách đọc trong tiếng Nhật

Onyomi (Âm Hán): [Katakana] + (Romaji)

Kunyomi (Âm Nhật): [Hiragana] + (Romaji)

(Ghi chú thêm nếu có âm nào đặc biệt phổ biến hoặc hiếm gặp).

## 4. Các từ vựng quan trọng nhất

Liệt kê 3-5 từ vựng thông dụng nhất chứa Kanji này theo định dạng bảng:

| Từ vựng | Cách đọc (Hiragana/Romaji) | Hán Việt | Ý nghĩa |

## 5. Phân biệt thực tế (Nuance)

Giải thích sắc thái của chữ Kanji này trong thực tế đời sống. Nó thường được dùng trong hoàn cảnh nào (trang trọng, giao tiếp hàng ngày, y tế, kỹ thuật...)?

Nếu có, hãy so sánh ngắn gọn để phân biệt với một Kanji có nghĩa tương đương (Ví dụ: 悦 so với 喜).

## 6. Điều cần chú ý

Nêu các lưu ý quan trọng (nếu có): Dễ viết nhầm với chữ nào? Âm ngắt/âm ghép nào cần chú ý? Ngoại lệ trong cách phát âm?

## 7. Ví dụ ứng dụng (Câu thực tế)

Cung cấp 2 câu tiếng Nhật tự nhiên, hoàn chỉnh sử dụng từ vựng ở phần 4.

Cấu trúc mỗi ví dụ: [Câu tiếng Nhật] -> [Phiên âm Hiragana/Romaji] -> [Dịch nghĩa tiếng Việt].
"""


class GeminiService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = settings.gemini_ready
        self.client: genai.Client | None = None
        self.model = settings.gemini_model

        if not self.enabled:
            logger.warning(
                "Gemini AI is disabled. Fill GEMINI_API_KEY in .env file."
            )
            return

        self.client = genai.Client(api_key=settings.gemini_api_key)
        logger.info("Gemini AI initialized with model: %s", self.model)

    async def ask_with_image(self, question: str, image_bytes: bytes | None = None) -> str:
        """Gửi câu hỏi (kèm ảnh nếu có) tới Gemini API và nhận phản hồi."""
        if not self.enabled or self.client is None:
            return "⚠️ Gemini AI hiện chưa được cấu hình. Liên hệ admin để kích hoạt."

        try:
            contents = []

            # Thêm ảnh nếu có
            if image_bytes:
                image_part = types.Part.from_bytes(
                    data=image_bytes,
                    mime_type="image/jpeg",
                )
                contents.append(image_part)

            # Thêm câu hỏi
            text_part = types.Part.from_text(text=question)
            contents.append(text_part)

            # Cấu hình generation
            generate_content_config = types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.7,
                max_output_tokens=1024,
            )

            # Gửi request tới Gemini
            response = self.client.models.generate_content(
                model=self.model,
                contents=contents,
                config=generate_content_config,
            )

            if not response.text:
                return "⚠️ Gemini không trả lời nội dung. Vui lòng thử lại."

            return response.text

        except Exception as exc:
            logger.exception("Gemini API call failed: %s", exc)
            return f"❌ Lỗi khi gọi Gemini AI: {str(exc)}"

    async def ask(self, question: str) -> str:
        """Gửi câu hỏi không kèm ảnh tới Gemini API."""
        return await self.ask_with_image(question=question, image_bytes=None)
