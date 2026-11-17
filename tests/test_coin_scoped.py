"""Coin bazlı karar/skor: izolasyon, yenileme kapsamı, watcher tanımı, görünümler, eski DB."""

from __future__ import annotations

import sqlite3
import sys
from contextlib import nullcontext

import pandas as pd

from avimsin import filter as filter_cli
from avimsin import score as score_cli
from avimsin.alerts.watcher import smart_wallets_for_chain, watch_loop
from avimsin.collectors.transfers import TransferEvent
from avimsin.storage.db import connect, save_coin, save_purchase, save_verdict
from avimsin.storage.views import all_coins_view, coin_options, coin_view, has_coin_results

C1 = "0x" + "a1" * 20
C2 = "0x" + "a2" * 20
SHARED = "0x" + "11" * 20
ONLY2 = "0x" + "22" * 20
POOL = "0x" + "99" * 20
NOTE = "coin bazında yeniden"


class Client:
    def __init__(self, events, head=100_000):
        self.events, self.head = events, head

    def block_number(self):
        return self.head

    def token_transfers(self, token, start, end):
        return [e for e in self.events if e.token == token and start <= e.block <= end]

    def decimals(self, token):
        return 6

    def is_contract(self, wallet):
        return False


def good(coin, wallet):  # girdi + sonra kısmi çıkış: ok, skorlanabilir
    return [
        TransferEvent(POOL, wallet, 1, "i", 1_000_000, coin),
        TransferEvent(wallet, POOL, 60_000, "o", 2_500_000, coin),
    ]


def dumpy(coin, wallet):  # girdinin %90'ı hemen çıkar: dump
    return [
        TransferEvent(POOL, wallet, 1, "i2", 1_000_000, coin),
        TransferEvent(wallet, POOL, 100, "o2", 900_000, coin),
    ]


def run(monkeypatch, module, path, coin, client):
    monkeypatch.setattr(module, "open_chain", lambda chain: nullcontext(client))
    monkeypatch.setattr(sys, "argv", [module.__name__, coin, "--db", str(path), "--chain", "robinhood"])
    module.main()


def win(path, *coins):
    """Score yalnız kayıtlı pencereyi okur; bu testlerde pencere filter'sız elle yazılır."""
    with sqlite3.connect(path) as conn:
        for coin in coins:
            conn.execute(
                "INSERT OR REPLACE INTO analysis_windows VALUES (?, 1, 100000, 1)", (coin,)
            )


def q(path, sql, args=()):
    with sqlite3.connect(path) as conn:
        return conn.execute(sql, args).fetchall()


def seed_two_coins(path):
    conn = connect(path)
    with conn:
        for c in (C1, C2):
            save_coin(conn, c, 1, "robinhood")
            save_purchase(conn, c, SHARED, 1, f"p-{c}")
        save_purchase(conn, C2, ONLY2, 1, "p-only2")
    conn.close()


def test_filter_coin2_does_not_clobber_coin1_verdict_or_score(tmp_path, monkeypatch):
    path = tmp_path / "db.sqlite"
    seed_two_coins(path)
    client = Client(good(C1, SHARED) + dumpy(C2, SHARED) + good(C2, ONLY2))
    run(monkeypatch, filter_cli, path, C1, client)
    run(monkeypatch, score_cli, path, C1, client)
    before = q(path, "SELECT * FROM coin_scores WHERE coin = ?", (C1,))
    assert before and before[0][1] == SHARED

    run(monkeypatch, filter_cli, path, C2, client)

    verdicts = {(c, w): v for c, w, v in q(path, "SELECT coin, wallet, verdict FROM coin_verdicts")}
    assert verdicts[(C1, SHARED)] == "ok"
    assert verdicts[(C2, SHARED)] == "dump"
    assert verdicts[(C2, ONLY2)] == "ok"
    assert q(path, "SELECT * FROM coin_scores WHERE coin = ?", (C1,)) == before


def test_score_coin2_refreshes_only_coin2(tmp_path, monkeypatch):
    path = tmp_path / "db.sqlite"
    seed_two_coins(path)
    client = Client(good(C1, SHARED) + good(C2, SHARED) + good(C2, ONLY2))
    for coin in (C1, C2):
        run(monkeypatch, filter_cli, path, coin, client)
    run(monkeypatch, score_cli, path, C1, client)
    c1_before = q(path, "SELECT * FROM coin_scores WHERE coin = ?", (C1,))
    assert c1_before
    run(monkeypatch, score_cli, path, C2, client)
    assert q(path, "SELECT * FROM coin_scores WHERE coin = ?", (C1,)) == c1_before
    assert len(q(path, "SELECT * FROM coin_scores WHERE coin = ?", (C2,))) == 2

    with sqlite3.connect(path) as conn:  # coin2'de ONLY2 artık uygun değil
        conn.execute("UPDATE coin_verdicts SET verdict='bot' WHERE coin=? AND wallet=?", (C2, ONLY2))
    run(monkeypatch, score_cli, path, C2, client)

    assert [r[1] for r in q(path, "SELECT * FROM coin_scores WHERE coin = ?", (C2,))] == [SHARED]
    assert q(path, "SELECT * FROM coin_scores WHERE coin = ?", (C1,)) == c1_before


