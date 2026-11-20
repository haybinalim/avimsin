"""Solana RPC sağlayıcı havuzu ve kotalı ortak sayaç.

Her sağlayıcının (helius, chainstack, drpc, alchemy, public) kotası kodla zorlanan
sert bir sınırla kesilir: ücretsiz limitin EN ÇOK %99'u (HARD_LIMIT_PERCENT).
İstek gönderilmeden ÖNCE birim ayrılır (atomik, SQLite BEGIN IMMEDIATE); sayaç
yazılamazsa istek GÖNDERİLMEZ (RpcUsageError) — kotasız devam etmek ücretliye
taşma riskidir.

Gizlilik: URL ve API anahtarı ne loga, ne hata metnine, ne DB'ye yazılır;
sağlayıcı kimliği URL'den türetilen kararlı bir hash'tir, ekranda `.env` adı görünür.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# Kullanıcının kesin kuralı: ücretsiz limitin en çok %99'u. Artırılmaz.
HARD_LIMIT_PERCENT = 99
WARN_PERCENTS = (80, 95)
DAY_SECONDS = 86400.0
DB_RETRIES = 3
DB_RETRY_WAIT = 0.2

# 429 gövdesinde bunlardan biri varsa sağlayıcı dönem sonuna kadar tükenmiş sayılır.
QUOTA_MARKERS = ("max usage", "monthly limit", "monthly capacity limit", "quota exceeded", "exceeded your")

DEFAULT_USAGE_DB = "data/rpc_usage.sqlite"
PROFILE_ORDER = ("helius", "chainstack", "drpc", "alchemy", "public")


@dataclass(frozen=True)
class Profile:
    quota: int | None  # dönem başına birim; None = kotasız
    rps: float | None  # saniyedeki istek; None = sınırsız
    period_days: int
    default_cost: int
    costs: dict[str, int] = field(default_factory=dict)


# Maliyetler araştırma bulgularıdır; doğrulanamayanlar güvenli (yüksek) tarafta.
PROFILES: dict[str, Profile] = {
    "helius": Profile(1_000_000, 10.0, 30, 1),
    "chainstack": Profile(3_000_000, 5.0, 30, 2),
    "drpc": Profile(210_000_000, None, 30, 20),
    # Alchemy saniyede 300 CU: en pahalı yöntem (40 CU) ile 7.5 istek/sn.
    "alchemy": Profile(
        30_000_000, 7.5, 30, 40,
        {"getAccountInfo": 10, "getSlot": 20},
    ),
    "public": Profile(None, None, 30, 1),  # pacing: settings.solana_min_interval_seconds
}

URL_HINTS = (
    ("helius-rpc.com", "helius"),
    ("rpc.helius.xyz", "helius"),
    ("chainstack.com", "chainstack"),
    ("p2pify.com", "chainstack"),
    ("drpc.org", "drpc"),
    ("alchemy.com", "alchemy"),
)
# Yalnız bunlar OTOMATİK kotasız (public) sayılır; başka alan adı açık _PROFILE ister.
PUBLIC_HOSTS = ("api.mainnet-beta.solana.com", "api.devnet.solana.com", "api.testnet.solana.com")


class RpcUsageError(RuntimeError):
    """Kota sayacı okunamadı/yazılamadı; istek gönderilmedi."""


class RpcPoolExhausted(RuntimeError):
    """Tüm sağlayıcıların kotası tükendi (ve kotasız public uç yok)."""


def _log(message: str) -> None:
    print(f"[rpc] {message}", file=sys.stderr, flush=True)


def guess_profile(url: str) -> str | None:
    """URL ana makinesinden profil; tanınmayan alan adı için None (sessizce public SAYILMAZ)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    if host in PUBLIC_HOSTS:
        return "public"
    for hint, profile in URL_HINTS:
        if host == hint or host.endswith("." + hint):
            return profile
    return None


def provider_id(url: str) -> str:
    """URL'den kararlı, geri çevrilemez kimlik (anahtar/adres DB'ye yazılmaz)."""
    return hashlib.sha256(b"avimsin-rpc:" + url.encode()).hexdigest()[:16]


