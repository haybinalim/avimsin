"""Dağıtım (dev/bundle) ayrımı: aynı işlemde aynı göndericiden ≥3 farklı alıcı erken alıcı değildir."""

from __future__ import annotations

from avimsin.collectors.early_buyers import DistributionSkips, earliest_buyers
from avimsin.collectors.transfers import TransferEvent
from avimsin.config import Settings
from tests.fake_solana import FakeSolanaChain, make_balance_tx
from tests.test_solana_fast import MINT, POOL, addr, make_adapter, sig

DEV = "dev"
TOKEN = "tok"


class EvmStub:
    """Yalnız token_transfers veren (EVM benzeri, iter_tx_transfers yok) istemci."""

    def __init__(self, events):
        self.events = events

    def block_number(self):
        return 100

    def token_transfers(self, token, a, b):
        return self.events


class Stub(EvmStub):
    """İşlem başına gruplu akış veren (Solana benzeri) istemci."""

    def iter_tx_transfers(self, token, a, b):
        group = []
        for e in self.events:
            if group and group[0].tx != e.tx:
                yield group
                group = []
            group.append(e)
        if group:
            yield group


def ev(frm, to, tx, block=1):
    return TransferEvent(frm, to, block, tx, 5, TOKEN)


def wallets(buyers):
    return [b.wallet for b in buyers]


def test_distribution_tx_is_skipped_and_counted() -> None:
    events = [ev(DEV, f"w{i}", "t1") for i in range(4)] + [ev("pool", "real", "t2", 2)]
    skips = DistributionSkips()
    assert wallets(earliest_buyers(Stub(events), TOKEN, 0, 10, skips)) == ["real"]
    assert (skips.receivers, skips.txs) == (4, 1)


def test_evm_path_has_no_distribution_rule() -> None:
    events = [ev(DEV, f"w{i}", "t1") for i in range(4)] + [ev("pool", "real", "t2", 2)]
    skips = DistributionSkips()
    got = wallets(earliest_buyers(EvmStub(events), TOKEN, 0, 10, skips))
    assert got == ["w0", "w1", "w2", "w3", "real"]
    assert (skips.receivers, skips.txs) == (0, 0)


def test_two_receivers_not_distribution() -> None:
    events = [ev("pool", "a", "t1"), ev("pool", "b", "t1")]
    assert wallets(earliest_buyers(Stub(events), TOKEN, 0, 10)) == ["a", "b"]


def test_three_receivers_from_different_senders_not_distribution() -> None:
    events = [ev("p1", "a", "t1"), ev("p2", "b", "t1"), ev("p3", "c", "t1")]
    assert wallets(earliest_buyers(Stub(events), TOKEN, 0, 10)) == ["a", "b", "c"]


def test_same_receivers_split_across_txs_not_distribution() -> None:
    events = [ev(DEV, "a", "t1"), ev(DEV, "b", "t2"), ev(DEV, "c", "t3")]
    assert wallets(earliest_buyers(Stub(events), TOKEN, 0, 10)) == ["a", "b", "c"]


def test_threshold_configurable_and_env(monkeypatch) -> None:
    events = [ev(DEV, "a", "t1"), ev(DEV, "b", "t1")]
    assert earliest_buyers(Stub(events), TOKEN, 0, 10, min_receivers=2) == []
    monkeypatch.setenv("DISTRIBUTION_MIN_RECEIVERS", "5")
    assert Settings.from_env().distribution_min_receivers == 5


def test_solana_stream_early_stop_counts_only_real_buyers() -> None:
    txs, sigs = {}, []
    # slot 100: 4 alıcılı dağıtım; sonra 5 gerçek alım (her biri ayrı işlem).
    txs["dist"] = make_balance_tx(
        MINT, [(POOL, 4000)] + [(addr(10 + i), 0) for i in range(4)],
        [(POOL, 0)] + [(addr(10 + i), 1000) for i in range(4)],
    )
    sigs.append(sig("dist", 100))
    for i in range(5):
        w = addr(50 + i)
        txs[f"buy{i}"] = make_balance_tx(MINT, [(POOL, 10), (w, 0)], [(POOL, 0), (w, 10)])
        sigs.append(sig(f"buy{i}", 200 + i))
    sigs.sort(key=lambda e: -e["slot"])
    chain = FakeSolanaChain(
        signatures=sigs, transactions=txs, latest_slot=1000,
        address_signatures={MINT: sigs}, fill_blocks=True,
    )
    skips = DistributionSkips()
    buyers = earliest_buyers(make_adapter(chain), MINT, 100, 2, skips)
    assert wallets(buyers) == [addr(50), addr(51)]
    assert chain.count("getTransaction") == 3  # dağıtım + 2 gerçek alım; gerisi açılmadı
    assert (skips.receivers, skips.txs) == (4, 1)
