"""Regresyon: watcher sinyal mantığı token alanıyla eşleşir, base58 korunur."""

from __future__ import annotations

from avimsin.alerts.watcher import detect_signals
from avimsin.collectors.transfers import ZERO_ADDRESS, TransferEvent
from avimsin.storage.db import connect, save_coin, save_purchase

COIN = "0x" + "aa" * 20
W1 = "0x" + "11" * 20
W2 = "0x" + "22" * 20
W3 = "0x" + "33" * 20


def memdb():
    conn = connect(":memory:")
    save_coin(conn, COIN, 100)
    conn.commit()
    return conn


def ev(frm: str, to: str, block: int = 101, tx: str = "0xtx", token: str = COIN) -> TransferEvent:
    return TransferEvent(frm=frm, to=to, block=block, tx=tx, value=10, token=token)


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