def test_score_reads_eligibility_from_coin_verdicts_not_legacy_field(tmp_path, monkeypatch):
    path = tmp_path / "db.sqlite"
    seed_two_coins(path)
    with sqlite3.connect(path) as conn:  # eski alan 'ok' diyor, coin kararı yok
        conn.execute("UPDATE wallets SET verdict = 'ok'")
    win(path, C1)
    run(monkeypatch, score_cli, path, C1, Client(good(C1, SHARED)))
    assert q(path, "SELECT * FROM coin_scores") == []


def test_cli_prints_legacy_note_when_new_tables_empty(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db.sqlite"
    seed_two_coins(path)
    client = Client(good(C1, SHARED))
    win(path, C1)
    run(monkeypatch, score_cli, path, C1, client)
    assert NOTE in capsys.readouterr().out
    run(monkeypatch, filter_cli, path, C1, client)
    assert NOTE in capsys.readouterr().out
    run(monkeypatch, filter_cli, path, C2, client)  # artık dolu: not yok
    assert NOTE not in capsys.readouterr().out


LEGACY_SQL = """
CREATE TABLE coins (address TEXT PRIMARY KEY, first_block INTEGER NOT NULL, chain TEXT);
CREATE TABLE wallets (
    address TEXT PRIMARY KEY, first_seen_block INTEGER NOT NULL,
    is_contract INTEGER, verdict TEXT, verdict_rule TEXT, verdict_reason TEXT
);
CREATE TABLE purchases (
    coin TEXT NOT NULL, wallet TEXT NOT NULL, block INTEGER NOT NULL,
    tx TEXT NOT NULL, PRIMARY KEY (coin, tx, wallet)
);
CREATE TABLE scores (
    wallet TEXT PRIMARY KEY, winrate REAL NOT NULL, net_pnl INTEGER NOT NULL,
    trades INTEGER NOT NULL, frequency REAL NOT NULL, score REAL NOT NULL
);
INSERT INTO coins VALUES ('{c1}', 1, 'robinhood');
INSERT INTO wallets VALUES ('{w}', 1, NULL, 'ok', '-', 'eski');
INSERT INTO purchases VALUES ('{c1}', '{w}', 1, 't');
INSERT INTO scores VALUES ('{w}', 1.0, 5, 2, 0.1, 0.9);
"""


def test_legacy_db_opens_old_data_intact_and_new_code_ignores_it(tmp_path, monkeypatch):
    db = tmp_path / "legacy.sqlite"
    raw = sqlite3.connect(db)
    raw.executescript(LEGACY_SQL.format(c1=C1, w=SHARED))
    raw.commit()
    raw.close()
    connect(db).close()
    tables = {r[0] for r in q(db, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"coin_verdicts", "coin_scores"} <= tables
    assert q(db, "SELECT verdict FROM wallets") == [("ok",)]
    assert q(db, "SELECT wallet, score FROM scores") == [(SHARED, 0.9)]

    win(db, C1)
    run(monkeypatch, score_cli, db, C1, Client(good(C1, SHARED)))  # eski 'ok' okunmaz
    assert q(db, "SELECT * FROM coin_scores") == []
    run(monkeypatch, filter_cli, db, C1, Client(good(C1, SHARED)))
    assert q(db, "SELECT verdict FROM wallets") == [("ok",)]  # eski alanlara yazılmadı
    assert q(db, "SELECT wallet, score FROM scores") == [(SHARED, 0.9)]
    assert q(db, "SELECT coin, wallet, verdict FROM coin_verdicts") == [(C1, SHARED, "ok")]


# --- watcher: smart wallet tanımı ---


def chain_db():
    conn = connect(":memory:")
    save_coin(conn, C1, 1, "robinhood")
    save_coin(conn, C2, 1, "robinhood")
    return conn


def test_smart_wallet_ok_plus_dump_counts():
    conn = chain_db()
    save_verdict(conn, C1, SHARED, "ok", "-", "x")
    save_verdict(conn, C2, SHARED, "dump", "dump", "x")
    assert smart_wallets_for_chain(conn, "robinhood") == {SHARED}


def test_smart_wallet_bot_in_any_coin_excluded():
    conn = chain_db()
    save_verdict(conn, C1, SHARED, "ok", "-", "x")
    save_verdict(conn, C2, SHARED, "bot", "contract", "x")
    save_verdict(conn, C1, ONLY2, "ok", "-", "x")
    assert smart_wallets_for_chain(conn, "robinhood") == {ONLY2}


def test_smart_wallet_other_chain_and_null_chain_do_not_mix():
    conn = chain_db()
    sol = "M" * 44
    save_coin(conn, sol, 1, "solana")
    save_coin(conn, "U" * 44, 1, None)
    save_verdict(conn, C1, SHARED, "ok", "-", "x")
    save_verdict(conn, sol, SHARED, "bot", "contract", "x")  # başka zincirin botu karışmaz
    save_verdict(conn, "U" * 44, ONLY2, "ok", "-", "x")  # NULL zincir dahil değil
    assert smart_wallets_for_chain(conn, "robinhood") == {SHARED}
    assert smart_wallets_for_chain(conn, "solana") == set()


class Heads:
    def __init__(self, *heads):
        self.heads = iter(heads)

    def block_number(self):
        return next(self.heads)

    def token_transfers(self, coin, start, end):
        return []


WARN = "UYARI: robinhood için smart wallet yok;"


def test_empty_smart_set_warns_once_with_hint(capsys):
    conn = chain_db()
    conn.commit()
    watch_loop(Heads(100, 110), conn, once=True, threshold=1, chain="robinhood")
    out = capsys.readouterr().out
    assert out.count(WARN) == 1
    assert "avimsin-filter ve avimsin-score'u coin bazında yeniden çalıştırın." in out


def test_warning_not_repeated_but_repeats_after_nonempty(capsys, monkeypatch):
    conn = chain_db()
    conn.commit()
    state = {"n": 0}

    class Seq(Heads):
        def block_number(self):
            state["n"] += 1
            if state["n"] == 4:  # küme dolsun
                save_verdict(conn, C1, SHARED, "ok", "-", "x")
            if state["n"] == 5:  # yeniden boşalsın
                save_verdict(conn, C1, SHARED, None, "-", "x")
            if state["n"] > 6:
                raise KeyboardInterrupt
            return 100 + 10 * state["n"]

    monkeypatch.setattr("avimsin.alerts.watcher.time.sleep", lambda s: None)
    try:
        watch_loop(Seq(), conn, once=False, threshold=1, chain="robinhood", poll_seconds=0)
    except KeyboardInterrupt:
        pass
    assert capsys.readouterr().out.count(WARN) == 2  # ilk tur + dolu→boş; arada tekrar yok


# --- görünümler ---


def frames():
    purchases = pd.DataFrame(
        {"coin": [C1, C2, C2], "wallet": [SHARED, SHARED, ONLY2], "block": [1, 1, 1]}
    )
    verdicts = pd.DataFrame(
        {
            "coin": [C1, C2, C2],
            "wallet": [SHARED, SHARED, ONLY2],
            "verdict": ["ok", "dump", "bot"],
            "rule": ["-", "dump", "contract"],
            "reason": ["a", "b", "c"],
        }
    )
    scores = pd.DataFrame(
        {
            "coin": [C1, C2],
            "wallet": [SHARED, SHARED],
            "winrate": [1.0, 0.5],
            "net_pnl": [3, 1],
            "trades": [1, 1],
            "frequency": [0.1, 0.1],
            "score": [0.9, 0.4],
        }
    )
    return purchases, verdicts, scores


def test_coin_view_only_that_coins_buyers_and_results():
    p, v, s = frames()
    view = coin_view(C2, p, v, s)
    assert set(view["wallet"]) == {SHARED, ONLY2}
    row = view.set_index("wallet")
    assert row.loc[SHARED, "verdict"] == "dump"
    assert row.loc[SHARED, "score"] == 0.4  # C1'in 0.9'u karışmaz
    assert row.loc[ONLY2, "verdict"] == "bot" and pd.isna(row.loc[ONLY2, "score"])
    assert coin_view(C1, p, v, s)["wallet"].tolist() == [SHARED]


def test_all_coins_view_best_score_ok_coins_and_bot_flag():
    p, v, s = frames()
    result = all_coins_view(p, v, s)
    view = result.set_index("wallet")
    assert view.loc[SHARED, "best_score"] == 0.9 and view.loc[SHARED, "best_coin"] == C1
    assert view.loc[SHARED, "ok_coins"] == 1 and view.loc[SHARED, "dump_coins"] == 1
    assert not view.loc[SHARED, "is_bot"]
    assert view.loc[ONLY2, "bot_coins"] == 1 and view.loc[ONLY2, "is_bot"]
    assert view.loc[ONLY2, "ok_coins"] == 0 and pd.isna(view.loc[ONLY2, "best_score"])
    assert result["wallet"].tolist()[0] == SHARED  # en iyi skor önce


def test_views_empty_new_tables():
    p, _, _ = frames()
    ev = pd.DataFrame(columns=["coin", "wallet", "verdict", "rule", "reason"])
    es = pd.DataFrame(columns=["coin", "wallet", "winrate", "net_pnl", "trades", "frequency", "score"])
    assert not has_coin_results(ev, es)
    assert (~all_coins_view(p, ev, es)["is_bot"]).all()
    assert coin_view(C1, p, ev, es)["verdict"].isna().all()
    assert coin_options(pd.DataFrame({"address": [C2, C1]})) == [C1, C2]
