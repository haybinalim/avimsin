"""Solana cüzdan bazlı izleme (sahte zincir; ağ yok)."""

from __future__ import annotations

from avimsin.alerts.watcher import WatchRpcError, format_signal, watch_loop
from avimsin.chains.solana import SolanaAdapter, SolanaClient
from avimsin.storage.db import connect, save_coin, save_purchase, save_verdict
from tests.fake_solana import FakeSolanaChain, make_balance_tx

import pytest

KNOWN = "K" * 44  # coins tablosundaki (taranmış) coin
NEW = "N" * 44  # coins'te OLMAYAN yeni mint
SRC = "P" * 44
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WALLETS = ["W" * 44, "X" * 44, "Y" * 44]


def make_db():
    conn = connect(":memory:")
    save_coin(conn, KNOWN, 100, chain="solana")
    for w in WALLETS:
        save_purchase(conn, KNOWN, w, 101, f"p{w[0]}")
        save_verdict(conn, KNOWN, w, "ok", "none", "geçti")
    conn.commit()
    return conn


def entry(sig: str, slot: int, err=None) -> dict:
    return {"signature": sig, "slot": slot, "err": err}


def inflow_tx(mint: str, wallet: str) -> dict:
    return make_balance_tx(mint, [(SRC, 10)], [(wallet, 10)])


def adapter_for(chain: FakeSolanaChain) -> SolanaAdapter:
    return SolanaAdapter(SolanaClient("http://x", transport=chain.transport(), min_interval=0))


def run(chain, conn, **kw):
    kw.setdefault("threshold", 3)
    return watch_loop(adapter_for(chain), conn, once=True, telegram=None, chain="solana", **kw)


class Tg:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_message(self, chat_id=None, text="") -> dict:
        self.sent.append(text)
        return {}


def base_chain(extra_sigs=None, extra_txs=None, **kw) -> FakeSolanaChain:
    """Her cüzdanın tek eski imzası var (ilk turda cursor olur)."""
    sigs = {w: [entry(f"old-{w[0]}", 900)] for w in WALLETS}
    txs = {f"old-{w[0]}": inflow_tx(NEW, w) for w in WALLETS}  # geçmiş: sayılmamalı
    for w, lst in (extra_sigs or {}).items():
        sigs[w] = lst + sigs[w]
    txs.update(extra_txs or {})
    return FakeSolanaChain([], txs, address_signatures=sigs, **kw)


def test_first_round_no_backfill_and_new_mint_signals(capsys) -> None:
    conn = make_db()
    chain = base_chain()
    assert run(chain, conn) == 0
    assert chain.count("getTransaction") == 0  # geriye dönük tarama yok
    assert "geriye dönük tarama yok" in capsys.readouterr().out
    # Yeni mint'e (coins'te yok) 3 cüzdan girer -> sinyal.
    sigs = {w: [entry(f"n-{w[0]}", 1000 + i)] for i, w in enumerate(WALLETS)}
    for w in WALLETS:
        chain.transactions[f"n-{w[0]}"] = inflow_tx(NEW, w)
        chain.address_signatures[w] = sigs[w] + chain.address_signatures[w]
    tg = Tg()
    sent = watch_loop(
        adapter_for(chain), conn, once=True, threshold=3, telegram=tg, chain="solana"
    )
    assert sent == 1
    assert NEW in tg.sent[0]
    assert f"https://dexscreener.com/solana/{NEW}" in tg.sent[0]
    out = capsys.readouterr().out
    assert "tur: 3 cüzdan, 3 yeni işlem, 3 giriş, 0 hata" in out


def test_second_round_opens_only_new_signatures() -> None:
    conn = make_db()
    chain = base_chain()
    run(chain, conn)
    w = WALLETS[0]
    chain.address_signatures[w].insert(0, entry("n1", 1000))
    chain.transactions["n1"] = inflow_tx(NEW, w)
    run(chain, conn)
    assert chain.count("getTransaction") == 1
    run(chain, conn)  # yeni imza yok
    assert chain.count("getTransaction") == 1


