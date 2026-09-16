"""CLI zincir koruması, uygunluk ve atomik skor yenileme regresyonları."""

from __future__ import annotations

import sqlite3
import sys
from contextlib import nullcontext

import pytest

from avimsin import filter as filter_cli
from avimsin import scan as scan_cli
from avimsin import score as score_cli
from avimsin.collectors.transfers import TransferEvent
from avimsin.storage.db import connect, save_coin, save_purchase, save_score, save_verdict

COIN = "0x" + "ab" * 20
OTHER_COIN = "0x" + "cd" * 20
CLEAN = "0x" + "11" * 20
BOT = "0x" + "22" * 20
DUMP = "0x" + "33" * 20
UNKNOWN = "0x" + "44" * 20
OUTSIDE = "0x" + "55" * 20
POOL = "0x" + "99" * 20


class Client:
    def __init__(self, events):
        self.events = events

    def block_number(self):
        return 1000

    def token_transfers(self, token, start, end):
        return [event for event in self.events if start <= event.block <= end]

    def decimals(self, token):
        return 6

    def is_contract(self, wallet):
        return False


def seed(path, chain="robinhood"):
    conn = connect(path)
    with conn:
        save_coin(conn, COIN, 1, chain)
        for wallet, verdict in [(CLEAN, "ok"), (BOT, "bot"), (DUMP, "dump"), (UNKNOWN, None)]:
            save_purchase(conn, COIN, wallet, 1, wallet)
            save_verdict(conn, wallet, verdict, "-", "seed")
            save_score(conn, wallet, 0.0, -1000000, 1, 0.0, 0.1, 1000000)
        save_coin(conn, OTHER_COIN, 1, "robinhood")
        save_purchase(conn, OTHER_COIN, OUTSIDE, 1, "outside")
        save_verdict(conn, OUTSIDE, "ok", "-", "seed")
        save_score(conn, OUTSIDE, 1.0, 1000000, 2, 0.1, 0.9, 1000000)
    conn.close()


def rows(path, table):
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()


def invoke(monkeypatch, module, path, *args):
    monkeypatch.setattr(sys, "argv", [module.__name__, COIN, "--db", str(path), *args])
    module.main()


def transfers(wallet=CLEAN):
    return [
        TransferEvent(POOL, wallet, 1, "in", 1000000, COIN),
        TransferEvent(wallet, POOL, 500, "out", 2500000, COIN),
    ]


@pytest.mark.parametrize(
    ("module", "explicit_start"),
    [(scan_cli, True), (filter_cli, False), (filter_cli, True), (score_cli, False), (score_cli, True)],
)
def test_chain_mismatch_rejected_before_rpc_or_results(tmp_path, monkeypatch, module, explicit_start):
    path = tmp_path / "analysis.sqlite"
    seed(path, chain="solana")
    before = {table: rows(path, table) for table in ("coins", "wallets", "scores", "purchases")}
    rpc_calls = []
    monkeypatch.setattr(module, "open_chain", lambda chain: rpc_calls.append(chain))
    args = ["--chain", "robinhood"]
    if explicit_start:
        args += ["--from-block", "1"]
    with pytest.raises(SystemExit) as exc:
        invoke(monkeypatch, module, path, *args)
    assert "solana" in str(exc.value) and "robinhood" in str(exc.value)
    assert rpc_calls == []
    assert {table: rows(path, table) for table in before} == before


@pytest.mark.parametrize("module", [scan_cli, filter_cli, score_cli])
def test_legacy_chain_requires_explicit_selection(tmp_path, monkeypatch, module):
    path = tmp_path / "analysis.sqlite"
    seed(path, chain=None)
    client = Client(transfers())
    rpc_calls = []

    def open_client(chain):
        rpc_calls.append(chain)
        return nullcontext(client)

    monkeypatch.setattr(module, "open_chain", open_client)
    with pytest.raises(SystemExit):
        invoke(monkeypatch, module, path, "--from-block", "1")
    assert rpc_calls == []
    assert rows(path, "coins")[0][2] is None
    invoke(monkeypatch, module, path, "--from-block", "1", "--chain", "solana")
    assert rpc_calls == ["solana"]
    assert rows(path, "coins")[0][2] == "solana"


