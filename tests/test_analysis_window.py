"""Kalıcı analiz penceresi: hesap, eksik/tam geçişi, kaymama, filter/score paylaşımı."""

from __future__ import annotations

import sqlite3
import sys
from contextlib import nullcontext

import pytest

from avimsin import filter as filter_cli
from avimsin import score as score_cli
from avimsin.storage.db import connect, load_window, save_coin, save_purchase, save_verdict

EVM_COIN = "0x" + "ab" * 20
SOL_COIN = "So11111111111111111111111111111111111111112"
WALLET = {"robinhood": "0x" + "11" * 20, "solana": "Wallet1111111111111111111111111111111111111"}
COINS = {"robinhood": EVM_COIN, "solana": SOL_COIN}
DAY = {"solana": 216_000, "robinhood": 345_600}  # 86400 / 0.4 ve 86400 / 0.25
FIRST = 1_000


class Client:
    def __init__(self, latest):
        self.latest = latest
        self.calls = []

    def block_number(self):
        return self.latest

    def token_transfers(self, token, start, end):
        self.calls.append((start, end))
        return []

    def decimals(self, token):
        return 6

    def is_contract(self, wallet):
        return False


def seed(path, chain, first=FIRST):
    conn = connect(path)
    with conn:
        save_coin(conn, COINS[chain], first, chain)
        save_purchase(conn, COINS[chain], WALLET[chain], first, "tx")
        save_verdict(conn, COINS[chain], WALLET[chain], "ok", "-", "seed")
    conn.close()


def make_eligible(path, chain):
    """Boş sahte geçmiş filter'da 'bilinmiyor' çıkar; score'un çalışması için ok'a çevirir."""
    conn = connect(path)
    with conn:
        save_verdict(conn, COINS[chain], WALLET[chain], "ok", "-", "test")
    conn.close()


def run(monkeypatch, module, path, chain, client, *args):
    monkeypatch.setattr(module, "open_chain", lambda c: nullcontext(client))
    monkeypatch.setattr(
        sys, "argv", [module.__name__, COINS[chain], "--db", str(path), "--chain", chain, *args]
    )
    module.main()


def window(path, chain):
    conn = sqlite3.connect(path)
    try:
        return load_window(conn, COINS[chain])
    finally:
        conn.close()


@pytest.mark.parametrize("chain", ["solana", "robinhood"])
def test_first_run_targets_24h_in_chain_blocks(tmp_path, monkeypatch, capsys, chain):
    path = tmp_path / "a.sqlite"
    seed(path, chain)
    client = Client(FIRST + DAY[chain] + 50_000)
    run(monkeypatch, filter_cli, path, chain, client)
    assert client.calls == [(FIRST, FIRST + DAY[chain])]
    assert window(path, chain) == (FIRST, FIRST + DAY[chain], True)
    assert f"Analiz penceresi: {FIRST}–{FIRST + DAY[chain]} (tam)" in capsys.readouterr().out


def test_incomplete_window_truncates_warns_then_extends(tmp_path, monkeypatch, capsys):
    path = tmp_path / "a.sqlite"
    seed(path, "solana")
    run(monkeypatch, filter_cli, path, "solana", Client(FIRST + 1000))
    out = capsys.readouterr().out
    assert window(path, "solana") == (FIRST, FIRST + 1000, False)
    assert "Analiz penceresi: 1000–2000 (eksik)" in out
    assert f"UYARI: pencere eksik (1000 / {DAY['solana']} blok); sonraki çalıştırmada tamamlanacak" in out

    client = Client(FIRST + 5000)  # hâlâ yetişmedi: uzar ama eksik kalır
    run(monkeypatch, filter_cli, path, "solana", client)
    assert window(path, "solana") == (FIRST, FIRST + 5000, False)
    assert client.calls == [(FIRST, FIRST + 5000)]

    client = Client(FIRST + DAY["solana"] + 10)
    run(monkeypatch, filter_cli, path, "solana", client)
    assert window(path, "solana") == (FIRST, FIRST + DAY["solana"], True)
    assert client.calls == [(FIRST, FIRST + DAY["solana"])]


def test_complete_window_never_drifts(tmp_path, monkeypatch):
    path = tmp_path / "a.sqlite"
    seed(path, "solana")
    run(monkeypatch, filter_cli, path, "solana", Client(FIRST + DAY["solana"] + 1))
    fixed = (FIRST, FIRST + DAY["solana"], True)
    assert window(path, "solana") == fixed
    for latest in (FIRST + DAY["solana"] * 5, FIRST + DAY["solana"] * 40):
        client = Client(latest)
        run(monkeypatch, filter_cli, path, "solana", client)
        assert client.calls == [fixed[:2]]
        assert window(path, "solana") == fixed
        make_eligible(path, "solana")
        client = Client(latest)
        run(monkeypatch, score_cli, path, "solana", client)
        assert client.calls == [fixed[:2]]


def test_score_uses_window_saved_by_filter(tmp_path, monkeypatch):
    path = tmp_path / "a.sqlite"
    seed(path, "solana")
    run(monkeypatch, filter_cli, path, "solana", Client(FIRST + DAY["solana"] + 1))
    make_eligible(path, "solana")
    client = Client(FIRST + DAY["solana"] * 9)
    run(monkeypatch, score_cli, path, "solana", client)
    assert client.calls == [(FIRST, FIRST + DAY["solana"])]


def test_explicit_blocks_update_record_and_report(tmp_path, monkeypatch, capsys):
    path = tmp_path / "a.sqlite"
    seed(path, "solana")
    run(monkeypatch, filter_cli, path, "solana", Client(FIRST + DAY["solana"] + 1))
    capsys.readouterr()
    client = Client(10_000_000)
    run(monkeypatch, filter_cli, path, "solana", client, "--from-block", "2000", "--to-block", "3000")
    out = capsys.readouterr().out
    assert client.calls == [(2000, 3000)]
    assert window(path, "solana") == (2000, 3000, True)
    assert f"Pencere güncellendi: {FIRST}–{FIRST + DAY['solana']} (tam) → 2000–3000 (tam)" in out
    # Sonraki argümansız çalıştırma yeni kaydı kullanır.
    make_eligible(path, "solana")
    client = Client(99_999_999)
    run(monkeypatch, score_cli, path, "solana", client)
    assert client.calls == [(2000, 3000)]


def test_explicit_to_block_beyond_latest_errors(tmp_path, monkeypatch):
    path = tmp_path / "a.sqlite"
    seed(path, "solana")
    client = Client(5000)
    with pytest.raises(SystemExit) as exc:
        run(monkeypatch, filter_cli, path, "solana", client, "--to-block", "5001")
    assert "5001" in str(exc.value)
    assert client.calls == []
    assert window(path, "solana") is None