@dataclass
class Provider:
    name: str
    profile: str
    url: str = field(repr=False)
    quota: int | None
    rps: float | None
    period_days: int
    default_cost: int
    costs: dict[str, int] = field(default_factory=dict)
    min_interval: float = 0.0
    period_anchor: float | None = None  # hesabın yenilenme günü (epoch, UTC 00:00)
    id: str = ""
    # süreç içi durum
    last_call: float | None = None
    cooldown_until: float = 0.0
    exhausted_until: float = 0.0  # duvar saati

    def __post_init__(self) -> None:
        if not self.id:
            self.id = provider_id(self.url)

    def cost(self, method: str) -> int:
        return self.costs.get(method, self.default_cost)

    @property
    def limit(self) -> int | None:
        """Sert sınır: kotanın %99'u (tam sayıya aşağı yuvarlanır)."""
        if self.quota is None:
            return None
        return self.quota * HARD_LIMIT_PERCENT // 100

    def period_start_for(self, now: float) -> float | None:
        """Çapa varsa `now`ı içeren dönemin başlangıcı; yoksa None (ilk kullanım)."""
        if self.period_anchor is None:
            return None
        return self.period_anchor + ((now - self.period_anchor) // self.period_seconds) * self.period_seconds

    @property
    def period_seconds(self) -> float:
        return self.period_days * DAY_SECONDS


def _opt(options: dict[str, str], name: str, suffix: str) -> str | None:
    value = options.get(f"{name}_{suffix}")
    return value.strip() if value and value.strip() else None


def build_providers(
    endpoints: dict[str, str],
    options: dict[str, str],
    public_interval: float,
    prefix: str = "solana",
) -> list[Provider]:
    """`RPC_SOLANA_*` uçlarından sağlayıcıları kurar; sıra: ORDER, sonra profil sırası."""
    providers: list[Provider] = []
    for name, url in endpoints.items():
        if not name.startswith(prefix):
            continue
        profile_name = _opt(options, name, "profile") or guess_profile(url)
        if profile_name is None:
            raise ValueError(
                f"RPC_{name.upper()}: sağlayıcı tanınmadı; RPC_{name.upper()}_PROFILE "
                f"açıkça verin ({', '.join(PROFILE_ORDER)}; kotasız kullanım için public)"
            )
        profile_name = profile_name.lower()
        if profile_name not in PROFILES:
            raise ValueError(
                f"RPC_{name.upper()}_PROFILE geçersiz (geçerli: {', '.join(PROFILE_ORDER)})"
            )
        profile = PROFILES[profile_name]
        try:
            quota = _opt(options, name, "quota")
            rps = _opt(options, name, "rps")
            days = _opt(options, name, "period_days")
            quota_v = int(quota) if quota else profile.quota
            rps_v = float(rps) if rps else profile.rps
            days_v = int(days) if days else profile.period_days
        except ValueError:
            raise ValueError(f"RPC_{name.upper()}_QUOTA/_RPS/_PERIOD_DAYS sayı olmalı") from None
        if (quota_v is not None and quota_v <= 0) or (rps_v is not None and rps_v <= 0) or days_v <= 0:
            raise ValueError(f"RPC_{name.upper()}_QUOTA/_RPS/_PERIOD_DAYS pozitif olmalı")
        anchor = None
        start_text = _opt(options, name, "period_start")
        if start_text:
            try:
                anchor = datetime.strptime(start_text, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                raise ValueError(f"RPC_{name.upper()}_PERIOD_START YYYY-MM-DD olmalı") from None
        interval = 1.0 / rps_v if rps_v else 0.0
        if profile_name == "public" and not rps:
            interval = public_interval
        providers.append(
            Provider(
                name=name, profile=profile_name, url=url, quota=quota_v, rps=rps_v,
                period_days=days_v, default_cost=profile.default_cost,
                costs=dict(profile.costs), min_interval=interval, period_anchor=anchor,
            )
        )
    providers.sort(key=lambda p: (PROFILE_ORDER.index(p.profile), p.name))
    order = [
        item.strip().lower().removeprefix(prefix + "_")
        for item in (options.get(f"{prefix}_order") or "").split(",") if item.strip()
    ]
    if order:
        def rank(p: Provider) -> int:
            short = p.name.removeprefix(prefix + "_")
            for i, wanted in enumerate(order):
                if wanted in (p.name, short):
                    return i
            return len(order)

        providers.sort(key=rank)  # kararlı sıralama: listede olmayanlar profil sırasını korur
    return providers


class UsageStore:
    """Süreçler arası ortak sayaç: `rpc_usage(provider, period_start, units)`, WAL."""

    def __init__(self, path: str | Path, wall=time.time, sleep=time.sleep) -> None:
        self.path = Path(path)
        self._wall = wall
        self._sleep = sleep
        self.created = not self.path.exists()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS rpc_usage ("
                    "provider TEXT PRIMARY KEY, period_start REAL NOT NULL, units INTEGER NOT NULL)"
                )
        except (sqlite3.Error, OSError):
            raise RpcUsageError("RPC kota sayacı açılamadı") from None

    def warn_if_new(self, providers: list[Provider]) -> None:
        """Sayaç DB'si yeni oluştuysa ve kotalı sağlayıcı varsa tek seferlik uyarı."""
        if self.created and any(p.quota is not None for p in providers):
            self.created = False
            _log("UYARI: kullanım sayacı sıfırdan başladı; sağlayıcı panelinden kullanımı kontrol edin")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10.0, isolation_level=None)

    def _tx(self, fn):
        """BEGIN IMMEDIATE altında fn(conn); kilit hatasında sınırlı yeniden dener."""
        for attempt in range(DB_RETRIES):
            conn = None
            try:
                conn = self._connect()
                conn.execute("BEGIN IMMEDIATE")
                try:
                    result = fn(conn)
                    conn.execute("COMMIT")
                    return result
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            except sqlite3.Error:
                self._sleep(DB_RETRY_WAIT)
            finally:
                if conn is not None:
                    conn.close()
        raise RpcUsageError("RPC kota sayacı yazılamadı; istek gönderilmedi") from None

    def _row(self, conn, provider: Provider, now: float) -> tuple[float, int]:
        """Geçerli dönem satırı; dönem bittiyse ya da yoksa sıfırlanmış yenisi."""
        row = conn.execute(
            "SELECT period_start, units FROM rpc_usage WHERE provider=?", (provider.id,)
        ).fetchone()
        aligned = provider.period_start_for(now)
        expired = row is None or (
            row[0] != aligned if aligned is not None else now >= row[0] + provider.period_seconds
        )
        if expired:
            start = now if aligned is None else aligned
            conn.execute(
                "INSERT OR REPLACE INTO rpc_usage(provider, period_start, units) VALUES (?,?,0)",
                (provider.id, start),
            )
            return start, 0
        return row[0], row[1]

    def reserve(self, provider: Provider, cost: int) -> tuple[bool, int, int, float]:
        """Atomik ayırma. (kabul, önceki, sonraki, dönem_bitişi) döner.

        Kabul yalnız `kullanım + maliyet <= %99 sınırı` ise; aksi halde sayaç artmaz.
        """
        assert provider.quota is not None and provider.limit is not None

        def work(conn):
            now = self._wall()
            start, units = self._row(conn, provider, now)
            end = start + provider.period_seconds
            if units + cost > provider.limit:
                return False, units, units, end
            conn.execute(
                "UPDATE rpc_usage SET units = units + ? WHERE provider=?", (cost, provider.id)
            )
            return True, units, units + cost, end

        return self._tx(work)

    def mark_exhausted(self, provider: Provider) -> float:
        """Sağlayıcıyı dönem sonuna kadar tükenmiş yapar (sayaç kotaya çekilir)."""
        assert provider.quota is not None

        def work(conn):
            start, units = self._row(conn, provider, self._wall())
            conn.execute(
                "UPDATE rpc_usage SET units=? WHERE provider=?",
                (max(units, provider.quota), provider.id),
            )
            return start + provider.period_seconds

        return self._tx(work)

    def read(self, provider: Provider) -> tuple[int, float | None]:
        """(kullanım, dönem_bitişi) — yazmaz; dönem bittiyse/yoksa (0, None)."""
        if not self.path.exists():
            return 0, None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT period_start, units FROM rpc_usage WHERE provider=?", (provider.id,)
                ).fetchone()
        except sqlite3.Error:
            raise RpcUsageError("RPC kota sayacı okunamadı") from None
        now = self._wall()
        aligned = provider.period_start_for(now)
        if row is None or (
            row[0] != aligned if aligned is not None else now >= row[0] + provider.period_seconds
        ):
            return 0, None
        return row[1], row[0] + provider.period_seconds


