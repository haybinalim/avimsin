"""Regresyon: watcher sinyal mantığı token alanıyla eşleşir, base58 korunur."""

from __future__ import annotations

import pytest

from avimsin.alerts.watcher import detect_signals, format_signal, watch_loop
from avimsin.collectors.transfers import ZERO_ADDRESS, TransferEvent
from avimsin.storage.db import connect, save_coin, save_purchase, save_verdict

COIN = "0x" + "aa" * 20
W1 = "0x" + "11" * 20
W2 = "0x" + "22" * 20
W3 = "0x" + "33" * 20


def memdb():
    conn = connect(":memory:")
    save_coin(conn, COIN, 100, chain="robinhood")
    conn.commit()
    return conn


def ev(
    frm: str,
    to: str,
    block: int = 101,
    tx: str = "0xtx",
    token: str = COIN,
    value: int = 10,
) -> TransferEvent:
    return TransferEvent(frm=frm, to=to, block=block, tx=tx, value=value, token=token)


def test_detect_groups_by_token_not_sender() -> None:
    conn = memdb()
    try:
        # Gönderen havuz/DEX (izlenen coin değil), token alanı izlenen coin:
        # sinyal üretmeli — eski kod e.frm'e bakıp bunu kaçırıyordu.
        events = [ev("0x" + "99" * 20, w, tx=f"0xt{i}") for i, w in enumerate([W1, W2, W3])]
        signals = detect_signals(conn, events, threshold=3)
        assert len(signals) == 1
        assert signals[0].coin == COIN
        assert sorted(signals[0].wallets) == sorted([W1, W2, W3])
    finally:
        conn.close()


def test_detect_ignores_unwatched_token_and_below_threshold() -> None:
    conn = memdb()
    try:
        other = ev(W1, W2, token="0x" + "bb" * 20)
        assert detect_signals(conn, [other], threshold=1) == []
        assert detect_signals(conn, [ev(W1, W2)], threshold=3) == []
    finally:
        conn.close()


def test_smart_wallet_filter_applies() -> None:
    conn = memdb()
    try:
        events = [ev(W1, W2), ev(W1, W3)]
        assert len(detect_signals(conn, events, threshold=1)) == 1
        assert (
            len(detect_signals(conn, events, threshold=1, smart_wallets={W2})) == 1
        )
        assert detect_signals(conn, events, threshold=2, smart_wallets={W2}) == []
    finally:
        conn.close()


@pytest.mark.parametrize(
    "frm,to,value",
    [(ZERO_ADDRESS, W2, 10), (W1, ZERO_ADDRESS, 10), (W1, W2, 0), (W1, W2, -1), (W2, W2, 10)],
)
def test_non_transfer_inflows_do_not_meet_threshold(frm, to, value) -> None:
    conn = memdb()
    try:
        events = [ev(frm, to, value=value), ev(W1, W3)]
        assert detect_signals(conn, events, threshold=2) == []
        signals = detect_signals(conn, [ev(W1, W2), ev(W1, W3)], threshold=2)
        assert signals[0].wallets == [W2, W3]
    finally:
        conn.close()


def test_signal_pairs_sorted_wallets_with_their_earliest_blocks() -> None:
    conn = memdb()
    try:
        events = [ev(W1, W3, block=120), ev(W1, W2, block=115), ev(W1, W3, block=105)]
        signal = detect_signals(conn, events, threshold=2)[0]
        assert signal.wallets == [W2, W3]
        assert signal.blocks == [115, 105]
        message = format_signal(signal, threshold=2)
        assert f"<code>{W2}</code> (blok 115)" in message
        assert f"<code>{W3}</code> (blok 105)" in message
        assert detect_signals(conn, events, threshold=3) == []
    finally:
        conn.close()


@pytest.mark.parametrize("chain", ["robinhood", "solana"])
def test_watch_queries_only_selected_chain_and_uses_its_smart_wallets(chain) -> None:
    conn = connect(":memory:")
    coins = {"robinhood": COIN, "solana": "M" * 44, None: "U" * 44}
    wallets = {"robinhood": W2, "solana": "S" * 44, None: "N" * 44}

    class Client:
        def __init__(self):
            self.blocks = iter([100, 110])
            self.queried = []

        def block_number(self):
            return next(self.blocks)

        def token_transfers(self, coin, start, end):
            self.queried.append(coin)
            # Yabancı zincir/NULL geçmişli smart wallet da aynı tokene girebilir.
            return [ev(W1, wallet, token=coin) for wallet in wallets.values()]

    try:
        for coin_chain, coin in coins.items():
            save_coin(conn, coin, 100, chain=coin_chain)
            save_purchase(conn, coin, wallets[coin_chain], 100, f"p-{coin_chain}")
            save_verdict(conn, wallets[coin_chain], "ok", "none", "geçti")
        conn.commit()
        for threshold, expected in [(2, 0), (1, 1)]:
            client = Client()
            assert watch_loop(client, conn, once=True, threshold=threshold, chain=chain) == expected
            assert client.queried == [coins[chain]]
    finally:
        conn.close()


def test_base58_addresses_survive_storage_roundtrip() -> None:
    conn = connect(":memory:")
    try:
        mint = "Mint111111111111111111111111111111111111111"
        mixed = "AbC123xYz987QwErTyUiOpAsDfGhJkLmNoPqRsT1"
        save_coin(conn, mint, 500)
        save_purchase(conn, mint, mixed, 501, "sig1")
        conn.commit()
        coin = conn.execute("SELECT address FROM coins").fetchone()[0]
        wallet = conn.execute("SELECT wallet FROM purchases").fetchone()[0]
        assert coin == mint  # lower() bozulması yok
        assert wallet == mixed
        # EVM hâlâ küçültülür
        save_coin(conn, "0xABcDEF1234", 1)
        conn.commit()
        row = conn.execute(
            "SELECT address FROM coins WHERE address = ?", ("0xabcdef1234",)
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_mint_burn_never_become_early_buyers() -> None:
    from avimsin.collectors.early_buyers import earliest_buyers

    class Stub:
        def block_number(self) -> int:
            return 200

        def token_transfers(self, token: str, a: int, b: int):
            return [
                TransferEvent(frm=ZERO_ADDRESS, to=W1, block=101, tx="t0", token=token),
                TransferEvent(frm=W1, to=ZERO_ADDRESS, block=102, tx="t1", token=token),
                TransferEvent(frm=W1, to=W2, block=103, tx="t2", token=token),
            ]

        def is_contract(self, w: str) -> bool:
            return False

        def decimals(self, t: str) -> int:
            return 18

    buyers = earliest_buyers(Stub(), COIN, 100, 10)  # type: ignore[arg-type]
    assert [b.wallet for b in buyers] == [W2]
