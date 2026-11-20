"""RPC havuzu: %99 sert kota sınırı, önce-ayır sırası, ortak sayaç, gizlilik."""
import json
import sqlite3
import sys
from types import SimpleNamespace

import httpx
import pytest

from avimsin import rpc_check
from avimsin.chains import solana
from avimsin.chains.rpc_pool import (
    RpcPool, RpcPoolExhausted, RpcUsageError, UsageStore, build_providers,
)
from avimsin.config import Settings

SECRET = "https://mainnet.helius-rpc.com/?api-key=TOPSECRET123"
SECRET2 = "https://x.solana-mainnet.quiknode.pro/KEY456SECRET/"
PUBLIC = "https://api.mainnet-beta.solana.com"


class Wall:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds
    monkeypatch.setattr(solana, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))
    return now


def providers(endpoints, options=None):
    return build_providers(endpoints, options or {}, public_interval=0.0)


def make(tmp_path, endpoints, options=None, handler=None, wall=None):
    wall = wall or Wall()
    store = UsageStore(tmp_path / "u.sqlite", wall=wall, sleep=lambda s: None)
    pool = RpcPool(providers(endpoints, options), store, wall=wall)
    seen = []

    def wrapped(request):
        seen.append((request.url.host, json.loads(request.content)["method"]))
        if handler is None:
            return httpx.Response(200, json={"result": 7})
        return handler(request)

    client = solana.SolanaClient("", transport=httpx.MockTransport(wrapped), pool=pool,
                                 rate_limit_wait=0)
    return client, pool, store, seen


def used(pool, name):
    p = next(p for p in pool.providers if p.name == name)
    return pool.store.read(p)[0]


# ---- profil / ayar ----

def test_profile_guess_and_override():
    ps = providers({"solana_a": SECRET, "solana_b": "https://x.chainstack.com/k",
                    "solana_c": "https://lb.drpc.org/k",
                    "solana_d": "https://solana-mainnet.g.alchemy.com/v2/k",
                    "solana_e": PUBLIC, "solana_f": SECRET2},
                   {"solana_f_profile": "drpc", "solana_e_quota": "50"})
    got = {p.name: p.profile for p in ps}
    assert got == {"solana_a": "helius", "solana_b": "chainstack", "solana_c": "drpc",
                   "solana_d": "alchemy", "solana_e": "public", "solana_f": "drpc"}
    assert [p.profile for p in ps] == ["helius", "chainstack", "drpc", "drpc", "alchemy", "public"]
    old = providers({"solana_a": "https://rpc.helius.xyz/?api-key=k",
                     "solana_b": "https://nd-1.p2pify.com/k"})
    assert [p.profile for p in old] == ["helius", "chainstack"]
    assert next(p for p in ps if p.name == "solana_e").quota == 50


def test_order_setting_overrides_profile_order():
    ps = providers({"solana_a": SECRET, "solana_p": PUBLIC}, {"solana_order": "p,solana_a"})
    assert [p.name for p in ps] == ["solana_p", "solana_a"]


def test_invalid_profile_error_has_no_url():
    with pytest.raises(ValueError) as e:
        providers({"solana_a": SECRET}, {"solana_a_profile": "nope"})
    assert "TOPSECRET" not in str(e.value)


