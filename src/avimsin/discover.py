"""avimsin-discover: kurala uyan yeni Solana coin'lerini bulur ve discovery_queue'ya ekler.

Kaynaklar (anahtarsız): GeckoTerminal (trending/hacim/yeni havuzlar) ve DexScreener
(boost/profil listesi + pair detayı). Bir kaynak düşerse diğeri ile devam edilir;
ikisi de düşerse (ya da ikisi de hiç ham sonuç vermezse) çıkış kodu 1.

Kullanım::

    uv run avimsin-discover [--dry-run] [--db <yol>]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import httpx

from .config import settings
from .storage.db import connect as db_connect

CHAIN = "solana"
GECKO_BASE = "https://api.geckoterminal.com/api/v2/networks/solana"
DEXSCREENER_BASE = "https://api.dexscreener.com"
GECKO_CALLS_PER_MIN = 8  # sunucu sınırı 10/dk; 2 çağrı güvenlik payı
DEXSCREENER_CALLS_PER_MIN = 50
DEXSCREENER_TOKENS_PER_CALL = 30
GECKO_ENDPOINTS = (
    ("trending", "/trending_pools?include=base_token&page=1"),
    ("hacim", "/pools?sort=h24_volume_usd_desc&include=base_token&page=1"),
    ("hacim-s2", "/pools?sort=h24_volume_usd_desc&include=base_token&page=2"),
    ("yeni", "/new_pools?include=base_token&page=1"),
)


class RateLimiter:
    """Kayan 60 sn penceresinde en çok ``per_minute`` çağrı; dolunca bekletir."""

    def __init__(
        self,
        per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.per_minute = per_minute
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()

    def wait(self) -> None:
        now = self._clock()
        while self._calls and now - self._calls[0] >= 60:
            self._calls.popleft()
        if len(self._calls) >= self.per_minute:
            self._sleep(60 - (now - self._calls[0]))
            now = self._clock()
            while self._calls and now - self._calls[0] >= 60:
                self._calls.popleft()
        self._calls.append(now)


@dataclass(frozen=True)
class Rules:
    """Aday kuralı eşikleri (varsayılan: config/.env)."""

    min_age_days: float
    max_age_days: float
    min_mcap_usd: float
    min_liquidity_usd: float
    min_volume_24h_usd: float
    ignored_mints: frozenset[str]

    @classmethod
    def from_settings(cls) -> "Rules":
        return cls(
            min_age_days=settings.discovery_min_age_days,
            max_age_days=settings.discovery_max_age_days,
            min_mcap_usd=settings.discovery_min_mcap_usd,
            min_liquidity_usd=settings.discovery_min_liquidity_usd,
            min_volume_24h_usd=settings.discovery_min_volume_24h_usd,
            ignored_mints=frozenset(settings.watch_ignored_mints),
        )


@dataclass(frozen=True)
class Pair:
    """Bir token'ın tek bir havuzdaki ham metrikleri (kaynaktan bağımsız)."""

    mint: str
    source: str
    pair_address: str | None
    name: str | None
    created_at: datetime | None
    mcap_usd: float | None  # marketCap, yoksa fdv
    liquidity_usd: float | None
    volume_24h_usd: float | None
    price_change_24h: float | None


