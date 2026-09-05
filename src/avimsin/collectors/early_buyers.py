"""Erken alıcı toplama: bir token'ın ilk sahiplerini Transfer olaylarından çıkarır."""

from __future__ import annotations

from dataclasses import dataclass

from ..chains.base import TRANSFER_TOPIC, EvmClient

ZERO_ADDRESS = "0x" + "0" * 40


@dataclass(frozen=True)
class EarlyBuyer:
    """Bir token'ı alan cüzdanın ilk izi."""

    wallet: str
    block: int
    tx: str


def earliest_buyers(client: EvmClient, token: str, from_block: int, count: int) -> list[EarlyBuyer]:
    """Token'ın `from_block` sonrasındaki ilk `count` farklı alıcısını bulur.

    Mint'ler (from = 0x0) erken alıcı sayılmaz: ilk bakiye genelde kontrat
    sahibinin cüzdanına gider, gerçek erken alıcı değildir.
    """
    latest = client.block_number()
    token = token.lower()
    seen: set[str] = set()
    buyers: list[EarlyBuyer] = []
    for log in client.iter_logs(from_block, latest, [TRANSFER_TOPIC], address=token):
        topics = log.get("topics", [])
        if len(topics) < 3:
            continue  # ERC-20 Transfer 3 topic taşır; anormal logu atla
        sender = "0x" + topics[1][-40:]
        if sender == ZERO_ADDRESS:
            continue  # mint: ilk bakiye kontrat sahibine gider, erken alıcı değildir
        receiver = "0x" + topics[2][-40:]
        if receiver == ZERO_ADDRESS:
            continue  # burn: yakılan bakiye sahipsizdir, alıcı değildir
        if receiver in seen:
            continue
        seen.add(receiver)
        buyers.append(
            EarlyBuyer(wallet=receiver, block=int(log["blockNumber"], 16), tx=log["transactionHash"])
        )
        if len(buyers) >= count:
            return buyers
    return buyers
