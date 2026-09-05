"""Token transfer geçmişi: zincirden transfer olaylarını çeker, cüzdan bazlı gruplar."""

from __future__ import annotations

from dataclasses import dataclass

from ..chains.base import TRANSFER_TOPIC, EvmClient

ZERO_ADDRESS = "0x" + "0" * 40


@dataclass(frozen=True)
class TransferEvent:
    """Tek bir ERC-20 Transfer olayı."""

    frm: str
    to: str
    block: int
    tx: str


def token_transfers(client: EvmClient, token: str, from_block: int, to_block: int) -> list[TransferEvent]:
    """Token'ın [from_block, to_block] aralığındaki tüm Transfer olaylarını chronolojik döndürür."""
    token = token.lower()
    events: list[TransferEvent] = []
    for log in client.iter_logs(from_block, to_block, [TRANSFER_TOPIC], address=token):
        topics = log.get("topics", [])
        if len(topics) < 3:
            continue
        events.append(
            TransferEvent(
                frm="0x" + topics[1][-40:],
                to="0x" + topics[2][-40:],
                block=int(log["blockNumber"], 16),
                tx=log["transactionHash"],
            )
        )
    events.sort(key=lambda e: (e.block, e.tx))
    return events
