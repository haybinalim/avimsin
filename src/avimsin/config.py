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
    # Eşik, bu süre içindeki farklı cüzdan sayısıyla değerlendirilir (saniye).
    watch_signal_window_seconds: int = 3600
    # Bildirimden sonra aynı coin için bu süre yalnız yeni cüzdanlar güncelleme getirir.
    watch_alert_cooldown_seconds: int = 86400
    # Solana izleme: "wallet" (varsayılan; cüzdan bazlı) ya da "coin" (eski, coin bazlı yol).
    watch_solana_mode: str = "wallet"
    # Cüzdan bazlı izleme: cüzdan başına tur başına en çok açılacak yeni işlem.
    watch_max_tx_per_wallet_poll: int = 50
    # Giriş sayılmayan mint'ler (wSOL, USDC, USDT).
    watch_ignored_mints: tuple[str, ...] = (
        "So11111111111111111111111111111111111111112",
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "Es9vMFrajbaCrUKu4KQPGcxwmWzQ2C26b2t3eG1wK8kq",
    )
    # filter/score'un kalıcı analiz penceresi uzunluğu (saniye); blok/slot sayısına
    # filters/chains.py BLOCK_INTERVAL_SECONDS ile çevrilir.
    analysis_window_seconds: int = 86400
    # Solana RPC: ardışık çağrılar arası asgari süre (saniye); public ucun ~1 istek/sn
    # sınırına göre 0.9. Ücretli uçta düşürülebilir.
    solana_min_interval_seconds: float = 0.9
    # Solana filter: cüzdanın pencere içi bu kadar işlemden fazlası açılmaz; cüzdan
    # high_frequency (bot) sayılır.
    solana_max_tx_per_wallet: int = 100
    # earliest_buyers: aynı işlemde aynı göndericiden bu kadar (veya fazla) farklı alıcıya
    # giden transferler içeriden dağıtım sayılır, alıcılar erken alıcı olmaz.
    distribution_min_receivers: int = 3

    @classmethod
    def from_env(cls) -> "Settings":
        """Ortam değişkenlerinden (ve `.env` dosyasından) ayarları toplar."""
        rpc: dict[str, str] = {}
        for key, value in os.environ.items():
            if key.startswith("RPC_") and value.strip():
                rpc[key.removeprefix("RPC_").lower()] = value.strip()

        threshold = int(os.environ.get("SMART_WALLET_SIGNAL_THRESHOLD", "3"))
        max_slots = os.environ.get("WATCH_MAX_SLOTS_PER_POLL", "").strip()
        ignored = os.environ.get("WATCH_IGNORED_MINTS", "").strip()
        extra = {}
        if ignored:
            extra["watch_ignored_mints"] = tuple(m.strip() for m in ignored.split(",") if m.strip())
        return cls(
            watch_solana_mode=os.environ.get("WATCH_SOLANA_MODE", "wallet").strip().lower(),
            watch_max_tx_per_wallet_poll=int(os.environ.get("WATCH_MAX_TX_PER_WALLET_POLL", "50")),
            **extra,
            rpc_endpoints=rpc,
            smart_wallet_signal_threshold=threshold,
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
            watch_poll_seconds=int(os.environ.get("WATCH_POLL_SECONDS", "30")),
            watch_max_slots_per_poll=int(max_slots) if max_slots else None,
            watch_signal_window_seconds=int(
                os.environ.get("WATCH_SIGNAL_WINDOW_SECONDS", "3600")
            ),
            analysis_window_seconds=int(os.environ.get("ANALYSIS_WINDOW_SECONDS", "86400")),
            solana_min_interval_seconds=float(
                os.environ.get("SOLANA_MIN_INTERVAL_SECONDS", "0.9")
            ),
            solana_max_tx_per_wallet=int(os.environ.get("SOLANA_MAX_TX_PER_WALLET", "100")),
            distribution_min_receivers=int(os.environ.get("DISTRIBUTION_MIN_RECEIVERS", "3")),
            watch_alert_cooldown_seconds=int(
                os.environ.get("WATCH_ALERT_COOLDOWN_SECONDS", "86400")
            ),
        )


settings = Settings.from_env()
