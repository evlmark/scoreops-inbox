"""Авторизация: проверка Google ID-token через публичный endpoint tokeninfo + домен whitelist.
Используем httpx (уже в зависимостях), без отдельной google-auth библиотеки."""
import os
import time
import httpx

GOOGLE_CLIENT_ID = os.getenv(
    "GOOGLE_CLIENT_ID",
    "151265251444-ch0muuoh80tsq9eerfqej6osgdjfe45h.apps.googleusercontent.com",
)

ALLOWED_DOMAINS = set(
    d.strip().lower().lstrip("@")
    for d in os.getenv("ALLOWED_EMAIL_DOMAINS", "dif.tech,bancoplata.mx").split(",")
    if d.strip()
)

EXTENSION_API_KEY = os.getenv("EXTENSION_API_KEY", "scoreops-ext-d8f72b3a1c9e4f5b")

TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"

# Простой in-memory кэш: token → (claims, expires_at). TTL = ~9 min (Google tokens живут 1ч)
_token_cache: dict = {}
_CACHE_TTL = 540  # 9 minutes


def verify_google_token(token: str) -> dict:
    """Проверяет Google ID-token через tokeninfo endpoint + домен whitelist.
    Возвращает claims dict; бросает ValueError если что-то не так."""
    if not token or len(token) < 20:
        raise ValueError("Empty or invalid token")

    # Проверим кэш
    now = time.time()
    cached = _token_cache.get(token)
    if cached and cached[1] > now:
        return cached[0]

    # Чистим протухшие записи
    if len(_token_cache) > 500:
        for k, (_, exp) in list(_token_cache.items()):
            if exp <= now:
                _token_cache.pop(k, None)

    try:
        r = httpx.get(TOKENINFO_URL, params={"id_token": token}, timeout=8.0)
    except Exception as e:
        raise ValueError(f"Google verify failed: {e}")

    if r.status_code != 200:
        raise ValueError(f"Invalid token (status {r.status_code})")

    info = r.json()
    if info.get("aud") != GOOGLE_CLIENT_ID:
        raise ValueError("Token audience mismatch")
    if info.get("email_verified") not in (True, "true"):
        raise ValueError("Email not verified")

    email = (info.get("email") or "").lower()
    if "@" not in email:
        raise ValueError("No email in token")
    domain = email.rsplit("@", 1)[-1]
    if domain not in ALLOWED_DOMAINS:
        raise ValueError(f"Domain not allowed: @{domain}")

    _token_cache[token] = (info, now + _CACHE_TTL)
    return info
