"""Merkezi ayarlar: her şey `.env` dosyasından okunur.

Gizli bilgiler (API anahtarları, tokenlar) repoya ASLA yazılmaz.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    """Uygulama ayarları."""

    rpc_endpoints: dict[str, str] = field(default_factory=dict)
    smart_wallet_signal_threshold: int = 3
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    watch_poll_seconds: int = 30
    # Tek turda geriye dönük taranacak en fazla aralık (None = sınır yok).
    # Solana ~2.5 slot/sn üretir; servis durup yeniden başlarsa biriken aralık
    # imza sayfalama şişmesine yol açar — sınır aşılırsa tur atlanır, güncele geçilir.
    watch_max_slots_per_poll: int | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        """Ortam değişkenlerinden (ve `.env` dosyasından) ayarları toplar."""
        rpc: dict[str, str] = {}
        for key, value in os.environ.items():
            if key.startswith("RPC_") and value.strip():
                rpc[key.removeprefix("RPC_").lower()] = value.strip()

        threshold = int(os.environ.get("SMART_WALLET_SIGNAL_THRESHOLD", "3"))
        max_slots = os.environ.get("WATCH_MAX_SLOTS_PER_POLL", "").strip()
        return cls(
            rpc_endpoints=rpc,
            smart_wallet_signal_threshold=threshold,
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
            watch_poll_seconds=int(os.environ.get("WATCH_POLL_SECONDS", "30")),
            watch_max_slots_per_poll=int(max_slots) if max_slots else None,
        )


settings = Settings.from_env()
