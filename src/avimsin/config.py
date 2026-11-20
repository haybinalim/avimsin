"""Merkezi ayarlar: her şey `.env` dosyasından okunur.

Gizli bilgiler (API anahtarları, tokenlar) repoya ASLA yazılmaz.
"""

from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

# `RPC_<AD>_<SONEK>` biçimli ek ayarlar uç değildir.
RPC_OPTION_SUFFIXES = ("_profile", "_quota", "_rps", "_period_days", "_period_start")


def default_usage_db() -> str:
    """Sayaç DB'si proje köküne göre mutlak yol (cwd'den bağımsız; servis ve tarama aynı dosyayı görür).

    Kök bulunamazsa (kurulu paket) cwd'ye düşülür.
    """
    root = Path(__file__).resolve().parents[2]
    base = root if (root / "pyproject.toml").exists() else Path.cwd()
    return str(base / "data" / "rpc_usage.sqlite")


@dataclass(frozen=True)
class Settings:
    """Uygulama ayarları."""

    rpc_endpoints: dict[str, str] = field(default_factory=dict)
    # RPC havuzu ek ayarları (`RPC_SOLANA_<AD>_PROFILE/_QUOTA/_RPS/_PERIOD_DAYS`,
    # `RPC_SOLANA_ORDER`); uç sayılmaz. Anahtarlar küçük harf, `rpc_` öneksiz.
    rpc_options: dict[str, str] = field(default_factory=dict)
    # Kotalı sağlayıcıların ortak kullanım sayacı (süreçler arası paylaşılır).
    rpc_usage_db: str = field(default_factory=lambda: default_usage_db())
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
    # Canlı izleme yüksek frekans kuralı: son bot_window içinde bundan FAZLA farklı giriş
    # kaydı olan cüzdan sinyal sayımına katılmaz (kalıcı işaret yok; pencere kayınca döner).
    watch_bot_window_seconds: int = 3600
    watch_bot_max_inflows: int = 10
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
    # Otomatik keşif (avimsin-discover): aday coin kuralı.
    discovery_min_age_days: float = 3
    discovery_max_age_days: float = 30
    discovery_min_mcap_usd: float = 1_000_000
    discovery_min_liquidity_usd: float = 100_000
    discovery_min_volume_24h_usd: float = 250_000
    # Otomatik keşif (avimsin-pipeline): tur başına coin sayısı ve coin başına süre sınırı (saniye).
    discovery_max_coins_per_run: int = 3
    pipeline_coin_timeout_seconds: int = 10800

    @classmethod
    def from_env(cls) -> "Settings":
        """Ortam değişkenlerinden (ve `.env` dosyasından) ayarları toplar."""
        rpc: dict[str, str] = {}
        rpc_options: dict[str, str] = {}
        for key, value in os.environ.items():
            if key.startswith("RPC_") and value.strip():
                name = key.removeprefix("RPC_").lower()
                if name == "usage_db":
                    continue
                if name == "solana_order" or name.endswith(RPC_OPTION_SUFFIXES):
                    rpc_options[name] = value.strip()
                else:
                    rpc[name] = value.strip()

        threshold = int(os.environ.get("SMART_WALLET_SIGNAL_THRESHOLD", "3"))
        max_slots = os.environ.get("WATCH_MAX_SLOTS_PER_POLL", "").strip()
        ignored = os.environ.get("WATCH_IGNORED_MINTS", "").strip()
        extra = {}
        if ignored:
            extra["watch_ignored_mints"] = tuple(m.strip() for m in ignored.split(",") if m.strip())
        return cls(
            watch_solana_mode=os.environ.get("WATCH_SOLANA_MODE", "wallet").strip().lower(),
            watch_max_tx_per_wallet_poll=int(os.environ.get("WATCH_MAX_TX_PER_WALLET_POLL", "50")),
            watch_bot_window_seconds=int(os.environ.get("WATCH_BOT_WINDOW_SECONDS", "3600")),
            watch_bot_max_inflows=int(os.environ.get("WATCH_BOT_MAX_INFLOWS", "10")),
            **extra,
            rpc_endpoints=rpc,
            rpc_options=rpc_options,
            rpc_usage_db=os.environ.get("RPC_USAGE_DB", "").strip() or default_usage_db(),
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
            discovery_min_age_days=float(os.environ.get("DISCOVERY_MIN_AGE_DAYS", "3")),
            discovery_max_age_days=float(os.environ.get("DISCOVERY_MAX_AGE_DAYS", "30")),
            discovery_min_mcap_usd=float(os.environ.get("DISCOVERY_MIN_MCAP_USD", "1000000")),
            discovery_min_liquidity_usd=float(
                os.environ.get("DISCOVERY_MIN_LIQUIDITY_USD", "100000")
            ),
            discovery_min_volume_24h_usd=float(
                os.environ.get("DISCOVERY_MIN_VOLUME_24H_USD", "250000")
            ),
            discovery_max_coins_per_run=int(os.environ.get("DISCOVERY_MAX_COINS_PER_RUN", "3")),
            pipeline_coin_timeout_seconds=int(
                os.environ.get("PIPELINE_COIN_TIMEOUT_SECONDS", "10800")
            ),
        )


settings = Settings.from_env()
