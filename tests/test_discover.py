"""avimsin-discover: kural sınırları, kaynak düşmesi, kuyruk tekilliği (ağ yok; sahte HTTP)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from avimsin import discover
from avimsin.discover import (
    Pair,
    RateLimiter,
    Rules,
    best_pair_per_mint,
    evaluate,
    parse_dexscreener_pairs,
    parse_gecko_pools,
    run_discovery,
)
from avimsin.storage.db import connect, save_coin

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 10, tzinfo=timezone.utc)
SOL = "So11111111111111111111111111111111111111112"
RULES = Rules(
    min_age_days=3, max_age_days=30, min_mcap_usd=1_000_000,
    min_liquidity_usd=100_000, min_volume_24h_usd=250_000,
    ignored_mints=frozenset({SOL}),
)
DCTY = "9hjBLBrc7WZW2H8UQvBAvG5WiTZotXUv2Bdcn4f4pump"  # fixture: gecko + dexscreener'da var


def load(name: str):
    return json.loads((FIX / name).read_text())


def good_pair(**kw) -> Pair:
    base = dict(
        mint="M" * 44, source="geckoterminal", pair_address="P", name="X",
        created_at=NOW - timedelta(days=10), mcap_usd=2_000_000, liquidity_usd=200_000,
        volume_24h_usd=500_000, price_change_24h=5.0,
    )
    base.update(kw)
    return Pair(**base)


# --- parser'lar (gerçek yanıt fixture'ları) ---

def test_gecko_fixture_parse():
    pairs = parse_gecko_pools(load("gecko_trending.json"))
    assert len(pairs) == 3
    dcty = next(p for p in pairs if p.mint == DCTY)
    assert dcty.mcap_usd == pytest.approx(18668290.63)  # market_cap null -> fdv
    assert dcty.liquidity_usd == pytest.approx(559827.3641)
    assert dcty.volume_24h_usd == pytest.approx(64573876.1477647)
    assert dcty.created_at == datetime(2026, 10, 20, 3, 3, 35, tzinfo=timezone.utc)


def test_dexscreener_fixture_parse():
    pairs = parse_dexscreener_pairs(load("dexscreener_tokens.json"))
    p = next(p for p in pairs if p.mint == DCTY)
    assert p.liquidity_usd == pytest.approx(561267.1)
    assert p.created_at is not None and p.created_at.tzinfo is not None


def test_dexscreener_null_created_at_is_unknown():
    item = dict(load("dexscreener_tokens.json")[0], pairCreatedAt=None)
    (p,) = parse_dexscreener_pairs([item])
    assert p.created_at is None
    assert evaluate(p, RULES, NOW) == (False, "yaş bilinmiyor")


def test_most_liquid_pair_wins_across_sources():
    pairs = parse_gecko_pools(load("gecko_trending.json")) + parse_dexscreener_pairs(
        load("dexscreener_tokens.json")
    )
    best = best_pair_per_mint(pairs)
    assert best[DCTY].source == "dexscreener"  # 561267 > 559827
    assert best[DCTY].liquidity_usd == pytest.approx(561267.1)


# --- kural sınırları: her eşiğin iki yanı ---

@pytest.mark.parametrize(
    "field,passing,failing",
    [
        ("created_at", NOW - timedelta(days=3), NOW - timedelta(days=3) + timedelta(seconds=1)),
        ("created_at", NOW - timedelta(days=30), NOW - timedelta(days=30, seconds=1)),
        ("mcap_usd", 1_000_000, 999_999),
        ("liquidity_usd", 100_000, 99_999),
        ("volume_24h_usd", 250_000, 249_999),
    ],
)
def test_rule_boundaries(field, passing, failing):
    assert evaluate(good_pair(**{field: passing}), RULES, NOW)[0] is True
    assert evaluate(good_pair(**{field: failing}), RULES, NOW)[0] is False


def test_ignored_mint_excluded():
    assert evaluate(good_pair(mint=SOL), RULES, NOW)[0] is False


def test_missing_mcap_fails():
    assert evaluate(good_pair(mcap_usd=None), RULES, NOW)[0] is False


# --- HTTP sahtesi ---

def fast_limiters():
    return dict(gecko_limiter=RateLimiter(10_000), dex_limiter=RateLimiter(10_000))


def make_http(gecko="ok", dex="ok", gecko_body=None, dex_boost=None, dex_tokens=None):
    gecko_body = gecko_body if gecko_body is not None else load("gecko_trending.json")
    dex_boost = dex_boost if dex_boost is not None else load("dexscreener_boosts.json")
    dex_tokens = dex_tokens if dex_tokens is not None else load("dexscreener_tokens.json")

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "api.geckoterminal.com":
            return httpx.Response(500) if gecko == "down" else httpx.Response(200, json=gecko_body)
        if host == "api.dexscreener.com":
            if dex == "down":
                return httpx.Response(503)
            if path.startswith("/tokens/v1/solana/"):
                return httpx.Response(200, json=dex_tokens)
            if path == "/token-boosts/top/v1":
                return httpx.Response(200, json=dex_boost)
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def queue_rows(conn):
    return conn.execute(
        "SELECT coin, chain, source, status, reason, metrics_json FROM discovery_queue"
    ).fetchall()


def test_discovery_enqueues_candidate_with_metrics():
    conn = connect(":memory:")
    code, fresh = run_discovery(conn, make_http(), RULES, now=NOW, **fast_limiters())
    assert code == 0
    rows = queue_rows(conn)
    assert [r[0] for r in rows] == [DCTY]
    assert rows[0][1:4] == ("solana", "dexscreener", "pending")
    metrics = json.loads(rows[0][5])
    assert metrics["volume_24h_usd"] > 250_000 and metrics["discovery_reason"]
    assert metrics["dexscreener_url"].endswith(DCTY)


def test_dry_run_writes_nothing():
    conn = connect(":memory:")
    code, fresh = run_discovery(conn, make_http(), RULES, dry_run=True, now=NOW, **fast_limiters())
    assert code == 0 and [c.mint for c in fresh] == [DCTY]
    assert queue_rows(conn) == []


def test_coin_already_in_coins_is_skipped():
    conn = connect(":memory:")
    save_coin(conn, DCTY, 1, chain="solana")
    conn.commit()
    code, fresh = run_discovery(conn, make_http(), RULES, now=NOW, **fast_limiters())
    assert code == 0 and fresh == []
    assert queue_rows(conn) == []


def test_coin_already_in_queue_not_added_twice():
    conn = connect(":memory:")
    run_discovery(conn, make_http(), RULES, now=NOW, **fast_limiters())
    conn.execute("UPDATE discovery_queue SET status = 'done', reason = 'ok=2'")
    conn.commit()
    run_discovery(conn, make_http(), RULES, now=NOW, **fast_limiters())
    rows = queue_rows(conn)
    assert len(rows) == 1 and rows[0][3] == "done" and rows[0][4] == "ok=2"


def test_age_rule_applied_end_to_end():
    conn = connect(":memory:")
    # Fixture pair'i NOW'da ~21 günlük: +60 gün çok eski, -19 gün (~2 gün) çok yeni.
    _, fresh = run_discovery(conn, make_http(), RULES, now=NOW + timedelta(days=60), **fast_limiters())
    assert fresh == []
    _, fresh = run_discovery(conn, make_http(), RULES, now=NOW - timedelta(days=19), **fast_limiters())
    assert fresh == []
    _, fresh = run_discovery(conn, make_http(), RULES, now=NOW, **fast_limiters())
    assert [c.mint for c in fresh] == [DCTY]


def test_gecko_down_dexscreener_continues(capsys):
    conn = connect(":memory:")
    code, fresh = run_discovery(conn, make_http(gecko="down"), RULES, now=NOW, **fast_limiters())
    assert code == 0 and [c.mint for c in fresh] == [DCTY]
    captured = capsys.readouterr()
    assert "geckoterminal" in captured.err and "DÜŞTÜ" in captured.out


def test_dexscreener_down_gecko_continues(capsys):
    conn = connect(":memory:")
    code, fresh = run_discovery(conn, make_http(dex="down"), RULES, now=NOW, **fast_limiters())
    assert code == 0 and [c.mint for c in fresh] == [DCTY]
    assert "dexscreener" in capsys.readouterr().err


def test_both_sources_down_exit_1():
    conn = connect(":memory:")
    code, fresh = run_discovery(
        conn, make_http(gecko="down", dex="down"), RULES, now=NOW, **fast_limiters()
    )
    assert code == 1 and fresh == []


def test_empty_results_are_not_silent_success(capsys):
    conn = connect(":memory:")
    http = make_http(gecko_body={"data": []}, dex_boost=[], dex_tokens=[])
    code, _ = run_discovery(conn, http, RULES, now=NOW, **fast_limiters())
    captured = capsys.readouterr()
    assert code == 1
    assert "0 ham sonuç" in captured.out + captured.err


def test_raw_counts_logged(capsys):
    conn = connect(":memory:")
    run_discovery(conn, make_http(), RULES, now=NOW, **fast_limiters())
    out = capsys.readouterr().out
    assert "kaynak geckoterminal: OK, 12 ham sonuç" in out  # 4 uç x 3 havuz
    assert "kaynak dexscreener: OK, 2 ham sonuç" in out


def test_rate_limiter_blocks_after_limit():
    t = [0.0]
    slept: list[float] = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    lim = RateLimiter(8, clock=lambda: t[0], sleep=sleep)
    for _ in range(8):
        lim.wait()
    assert slept == []
    lim.wait()  # 9. çağrı 60 sn penceresi dolana kadar bekler
    assert slept == [60]
    assert discover.GECKO_CALLS_PER_MIN == 8 and discover.DEXSCREENER_CALLS_PER_MIN == 50