def test_score_lists_only_clean_and_replaces_stale_window(tmp_path, monkeypatch, capsys):
    path = tmp_path / "analysis.sqlite"
    seed(path)
    client = Client([event for wallet in (CLEAN, BOT, DUMP, UNKNOWN) for event in transfers(wallet)])
    monkeypatch.setattr(score_cli, "open_chain", lambda chain: nullcontext(client))
    outside_before = [row for row in rows(path, "scores") if row[0] == OUTSIDE]
    invoke(monkeypatch, score_cli, path)
    output = capsys.readouterr().out
    assert CLEAN in output
    assert all(wallet not in output for wallet in (BOT, DUMP, UNKNOWN))
    assert "+1.50" in output  # Display uses token units, not 1,500,000 raw units.
    assert [row[0] for row in rows(path, "scores")] == [CLEAN, OUTSIDE]
    assert [row for row in rows(path, "scores") if row[0] == OUTSIDE] == outside_before

    invoke(monkeypatch, score_cli, path, "--to-block", "100")
    assert CLEAN not in capsys.readouterr().out
    assert rows(path, "scores") == outside_before


def test_score_invalidates_when_no_wallet_remains_eligible(tmp_path, monkeypatch):
    path = tmp_path / "analysis.sqlite"
    seed(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE wallets SET verdict = NULL WHERE address = ?", (CLEAN,))
    rpc_calls = []
    monkeypatch.setattr(score_cli, "open_chain", lambda chain: rpc_calls.append(chain))
    invoke(monkeypatch, score_cli, path)
    assert rpc_calls == []
    assert [row[0] for row in rows(path, "scores")] == [OUTSIDE]


def test_empty_filter_history_becomes_unknown_and_invalidates_score(tmp_path, monkeypatch, capsys):
    path = tmp_path / "analysis.sqlite"
    seed(path)
    monkeypatch.setattr(filter_cli, "open_chain", lambda chain: nullcontext(Client([])))
    invoke(monkeypatch, filter_cli, path)
    assert all(row[3] is None for row in rows(path, "wallets") if row[0] != OUTSIDE)
    assert [row[0] for row in rows(path, "scores")] == [OUTSIDE]
    capsys.readouterr()
    invoke(monkeypatch, score_cli, path)
    output = capsys.readouterr().out
    assert all(wallet not in output for wallet in (CLEAN, BOT, DUMP, UNKNOWN))


@pytest.mark.parametrize("failure", ["rpc", "write"])
def test_failed_score_refresh_preserves_previous_results(tmp_path, monkeypatch, failure):
    path = tmp_path / "analysis.sqlite"
    seed(path, chain=None)
    before = {table: rows(path, table) for table in ("coins", "scores", "wallets")}
    client = Client(transfers())
    monkeypatch.setattr(score_cli, "open_chain", lambda chain: nullcontext(client))
    if failure == "rpc":
        def fail_transfers(*args):
            raise RuntimeError("RPC failed")
        monkeypatch.setattr(client, "token_transfers", fail_transfers)
    else:
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TRIGGER reject_score BEFORE INSERT ON scores "
                "BEGIN SELECT RAISE(ABORT, 'score write failed'); END"
            )
    with pytest.raises((RuntimeError, sqlite3.IntegrityError)):
        invoke(monkeypatch, score_cli, path, "--chain", "robinhood")
    assert {table: rows(path, table) for table in before} == before


def test_failed_filter_refresh_preserves_verdicts_and_scores(tmp_path, monkeypatch):
    path = tmp_path / "analysis.sqlite"
    seed(path, chain=None)
    before = {table: rows(path, table) for table in ("coins", "scores", "wallets")}
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TRIGGER reject_verdict BEFORE UPDATE ON wallets "
            f"WHEN NEW.address = '{BOT}' BEGIN SELECT RAISE(ABORT, 'verdict write failed'); END"
        )
    monkeypatch.setattr(filter_cli, "open_chain", lambda chain: nullcontext(Client([])))
    with pytest.raises(sqlite3.IntegrityError):
        invoke(monkeypatch, filter_cli, path, "--chain", "robinhood")
    assert {table: rows(path, table) for table in before} == before