def test_settings_option_keys_are_not_endpoints(monkeypatch):
    for k in ("RPC_SOLANA_HELIUS", "RPC_SOLANA_HELIUS_PROFILE", "RPC_SOLANA_HELIUS_QUOTA",
              "RPC_SOLANA_ORDER", "RPC_USAGE_DB"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RPC_SOLANA_HELIUS", SECRET)
    monkeypatch.setenv("RPC_SOLANA_HELIUS_PROFILE", "helius")
    monkeypatch.setenv("RPC_SOLANA_HELIUS_QUOTA", "5")
    monkeypatch.setenv("RPC_SOLANA_ORDER", "helius")
    monkeypatch.setenv("RPC_USAGE_DB", "/tmp/x.sqlite")
    s = Settings.from_env()
    assert s.rpc_endpoints.get("solana_helius") == SECRET
    assert "solana_helius_profile" not in s.rpc_endpoints
    assert "solana_order" not in s.rpc_endpoints and "usage_db" not in s.rpc_endpoints
    assert s.rpc_options["solana_helius_quota"] == "5"
    assert s.rpc_usage_db == "/tmp/x.sqlite"


# ---- %99 sınırı (iki yan) ----

def test_hard_limit_is_99_percent_both_sides(tmp_path):
    p = providers({"solana_a": SECRET}, {"solana_a_quota": "1000"})[0]
    assert p.limit == 990
    store = UsageStore(tmp_path / "u.sqlite", wall=Wall())
    assert store.reserve(p, 989)[0] is True      # sınırın altı
    assert store.reserve(p, 1)[0] is True        # tam sınır (990)
    assert store.read(p)[0] == 990
    assert store.reserve(p, 1)[0] is False       # sınırın üstü: artmaz
    assert store.read(p)[0] == 990


def test_pool_skips_provider_at_limit_and_uses_next(tmp_path, clock):
    client, pool, store, seen = make(
        tmp_path, {"solana_a": SECRET, "solana_p": PUBLIC}, {"solana_a_quota": "100"})
    for _ in range(99):
        clock[0] += 1  # RPS aralığı geçsin
        client.get_slot()
    assert {h for h, _ in seen} == {"mainnet.helius-rpc.com"}
    clock[0] += 1
    client.get_slot()  # 100.: helius %99 dolu -> public
    assert seen[-1][0] == "api.mainnet-beta.solana.com"
    assert used(pool, "solana_a") == 99


# ---- maliyet ----

@pytest.mark.parametrize(("url", "method", "cost"), [
    (SECRET, "getSlot", 1),
    ("https://x.chainstack.com/k", "getTransaction", 2),
    ("https://lb.drpc.org/k", "getSignaturesForAddress", 20),
    ("https://a.g.alchemy.com/v2/k", "getTransaction", 40),
    ("https://a.g.alchemy.com/v2/k", "getBlock", 40),
    ("https://a.g.alchemy.com/v2/k", "getAccountInfo", 10),
    ("https://a.g.alchemy.com/v2/k", "getSlot", 20),
])
def test_cost_profiles(tmp_path, clock, url, method, cost):
    client, pool, _, _ = make(tmp_path, {"solana_a": url})
    client.call(method, [])
    assert used(pool, "solana_a") == cost


# ---- 429 / tükenme ----

def test_max_usage_429_marks_exhausted_and_tries_next(tmp_path, clock):
    def handler(request):
        if request.url.host == "mainnet.helius-rpc.com":
            return httpx.Response(429, text="max usage reached")
        return httpx.Response(200, json={"result": 5})
    client, pool, store, seen = make(
        tmp_path, {"solana_a": SECRET, "solana_p": PUBLIC}, handler=handler)
    assert client.get_slot() == 5
    assert [h for h, _ in seen] == ["mainnet.helius-rpc.com", "api.mainnet-beta.solana.com"]
    pa = pool.providers[0]
    assert store.read(pa)[0] >= pa.quota  # dönem sonuna kadar tükenmiş
    client.get_slot()
    assert [h for h, _ in seen].count("mainnet.helius-rpc.com") == 1  # bir daha denenmedi


def test_plain_429_backs_off_to_next_provider(tmp_path, clock):
    def handler(request):
        if request.url.host == "mainnet.helius-rpc.com":
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json={"result": 5})
    client, pool, store, seen = make(
        tmp_path, {"solana_a": SECRET, "solana_p": PUBLIC}, handler=handler)
    assert client.get_slot() == 5
    assert store.read(pool.providers[0])[0] < pool.providers[0].quota  # tükenmiş SAYILMADI


def test_all_exhausted_falls_back_to_public_with_log(tmp_path, clock, capsys):
    client, pool, store, seen = make(
        tmp_path, {"solana_a": SECRET, "solana_p": PUBLIC}, {"solana_a_quota": "1"})
    client.get_slot()  # limit 0 (1*99//100): helius hiç kullanılamaz
    assert seen == [("api.mainnet-beta.solana.com", "getSlot")]
    err = capsys.readouterr().err
    assert "public" in err and "TOPSECRET" not in err


def test_all_exhausted_without_public_raises_and_sends_nothing(tmp_path, clock):
    client, pool, store, seen = make(tmp_path, {"solana_a": SECRET}, {"solana_a_quota": "1"})
    with pytest.raises(RpcPoolExhausted) as e:
        client.get_slot()
    assert seen == []
    assert "TOPSECRET" not in str(e.value)


# ---- ortak sayaç / dönem ----

def test_two_connections_share_counter(tmp_path):
    wall = Wall()
    p = providers({"solana_a": SECRET}, {"solana_a_quota": "100"})[0]
    s1 = UsageStore(tmp_path / "u.sqlite", wall=wall)
    s2 = UsageStore(tmp_path / "u.sqlite", wall=wall)
    for _ in range(60):
        assert s1.reserve(p, 1)[0]
    for _ in range(39):
        assert s2.reserve(p, 1)[0]
    assert s1.reserve(p, 1)[0] is False and s2.reserve(p, 1)[0] is False
    assert s1.read(p)[0] == s2.read(p)[0] == 99


