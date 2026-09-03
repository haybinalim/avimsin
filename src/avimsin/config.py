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

    @classmethod
    def from_env(cls) -> "Settings":
        """Ortam değişkenlerinden (ve `.env` dosyasından) ayarları toplar."""
        rpc: dict[str, str] = {}
        for key, value in os.environ.items():
            if key.startswith("RPC_") and value.strip():
                rpc[key.removeprefix("RPC_").lower()] = value.strip()

        threshold = int(os.environ.get("SMART_WALLET_SIGNAL_THRESHOLD", "3"))
        return cls(rpc_endpoints=rpc, smart_wallet_signal_threshold=threshold)


settings = Settings.from_env()
