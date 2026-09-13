"""Erken alıcı toplama: bir token'ın ilk sahiplerini Transfer olaylarından çıkarır.

Ağ-bağımsız: ChainClient protokolünü dolduran her adaptörle çalışır
(EVM ERC-20 logları, Solana SPL balance değişimleri).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..chains.protocol import ChainClient
from .transfers import ZERO_ADDRESS


@dataclass(frozen=True)
class EarlyBuyer:
    """Bir token'ı alan cüzdanın ilk izi."""

    wallet: str
    block: int
    tx: str


def earliest_buyers(client: ChainClient, token: str, from_block: int, count: int) -> list[EarlyBuyer]:
    """Token'ın `from_block` sonrasındaki ilk `count` farklı alıcısını bulur.

    Mint'ler (from = 0x0 / mint authority) erken alıcı sayılmaz: ilk bakiye
    genelde dağıtıcı cüzdana gider, gerçek erken alıcı değildir.
    """
    latest = client.block_number()
    seen: set[str] = set()
    buyers: list[EarlyBuyer] = []
    for event in client.token_transfers(token, from_block, latest):
        sender = event.frm
        if sender == ZERO_ADDRESS:
            continue  # mint: ilk bakiye dağıtıcıya gider, erken alıcı değildir
        receiver = event.to
        if receiver == ZERO_ADDRESS:
            continue  # burn: yakılan bakiye sahipsizdir, alıcı değildir
        if receiver in seen:
            continue
        seen.add(receiver)
        buyers.append(EarlyBuyer(wallet=receiver, block=event.block, tx=event.tx))
        if len(buyers) >= count:
            return buyers
    return buyers