def test_period_resets(tmp_path):
    wall = Wall()
    p = providers({"solana_a": SECRET}, {"solana_a_quota": "100"})[0]
    store = UsageStore(tmp_path / "u.sqlite", wall=wall)
    store.reserve(p, 99)
    assert store.reserve(p, 1)[0] is False
    wall.t += 30 * 86400 - 1
    assert store.reserve(p, 1)[0] is False   # dönem bitmedi
    wall.t += 2
    assert store.read(p) == (0, None)
    assert store.reserve(p, 1)[0] is True    # yeni dönem
    assert store.read(p)[0] == 1


def test_db_uses_wal_and_hashed_id(tmp_path):
    p = providers({"solana_a": SECRET})[0]
    store = UsageStore(tmp_path / "u.sqlite", wall=Wall())
    store.reserve(p, 1)
    conn = sqlite3.connect(tmp_path / "u.sqlite")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    rows = conn.execute("SELECT provider, period_start, units FROM rpc_usage").fetchall()
    assert len(rows) == 1 and rows[0][0] == p.id and "TOPSECRET" not in str(rows)


# ---- gizlilik ----

def test_no_url_or_key_in_logs_errors_or_db(tmp_path, clock, capsys):
    def handler(request):
        return httpx.Response(404, text=SECRET)
    client, pool, store, _ = make(
        tmp_path, {"solana_a": SECRET, "solana_b": SECRET2},
        {"solana_a_quota": "100", "solana_b_profile": "public"}, handler=handler)
    with pytest.raises(RuntimeError) as e:
        client.get_slot()
    assert "404" in str(e.value)
    for _ in range(200):
        try:
            client.get_slot()
        except RuntimeError as exc:
            assert "TOPSECRET" not in str(exc) and "KEY456" not in str(exc)
    out = capsys.readouterr()
    blob = out.out + out.err + repr(pool.providers)
    for secret in ("TOPSECRET", "KEY456", "helius-rpc.com/?api"):
        assert secret not in blob
    dump = "\n".join(store._connect().iterdump())
    assert "TOPSECRET" not in dump and "KEY456" not in dump


def test_transport_error_message_has_no_url(tmp_path, clock):
    def handler(request):
        raise httpx.ConnectError(SECRET, request=request)
    client, *_ = make(tmp_path, {"solana_a": SECRET}, handler=handler)
    client.max_retries = 2
    with pytest.raises(RuntimeError) as e:
        client.get_slot()
    assert "TOPSECRET" not in str(e.value) and "TOPSECRET" not in repr(e.value.__cause__)


# ---- sayaç hatası: istek gitmez ----

def test_counter_failure_sends_no_request(tmp_path, clock, monkeypatch):
    client, pool, store, seen = make(tmp_path, {"solana_a": SECRET})

    def broken():
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(store, "_connect", broken)
    with pytest.raises(RpcUsageError):
        client.get_slot()
    assert seen == []


def test_reserve_happens_before_send(tmp_path, clock):
    order = []

    def handler(request):
        order.append("send")
        return httpx.Response(200, json={"result": 1})
    client, pool, store, _ = make(tmp_path, {"solana_a": SECRET}, handler=handler)
    real = store.reserve

    def spy(p, cost):
        order.append("reserve")
        return real(p, cost)
    store.reserve = spy
    client.get_slot()
    assert order == ["reserve", "send"]


def test_warn_thresholds_log_once(tmp_path, clock, capsys):
    client, pool, store, _ = make(tmp_path, {"solana_a": SECRET}, {"solana_a_quota": "100"})
    for _ in range(96):
        clock[0] += 1
        client.get_slot()
    err = capsys.readouterr().err
    assert err.count("%80") == 1 and err.count("%95") == 1


# ---- eski davranış ----

def test_single_public_endpoint_keeps_legacy_path(tmp_path, monkeypatch):
    db = tmp_path / "never.sqlite"
    fake = SimpleNamespace(rpc_endpoints={"solana_main": PUBLIC}, rpc_options={},
                           solana_min_interval_seconds=0.9, rpc_usage_db=str(db))
    monkeypatch.setattr(solana, "settings", fake)

    class Stub(solana.SolanaClient):
        def call(self, method, params):
            return 1
    monkeypatch.setattr(solana, "SolanaClient", Stub)
    client = solana.connect()._client
    assert client._pool is None and client.url == PUBLIC
    assert not db.exists()


def test_public_pool_member_is_unmetered_and_writes_nothing(tmp_path, clock):
    client, pool, store, seen = make(tmp_path, {"solana_p": PUBLIC})
    client.get_slot()
    conn = sqlite3.connect(tmp_path / "u.sqlite")
    assert conn.execute("SELECT count(*) FROM rpc_usage").fetchone()[0] == 0


