"""Watch dayanıklılık — Solana pencere kesme ve sinyal akışı sahte zincirde."""

from __future__ import annotations

from avimsin.alerts.watcher import watch_loop
from avimsin.chains.solana import SolanaAdapter, SolanaClient
from avimsin.collectors.transfers import ZERO_ADDRESS, TransferEvent
from avimsin.storage.db import connect, save_coin, save_purchase, save_verdict
from tests.fake_solana import FakeSolanaChain, make_balance_tx

MINT = "M" * 44
A = "A" * 44
B = "B" * 44
SMART1 = "S" * 44
SMART2 = "T" * 44


def sigs(n: int, start_slot: int = 1000) -> list[dict]:
    return [
        {"signature": f"s{i}", "slot": start_slot - i, "err": None} for i in range(n)
    ]


def tx_map(n: int) -> dict[str, dict]:
    return {f"s{i}": make_balance_tx(MINT, [(A, 10)], [(B, 10)]) for i in range(n)}


class StubClient:
    """watch_loop için sahte ChainClient: slot'u kontrol ederiz.

    watch_loop önce `last_block`, sonra tur içinde `latest` okur; `then` ikinci
    okumadan itibaren döner — "zincir ilerledi" senaryosu budur.
    """

    def __init__(self, slot: int, then: int | None = None) -> None:
        self.slot = slot
        self.then = then if then is not None else slot
        self._reads = 0
        self.calls: list[tuple[int, int]] = []

    def block_number(self) -> int:
        self._reads += 1
        return self.slot if self._reads == 1 else self.then

    def token_transfers(self, token: str, a: int, b: int) -> list[TransferEvent]:
        self.calls.append((a, b))
        return []

    def is_contract(self, w: str) -> bool:
        return False

    def decimals(self, t: str) -> int:
        return 9


def test_signature_cap_limits_scan() -> None:
    chain = FakeSolanaChain(signatures=sigs(10), transactions=tx_map(10))
    adapter = SolanaAdapter(SolanaClient("http://x", transport=chain.transport()))
    assert len(adapter.token_transfers(MINT, 1, 2000)) == 10
    assert len(adapter.token_transfers(MINT, 1, 2000, max_signatures=3)) == 3


def test_signature_cap_none_keeps_full_scan() -> None:
    chain = FakeSolanaChain(signatures=sigs(10), transactions=tx_map(10))
    adapter = SolanaAdapter(SolanaClient("http://x", transport=chain.transport()))
    assert len(adapter.token_transfers(MINT, 1, 2000, max_signatures=None)) == 10


def test_watch_skips_oversized_window_and_catches_up() -> None:
    conn = connect(":memory:")
    try:
        save_coin(conn, MINT, 100, chain="solana")
        conn.commit()
        # 1000 → 51_000: 50_000 aralık > 10_000 sınır → tur atlanır.
        client = StubClient(slot=1000, then=51_000)
        sent = watch_loop(
            client, conn, once=True, max_slots_per_poll=10_000, telegram=None, chain="solana"
        )
        assert sent == 0
        assert client.calls == []  # tarama yok, sadece atlama
    finally:
        conn.close()


def test_watch_scans_when_within_window() -> None:
    conn = connect(":memory:")
    try:
        save_coin(conn, MINT, 100, chain="solana")
        save_purchase(conn, MINT, SMART1, 101, "t1")
        save_verdict(conn, SMART1, "ok", "none", "geçti")
        conn.commit()
        client = StubClient(slot=1000, then=1500)  # 500 aralık ≤ 10_000 sınır
        watch_loop(
            client, conn, once=True, max_slots_per_poll=10_000, telegram=None, chain="solana"
        )
        assert client.calls == [(1001, 1500)]
    finally:
        conn.close()


def test_watch_disabled_cap_scans_everything() -> None:
    conn = connect(":memory:")
    try:
        save_coin(conn, MINT, 100, chain="solana")
        conn.commit()
        client = StubClient(slot=1000, then=9_000_000)  # dev pencere; sınır None → taranır
        watch_loop(
            client, conn, once=True, max_slots_per_poll=None, telegram=None, chain="solana"
        )
        assert client.calls == [(1001, 9_000_000)]
    finally:
        conn.close()


def test_watch_signal_emitted_end_to_end_solana() -> None:
    """Solana adaptörü + gerçek sinyal yolu: 2 smart wallet → eşik 2 → 1 sinyal."""
    conn = connect(":memory:")
    try:
        save_coin(conn, MINT, 100, chain="solana")
        for w, tx in ((SMART1, "p1"), (SMART2, "p2")):
            save_purchase(conn, MINT, w, 101, tx)
            save_verdict(conn, w, "ok", "none", "geçti")
        conn.commit()

        txs = {
            "s1": make_balance_tx(MINT, [(A, 10)], [(SMART1, 10)]),
            "s2": make_balance_tx(MINT, [(A, 10)], [(SMART2, 10)]),
        }
        chain = FakeSolanaChain(
            signatures=[
                {"signature": "s1", "slot": 1005, "err": None},
                {"signature": "s2", "slot": 1006, "err": None},
            ],
            transactions=txs,
        )
        adapter = SolanaAdapter(SolanaClient("http://x", transport=chain.transport()))
        client = StubClient(slot=1000, then=1200)
        client.token_transfers = adapter.token_transfers  # type: ignore[method-assign]

        sent = watch_loop(
            client, conn, once=True, threshold=2, telegram=None, chain="solana"
        )
        assert sent == 1
    finally:
        conn.close()


def test_mint_events_do_not_produce_signal() -> None:
    """Mint (frm=ZERO) smart wallet alımı değildir; tek başına sinyal üretmez."""
    conn = connect(":memory:")
    try:
        save_coin(conn, MINT, 100, chain="solana")
        save_purchase(conn, MINT, SMART1, 101, "p1")
        save_verdict(conn, SMART1, "ok", "none", "geçti")
        conn.commit()

        txs = {"s1": make_balance_tx(MINT, [], [(SMART1, 100)])}
        chain = FakeSolanaChain(
            signatures=[{"signature": "s1", "slot": 1005, "err": None}], transactions=txs
        )
        adapter = SolanaAdapter(SolanaClient("http://x", transport=chain.transport()))
        events = adapter.token_transfers(MINT, 1, 2000)
        assert events[0].frm == ZERO_ADDRESS  # mint semantiği korunuyor

        client = StubClient(slot=1000, then=1200)
        client.token_transfers = adapter.token_transfers  # type: ignore[method-assign]
        # Eşiği karşılayan tek smart wallet mint'i de sinyal olmamalı.
        assert watch_loop(
            client, conn, once=True, threshold=1, telegram=None, chain="solana"
        ) == 0
    finally:
        conn.close()