def test_stable_inflows_not_counted() -> None:
    conn = make_db()
    chain = base_chain()
    run(chain, conn)
    w = WALLETS[0]
    chain.address_signatures[w][:0] = [entry("a", 1001), entry("b", 1002)]
    chain.transactions["a"] = inflow_tx(WSOL, w)
    chain.transactions["b"] = inflow_tx(USDC, w)
    run(chain, conn)
    assert conn.execute("SELECT COUNT(*) FROM watch_inflows").fetchone()[0] == 0


def test_failed_tx_not_counted() -> None:
    conn = make_db()
    chain = base_chain()
    run(chain, conn)
    w = WALLETS[0]
    bad_meta = inflow_tx(NEW, w)
    bad_meta["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    chain.address_signatures[w][:0] = [entry("bad1", 1001), entry("bad2", 1002, err={"x": 1})]
    chain.transactions["bad1"] = bad_meta  # imza listesi err=None, meta.err dolu
    chain.transactions["bad2"] = inflow_tx(NEW, w)  # imza listesinde err dolu
    run(chain, conn)
    assert conn.execute("SELECT COUNT(*) FROM watch_inflows").fetchone()[0] == 0
    assert chain.count("getTransaction") == 1  # err'li imza hiç açılmaz


def test_per_wallet_cap_keeps_newest(capsys) -> None:
    conn = make_db()
    chain = base_chain()
    run(chain, conn)
    w = WALLETS[0]
    # 5 yeni imza, sınır 2: en yeni 2 açılır.
    new = [entry(f"c{i}", 1010 - i) for i in range(5)]
    chain.address_signatures[w][:0] = new
    for e in new:
        chain.transactions[e["signature"]] = inflow_tx(NEW, w)
    run(chain, conn, max_tx_per_wallet=2)
    assert chain.count("getTransaction") == 2
    assert "kalanlar atlandı" in capsys.readouterr().out
    cur = conn.execute(
        "SELECT last_signature FROM watch_wallet_cursor WHERE wallet = ?", (w,)
    ).fetchone()[0]
    assert cur == "c0"  # cursor en yeniye geçti
    run(chain, conn, max_tx_per_wallet=2)
    assert chain.count("getTransaction") == 2  # atlananlar bir daha açılmaz


def test_rpc_error_keeps_cursor(capsys) -> None:
    conn = make_db()
    chain = base_chain()
    run(chain, conn)
    w = WALLETS[0]
    chain.address_signatures[w][:0] = [entry("m1", 1001), entry("m2", 1002)]
    chain.transactions["m2"] = inflow_tx(NEW, w)  # m1 eksik: getTransaction hata verir
    run(chain, conn)
    cur = conn.execute(
        "SELECT last_signature FROM watch_wallet_cursor WHERE wallet = ?", (w,)
    ).fetchone()[0]
    assert cur == f"old-{w[0]}"  # ilerlemedi
    assert conn.execute("SELECT COUNT(*) FROM watch_inflows").fetchone()[0] == 0  # hepsi-ya-hiç
    out = capsys.readouterr().out
    assert "1 hata" in out and "cursor ilerlemedi" in out
    # Düzelince aynı imzalar yeniden denenir.
    chain.transactions["m1"] = inflow_tx(NEW, w)
    run(chain, conn)
    assert conn.execute("SELECT COUNT(*) FROM watch_inflows").fetchone()[0] == 1


def test_all_wallets_failing_is_not_silent_success(capsys) -> None:
    conn = make_db()
    chain = base_chain()
    run(chain, conn)
    for w in WALLETS:
        chain.address_signatures[w].insert(0, entry(f"z-{w[0]}", 1001))  # tx gövdesi yok
    with pytest.raises(WatchRpcError):
        run(chain, conn)
    assert "3 hata" in capsys.readouterr().out


def test_empty_smart_wallet_set_is_visible(capsys) -> None:
    conn = connect(":memory:")
    chain = base_chain()
    run(chain, conn)
    out = capsys.readouterr().out
    assert "smart wallet yok" in out
    assert "tur: 0 cüzdan" in out and "kümesi boş" in out


def test_evm_message_unchanged_and_no_link() -> None:
    from avimsin.alerts.watcher import Signal

    sig = Signal(coin="0x" + "ab" * 20, wallets=["0x1"], blocks=[5])
    assert "dexscreener" not in format_signal(sig, 3)
    assert "dexscreener" not in format_signal(sig, 3, None)
    assert "dexscreener" in format_signal(sig, 3, "solana")