class RpcPool:
    """Sağlayıcı seçimi: sıra, RPS, kota (önce ayır), 429 geri çekilmesi."""

    def __init__(self, providers: list[Provider], store: UsageStore | None, wall=time.time) -> None:
        if not providers:
            raise RpcPoolExhausted("Tanımlı Solana RPC sağlayıcısı yok")
        self.providers = providers
        self.store = store
        self._wall = wall
        self._fallback_logged = False

    def acquire(self, method: str, now: float) -> tuple[Provider | None, float]:
        """(sağlayıcı, 0) ya da (None, bekleme_sn). Kota ayrılmış halde döner.

        Hiçbir sağlayıcı bir daha uygun değilse (hepsi kota tükendi) RpcPoolExhausted.
        """
        wait: float | None = None
        quota_skipped = False
        for p in self.providers:
            if p.cooldown_until > now:
                wait = _min(wait, p.cooldown_until - now)
                continue
            if p.last_call is not None and p.min_interval > 0:
                remaining = p.min_interval - (now - p.last_call)
                if remaining > 0:
                    wait = _min(wait, remaining)
                    continue
            if p.quota is not None:
                if p.exhausted_until > self._wall():
                    quota_skipped = True
                    continue
                ok, before, after, end = self.store.reserve(p, p.cost(method))
                if not ok:
                    p.exhausted_until = end
                    quota_skipped = True
                    continue
                self._warn(p, before, after)
            elif quota_skipped and not self._fallback_logged:
                self._fallback_logged = True
                _log(f"kotalı sağlayıcılar tükendi; {p.name} ({p.profile}) ucuna düşüldü")
            p.last_call = now
            return p, 0.0
        if wait is None:
            raise RpcPoolExhausted(
                "Tüm RPC sağlayıcılarının kotası tükendi (kotasız public uç tanımlı değil)"
            )
        return None, wait

    def _warn(self, p: Provider, before: int, after: int) -> None:
        for percent in WARN_PERCENTS:
            threshold = p.quota * percent // 100
            if before < threshold <= after:
                _log(f"UYARI: {p.name} ({p.profile}) kotasının %{percent}'ine ulaştı")

    def backoff(self, p: Provider, until: float) -> None:
        p.cooldown_until = max(p.cooldown_until, until)

    def exhaust(self, p: Provider) -> None:
        """429 gövdesi kota bitti diyor: dönem sonuna kadar kullanılmaz."""
        if p.quota is None:
            return
        p.exhausted_until = self.store.mark_exhausted(p)
        _log(f"{p.name} ({p.profile}) kotası tükendi (sağlayıcı bildirdi); dönem sonuna kadar atlanır")


def _min(current: float | None, value: float) -> float:
    return value if current is None else min(current, value)


def is_quota_body(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in QUOTA_MARKERS)


def build_pool(settings_obj) -> RpcPool | None:
    """Ayarlardan havuz kurar; Solana ucu yoksa None."""
    providers = build_providers(
        settings_obj.rpc_endpoints,
        getattr(settings_obj, "rpc_options", {}),
        settings_obj.solana_min_interval_seconds,
    )
    if not providers:
        return None
    # Kotasız (public) uçlarda sayaç yok: DB dosyası da açılmaz.
    store = None
    if any(p.quota is not None for p in providers):
        store = UsageStore(getattr(settings_obj, "rpc_usage_db", DEFAULT_USAGE_DB))
        store.warn_if_new(providers)
    return RpcPool(providers, store)