# ---- avimsin-rpc --usage ----

def test_usage_cli_prints_no_url(tmp_path, monkeypatch, capsys):
    db = tmp_path / "u.sqlite"
    s = SimpleNamespace(rpc_endpoints={"solana_a": SECRET, "solana_p": PUBLIC}, rpc_options={},
                        solana_min_interval_seconds=0.9, rpc_usage_db=str(db))
    monkeypatch.setattr(rpc_check, "settings", s)
    p = rpc_check._solana_providers()[0]
    UsageStore(db).reserve(p, 250_000)
    monkeypatch.setattr(sys, "argv", ["avimsin-rpc", "--usage"])
    with pytest.raises(SystemExit) as e:
        rpc_check.main()
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "250000/1000000" in out and "%25.0" in out and "helius" in out and "kotasız" in out
    assert "TOPSECRET" not in out and "helius-rpc.com" not in out


def test_rpc_check_measurement_counts_against_quota(tmp_path, monkeypatch, capsys):
    db = tmp_path / "u.sqlite"
    s = SimpleNamespace(rpc_endpoints={"solana_a": SECRET}, rpc_options={"solana_a_quota": "100"},
                        solana_min_interval_seconds=0.9, rpc_usage_db=str(db))
    monkeypatch.setattr(rpc_check, "settings", s)
    monkeypatch.setattr(sys, "argv", ["avimsin-rpc"])
    replies = {"getGenesisHash": "G", "getSlot": 5}
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"result": replies[json.loads(r.content)["method"]]})))
    monkeypatch.setattr(rpc_check.httpx, "Client", lambda: client)
    rpc_check.main()
    p = rpc_check._solana_providers()[0]
    assert UsageStore(db).read(p)[0] == 2


# ---- denetim düzeltmeleri ----

def test_unknown_host_without_profile_is_error_without_url():
    with pytest.raises(ValueError) as e:
        providers({"solana_q": SECRET2})
    assert "KEY456" not in str(e.value) and "quiknode" not in str(e.value)
    assert "_PROFILE" in str(e.value)
    ok = providers({"solana_q": SECRET2}, {"solana_q_profile": "public"})
    assert ok[0].profile == "public" and ok[0].quota is None


def test_single_unknown_host_connect_raises(monkeypatch):
    fake = SimpleNamespace(rpc_endpoints={"solana_q": SECRET2}, rpc_options={},
                           solana_min_interval_seconds=0.9, rpc_usage_db="x")
    monkeypatch.setattr(solana, "settings", fake)
    with pytest.raises(ValueError):
        solana.connect()


@pytest.mark.parametrize("host", ["api.mainnet-beta.solana.com", "api.devnet.solana.com",
                                  "api.testnet.solana.com"])
def test_known_public_hosts_auto_public(host):
    assert providers({"solana_p": f"https://{host}"})[0].profile == "public"


def test_quota_markers_are_narrow():
    from avimsin.chains.rpc_pool import is_quota_body
    assert is_quota_body("Max usage reached") and is_quota_body("Monthly capacity limit exceeded")
    assert not is_quota_body("per-second quota rate limit, credits per second")


def test_period_start_aligns_period(tmp_path):
    wall = Wall()  # 1_000_000 sn ~ 1970-01-12
    p = providers({"solana_a": SECRET},
                  {"solana_a_quota": "100", "solana_a_period_start": "1970-01-01"})[0]
    store = UsageStore(tmp_path / "u.sqlite", wall=wall)
    store.reserve(p, 50)
    assert store.read(p)[0] == 50
    _, end = store.read(p)
    assert end == 30 * 86400          # çapa 1970-01-01 + 30 gün
    wall.t = end + 1                  # yeni hizalı dönem
    assert store.read(p) == (0, None)
    assert store.reserve(p, 1)[0] and store.read(p)[1] == 60 * 86400


def test_new_counter_db_warns_once(tmp_path, capsys):
    ps = providers({"solana_a": SECRET})
    store = UsageStore(tmp_path / "new.sqlite", wall=Wall())
    store.warn_if_new(ps)
    store.warn_if_new(ps)
    assert capsys.readouterr().err.count("sıfırdan başladı") == 1
    UsageStore(tmp_path / "new.sqlite", wall=Wall()).warn_if_new(ps)  # var olan DB: uyarı yok
    assert "sıfırdan" not in capsys.readouterr().err


def test_default_usage_db_is_absolute():
    from pathlib import Path
    from avimsin.config import default_usage_db
    assert Path(default_usage_db()).is_absolute()