def _num(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _gecko_time(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_gecko_pools(payload: dict, source: str = "geckoterminal") -> list[Pair]:
    """GeckoTerminal havuz listesi → Pair (base token mint'i ilişkilerden)."""
    pairs: list[Pair] = []
    for pool in payload.get("data") or []:
        attrs = pool.get("attributes") or {}
        base_id = (
            (((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id")
        )
        if not base_id:
            continue
        mint = base_id.removeprefix("solana_")
        mcap = _num(attrs.get("market_cap_usd"))
        if mcap is None:
            mcap = _num(attrs.get("fdv_usd"))
        pairs.append(
            Pair(
                mint=mint,
                source=source,
                pair_address=attrs.get("address"),
                name=attrs.get("name"),
                created_at=_gecko_time(attrs.get("pool_created_at")),
                mcap_usd=mcap,
                liquidity_usd=_num(attrs.get("reserve_in_usd")),
                volume_24h_usd=_num((attrs.get("volume_usd") or {}).get("h24")),
                price_change_24h=_num((attrs.get("price_change_percentage") or {}).get("h24")),
            )
        )
    return pairs


def parse_dexscreener_pairs(payload: list) -> list[Pair]:
    """DexScreener /tokens/v1 yanıtı → Pair. Yalnız base token tarafı sayılır."""
    pairs: list[Pair] = []
    for item in payload or []:
        if item.get("chainId") != CHAIN:
            continue
        mint = (item.get("baseToken") or {}).get("address")
        if not mint:
            continue
        created_ms = _num(item.get("pairCreatedAt"))
        created = (
            datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc) if created_ms else None
        )
        mcap = _num(item.get("marketCap"))
        if mcap is None:
            mcap = _num(item.get("fdv"))
        base = item.get("baseToken") or {}
        pairs.append(
            Pair(
                mint=mint,
                source="dexscreener",
                pair_address=item.get("pairAddress"),
                name=base.get("symbol") or base.get("name"),
                created_at=created,
                mcap_usd=mcap,
                liquidity_usd=_num((item.get("liquidity") or {}).get("usd")),
                volume_24h_usd=_num((item.get("volume") or {}).get("h24")),
                price_change_24h=_num((item.get("priceChange") or {}).get("h24")),
            )
        )
    return pairs


def best_pair_per_mint(pairs: list[Pair]) -> dict[str, Pair]:
    """Aynı mint birden çok pair'de görünürse en likit olanı seçer."""
    best: dict[str, Pair] = {}
    for pair in pairs:
        current = best.get(pair.mint)
        if current is None or (pair.liquidity_usd or 0) > (current.liquidity_usd or 0):
            best[pair.mint] = pair
    return best


def evaluate(pair: Pair, rules: Rules, now: datetime) -> tuple[bool, str]:
    """Kuralı uygular; (uygun mu, gerekçe). Sınırlar dahildir (>= / <=)."""
    if pair.mint in rules.ignored_mints:
        return False, "yok sayılan mint (stable/wSOL)"
    if pair.created_at is None:
        return False, "yaş bilinmiyor"
    age_days = (now - pair.created_at).total_seconds() / 86400
    if age_days < rules.min_age_days:
        return False, f"çok yeni ({age_days:.1f} gün < {rules.min_age_days:g})"
    if age_days > rules.max_age_days:
        return False, f"çok eski ({age_days:.1f} gün > {rules.max_age_days:g})"
    if pair.mcap_usd is None or pair.mcap_usd < rules.min_mcap_usd:
        return False, f"piyasa değeri yetersiz ({pair.mcap_usd} < {rules.min_mcap_usd:g})"
    if pair.liquidity_usd is None or pair.liquidity_usd < rules.min_liquidity_usd:
        return False, f"likidite yetersiz ({pair.liquidity_usd} < {rules.min_liquidity_usd:g})"
    if pair.volume_24h_usd is None or pair.volume_24h_usd < rules.min_volume_24h_usd:
        return False, f"24s hacim yetersiz ({pair.volume_24h_usd} < {rules.min_volume_24h_usd:g})"
    return True, (
        f"yaş {age_days:.1f}g, mcap {pair.mcap_usd:,.0f}, likidite {pair.liquidity_usd:,.0f}, "
        f"24s hacim {pair.volume_24h_usd:,.0f}"
    )


@dataclass
class SourceResult:
    name: str
    ok: bool  # en az bir uç başarılı
    raw: int  # ham havuz/pair sayısı
    pairs: list[Pair]
    errors: list[str]


def _get_json(http: httpx.Client, limiter: RateLimiter, url: str, **kw):
    limiter.wait()
    response = http.get(url, **kw)
    response.raise_for_status()
    return response.json()


def fetch_gecko(http: httpx.Client, limiter: RateLimiter) -> SourceResult:
    pairs: list[Pair] = []
    errors: list[str] = []
    succeeded = 0
    for label, path in GECKO_ENDPOINTS:
        try:
            payload = _get_json(
                http, limiter, GECKO_BASE + path,
                headers={"Accept": "application/json;version=20230302"},
            )
            got = parse_gecko_pools(payload, source="geckoterminal")
        except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
            continue
        succeeded += 1
        print(f"  geckoterminal/{label}: {len(got)} ham havuz")
        pairs.extend(got)
    return SourceResult("geckoterminal", succeeded > 0, len(pairs), pairs, errors)


def fetch_dexscreener(http: httpx.Client, limiter: RateLimiter) -> SourceResult:
    errors: list[str] = []
    succeeded = 0
    mints: list[str] = []
    for label, path in (("boost", "/token-boosts/top/v1"), ("profil", "/token-profiles/latest/v1")):
        try:
            payload = _get_json(http, limiter, DEXSCREENER_BASE + path)
            found = [
                i["tokenAddress"] for i in payload
                if i.get("chainId") == CHAIN and i.get("tokenAddress")
            ]
        except (httpx.HTTPError, ValueError, AttributeError, TypeError, KeyError) as exc:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
            continue
        succeeded += 1
        print(f"  dexscreener/{label}: {len(found)} solana token")
        mints.extend(found)
    unique = list(dict.fromkeys(mints))
    pairs: list[Pair] = []
    detail_ok = 0
    for i in range(0, len(unique), DEXSCREENER_TOKENS_PER_CALL):
        chunk = unique[i : i + DEXSCREENER_TOKENS_PER_CALL]
        try:
            payload = _get_json(
                http, limiter, f"{DEXSCREENER_BASE}/tokens/v1/{CHAIN}/{','.join(chunk)}"
            )
            pairs.extend(parse_dexscreener_pairs(payload))
            detail_ok += 1
        except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
            errors.append(f"detay[{i}]: {type(exc).__name__}: {exc}")
    print(f"  dexscreener/detay: {len(pairs)} ham pair ({len(unique)} token)")
    # Liste uçları çalışıp detay hiç çalışmadıysa kaynak başarılı sayılmaz.
    ok = succeeded > 0 and (detail_ok > 0 or not unique)
    return SourceResult("dexscreener", ok, len(pairs), pairs, errors)


@dataclass
class Candidate:
    mint: str
    source: str
    reason: str
    metrics: dict


def select_candidates(
    results: list[SourceResult], rules: Rules, now: datetime
) -> tuple[list[Candidate], dict[str, int]]:
    """Kaynak sonuçlarını birleştirir, mint başına en likit pair'e kuralı uygular."""
    all_pairs = [p for r in results for p in r.pairs]
    best = best_pair_per_mint(all_pairs)
    candidates: list[Candidate] = []
    rejected: dict[str, int] = {}
    for mint, pair in best.items():
        ok, reason = evaluate(pair, rules, now)
        if not ok:
            key = reason.split(" (")[0]
            rejected[key] = rejected.get(key, 0) + 1
            continue
        metrics = asdict(pair)
        metrics["created_at"] = pair.created_at.isoformat() if pair.created_at else None
        metrics["age_days"] = round((now - pair.created_at).total_seconds() / 86400, 2)
        metrics["discovery_reason"] = reason
        metrics["dexscreener_url"] = f"https://dexscreener.com/solana/{mint}"
        candidates.append(Candidate(mint, pair.source, reason, metrics))
    candidates.sort(key=lambda c: c.metrics["volume_24h_usd"] or 0, reverse=True)
    return candidates, rejected


def known_coins(conn) -> set[str]:
    """coins tablosunda olan ya da kuyrukta bulunan mint'ler."""
    rows = conn.execute("SELECT address FROM coins").fetchall()
    rows += conn.execute("SELECT coin FROM discovery_queue").fetchall()
    return {r[0] for r in rows}


def enqueue(conn, candidates: list[Candidate], now: datetime) -> int:
    """Kuyruğa ekler; zaten coins'te ya da kuyrukta olanlar atlanır. Eklenen sayısı döner."""
    known = known_coins(conn)
    added = 0
    with conn:
        for c in candidates:
            if c.mint in known:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO discovery_queue"
                " (coin, chain, source, found_at, status, reason, metrics_json)"
                " VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (c.mint, CHAIN, c.source, now.isoformat(), c.reason, json.dumps(c.metrics)),
            )
            added += cur.rowcount
    return added


def run_discovery(
    conn,
    http: httpx.Client,
    rules: Rules,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    gecko_limiter: RateLimiter | None = None,
    dex_limiter: RateLimiter | None = None,
) -> tuple[int, list[Candidate]]:
    """Keşfi çalıştırır; (çıkış kodu, uygun adaylar) döner."""
    now = now or datetime.now(timezone.utc)
    gecko_limiter = gecko_limiter or RateLimiter(GECKO_CALLS_PER_MIN)
    dex_limiter = dex_limiter or RateLimiter(DEXSCREENER_CALLS_PER_MIN)
    results = [fetch_gecko(http, gecko_limiter), fetch_dexscreener(http, dex_limiter)]
    for r in results:
        for err in r.errors:
            print(f"UYARI: {r.name} {err}", file=sys.stderr)
        state = "OK" if r.ok else "DÜŞTÜ"
        print(f"kaynak {r.name}: {state}, {r.raw} ham sonuç")
        if r.ok and r.raw == 0:
            print(f"UYARI: {r.name} hata vermedi ama 0 ham sonuç döndü (şüpheli).", file=sys.stderr)
    live = [r for r in results if r.ok and r.raw > 0]
    if not live:
        print("HATA: hiçbir kaynaktan veri alınamadı; çıkış kodu 1.", file=sys.stderr)
        return 1, []
    candidates, rejected = select_candidates(live, rules, now)
    detail = ", ".join(f"{k}: {v}" for k, v in sorted(rejected.items())) or "-"
    print(f"{len(candidates)} aday kurala uydu; elenenler: {detail}")
    known = known_coins(conn)
    fresh = [c for c in candidates if c.mint not in known]
    print(f"{len(candidates) - len(fresh)} aday zaten coins/kuyrukta; {len(fresh)} yeni")
    if dry_run:
        print("--dry-run: kuyruğa yazılmadı.")
    else:
        added = enqueue(conn, fresh, now)
        print(f"{added} coin kuyruğa eklendi.")
    for c in fresh:
        print(f"  {c.mint}  [{c.source}] {c.reason}")
    return 0, fresh


def main(argv: list[str] | None = None) -> None:
    """CLI giriş noktası."""
    parser = argparse.ArgumentParser(
        description="Kurala uyan yeni Solana coin'lerini bulur ve discovery_queue'ya ekler."
    )
    parser.add_argument("--dry-run", action="store_true", help="Kuyruğa yazmadan adayları göster")
    parser.add_argument("--db", default="data/avimsin.sqlite", help="SQLite dosya yolu")
    args = parser.parse_args(argv)
    conn = db_connect(args.db)
    try:
        with httpx.Client(timeout=20) as http:
            code, _ = run_discovery(conn, http, Rules.from_settings(), dry_run=args.dry_run)
    finally:
        conn.close()
    sys.exit(code)
