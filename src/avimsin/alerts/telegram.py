"""Telegram Bot API istemcisi — mesaj gönderimi.

Bot token'ı `.env`'den okunur (`TELEGRAM_BOT_TOKEN`); sohbet kimliği
`TELEGRAM_CHAT_ID`. Testler sahte sunucu üzerinden çalışır.
"""

from __future__ import annotations

import re

import httpx

from ..config import settings

API_BASE = "https://api.telegram.org"

_URL_RE = re.compile(r"https?://\S+")


def describe_error(exc: BaseException) -> str:
    """Log/özet için güvenli hata özeti.

    httpx hata mesajları tam URL taşır (Telegram bot token'ı, RPC API anahtarı);
    bu yüzden mesaj yazdırılmaz: HTTPStatusError -> "HTTP <kod>", diğerleri -> sınıf adı.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def redact_urls(text: str) -> str:
    """Metindeki her http(s) URL'ini '<url>' ile maskeler (yol/sorgu anahtar taşıyabilir)."""
    return _URL_RE.sub("<url>", text)


class TelegramClient:
    """Bot API'ye konuşan minimal istemci."""

    def __init__(self, token: str | None = None, base_url: str | None = None) -> None:
        self.token = token if token is not None else settings.telegram_bot_token
        self.base_url = base_url if base_url is not None else API_BASE
        self._http = httpx.Client(timeout=15)

    def send_message(self, chat_id: str | None = None, text: str = "") -> dict:
        """sendMessage çağrısı yapar; API yanıtını döndürür (hata olmadan)."""
        payload = {
            "chat_id": chat_id if chat_id is not None else settings.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        response = self._http.post(f"{self.base_url}/bot{self.token}/sendMessage", json=payload)
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._http.close()
