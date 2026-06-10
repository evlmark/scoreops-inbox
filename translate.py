"""Перевод реплик чатов и звонков с испанского на английский через DeepSeek."""
import os
import traceback
from typing import Optional
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

_client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
)

MODEL = "deepseek-chat"


def translate_to_english(text: str) -> Optional[str]:
    """Переводит испанский текст на английский. Возвращает None при ошибке или пустом тексте."""
    if not text or not text.strip():
        return None
    if not os.getenv("DEEPSEEK_API_KEY"):
        print("[translate] no DEEPSEEK_API_KEY")
        return None
    try:
        resp = _client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": "You are a translator. Translate the given Spanish text to natural English. Reply with ONLY the translation — no preamble, no quotes, no notes."},
                {"role": "user", "content": text},
            ],
            max_tokens=600,
            temperature=0.2,
        )
        out = (resp.choices[0].message.content or "").strip()
        return out or None
    except Exception as e:
        print(f"[translate] error: {type(e).__name__}: {e}")
        traceback.print_exc()
        return None
