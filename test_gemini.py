"""Test nhanh Gemini API - kiểm tra phản hồi cơ bản."""
from __future__ import annotations

import asyncio
from app.config import get_settings
from app.gemini_service import GeminiService


async def test_gemini_text_only():
    """Test hỏi không có ảnh."""
    settings = get_settings()
    print(f"Gemini enabled: {settings.gemini_ready}")
    print(f"API key prefix: {settings.gemini_api_key[:10]}...")
    print(f"Model: {settings.gemini_model}")

    if not settings.gemini_ready:
        print("\n⚠️ Gemini chưa được cấu hình. Vui lòng điền GEMINI_API_KEY trong file .env")
        return

    service = GeminiService(settings)
    
    # Test 1: Hỏi cơ bản
    print("\n📤 Test 1: Hỏi cơ bản...")
    response = await service.ask("Kanji 陵 nghĩa là gì? Cho ví dụ.")
    print(f"📥 Response:\n{response}\n")

    # Test 2: Hỏi với gợi ý phân tích
    print("\n📤 Test 2: Hỏi chuyên sâu hơn...")
    response2 = await service.ask(
        "Hãy giải thích cách đọc và ý nghĩa của từ 陵辱 trong tiếng Nhật. "
        "Cho 2 ví dụ cụ thể."
    )
    print(f"📥 Response:\n{response2}\n")
    
    print("✅ Tests completed!")


if __name__ == "__main__":
    asyncio.run(test_gemini_text_only())
