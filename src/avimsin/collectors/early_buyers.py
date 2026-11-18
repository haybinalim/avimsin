"""Erken alıcı toplama: bir token'ın ilk sahiplerini Transfer olaylarından çıkarır.

Ağ-bağımsız: ChainClient protokolünü dolduran her adaptörle çalışır
(EVM ERC-20 logları, Solana SPL balance değişimleri).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..chains.protocol import ChainClient
from ..config import settings
from .transfers import ZERO_ADDRESS, TransferEvent


@dataclass(frozen=True)
class EarlyBuyer:
    """Bir token'ı alan cüzdanın ilk izi."""

    wallet: str
    block: int
    tx: str


@dataclass
class DistributionSkips:
    """Dağıtım sayılıp atlanan alıcılar (scan çıktısı için)."""

    receivers: int = 0
    txs: int = 0
    wallet_to_wallet: int = 0  # alım sayılmayan cüzdandan cüzdana transferler


def distribution_receivers(group: list[TransferEvent], min_receivers: int) -> set[tuple[str, str]]:
    """Aynı işlemde aynı göndericiden ≥ min_receivers FARKLI alıcıya giden (gönderen, alıcı) çiftleri.

    Bu desen satın alma değil içeriden dağıtımdır (dev/bundle). Mint (gönderen = ZERO)
    ve burn (alıcı = ZERO) sayılmaz.
    """
    by_sender: dict[str, set[str]] = {}
    for e in group:
        if e.frm != ZERO_ADDRESS and e.to != ZERO_ADDRESS:
            by_sender.setdefault(e.frm, set()).add(e.to)
    return {
        (sender, receiver)
        for sender, receivers in by_sender.items()
        if len(receivers) >= min_receivers
        for receiver in receivers
    }


def earliest_buyers(
    client: ChainClient,
    token: str,
    from_block: int,
    count: int,
    skips: DistributionSkips | None = None,
    min_receivers: int | None = None,
) -> list[EarlyBuyer]:
    """Token'ın `from_block` sonrasındaki ilk `count` farklı alıcısını bulur.

    Mint'ler (from = 0x0 / mint authority) erken alıcı sayılmaz: ilk bakiye
    genelde dağıtıcı cüzdana gider, gerçek erken alıcı değildir. Aynı işlemde aynı
    göndericiden ≥ DISTRIBUTION_MIN_RECEIVERS farklı alıcıya giden transferler
    (yalnız Solana; EVM'de kural yok) dağıtım sayılır ve alıcılar atlanır (``skips`` doldurulur). Sınır: dağıtım
    cüzdanları yalnız bu işlem-içi desenle yakalanır; her cüzdana ayrı işlemle
    dağıtım yapan gelişmiş bundle'lar yakalanmaz.
    """
    min_receivers = settings.distribution_min_receivers if min_receivers is None else min_receivers
    latest = client.block_number()
    # Adaptör işlem başına kronolojik tembel akış sunuyorsa (Solana) yeterli alıcı bulununca
    # durulur ve işlem sınırı bilinir; yoksa (EVM) olaylar tek tek işlenir ve dağıtım kuralı uygulanmaz.
    per_tx = getattr(client, "iter_tx_transfers", None)
    # Dağıtım kuralı yalnız işlem akışı olan adaptörde (Solana) uygulanır; EVM davranışı değişmez.
    groups: Iterable[list[TransferEvent]]
    if per_tx:
        groups = per_tx(token, from_block, latest)
    else:
        groups = ([e] for e in client.token_transfers(token, from_block, latest))
    # Adaptör alım kaynağını ayırt edebiliyorsa (Solana: eğri dışı PDA gönderen) cüzdandan
    # cüzdana transferler alım sayılmaz; yoksa (EVM) süzme yok — davranış değişmez.
    is_market = getattr(client, "is_market_sender", None)
    seen: set[str] = set()
    buyers: list[EarlyBuyer] = []
    for group in groups:
        dist = distribution_receivers(group, min_receivers)
        if dist and skips is not None:
            skips.txs += 1
            skips.receivers += len({receiver for _, receiver in dist})
        for event in group:
            sender = event.frm
            if sender == ZERO_ADDRESS:
                continue  # mint: ilk bakiye dağıtıcıya gider, erken alıcı değildir
            receiver = event.to
            if receiver == ZERO_ADDRESS:
                continue  # burn: yakılan bakiye sahipsizdir, alıcı değildir
            if (sender, receiver) in dist:
                continue  # içeriden dağıtım: satın alma değil
            if is_market is not None and not is_market(sender):
                if skips is not None:
                    skips.wallet_to_wallet += 1
                continue  # cüzdandan cüzdana transfer: alım değil
            if receiver in seen:
                continue
            seen.add(receiver)
            buyers.append(EarlyBuyer(wallet=receiver, block=event.block, tx=event.tx))
            if len(buyers) >= count:
                return buyers
    return buyers
