"""Sahte EVM JSON-RPC ağı — testlerde EvmClient'ın konuştuğu uç.

Tek token'ın transfer geçmişini bellekte tutar; eth_getLogs/eth_getCode/
eth_blockNumber/eth_chainId yanıtlarını gerçek biçimde üretir. httpx
MockTransport ile EvmClient'a takılır (network yok).
"""

from __future__ import annotations

import json

import httpx

from avimsin.chains.base import TRANSFER_TOPIC
from avimsin.collectors.transfers import ZERO_ADDRESS

CHAIN_ID = 4663


def pad_topic(address: str) -> str:
    """Adresi 32 byte'lık topic biçimine sola sıfırla."""
    return "0x" + "0" * 24 + address.removeprefix("0x")


class FakeEvmChain:
    """Bellekte token transferleri tutan sahte zincir."""

    def __init__(
        self,
        transfers: list[tuple[str, str, int, int]],  # (frm, to, block, value)
        token: str,
        latest_block: int,
        contracts: set[str] | None = None,
    ) -> None:
        self.transfers = sorted(transfers, key=lambda t: (t[2], t[1]))
        self.token = token.lower()
        self.latest_block = latest_block
        self.contracts = contracts or set()
        self._tx_counter = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        result = self._dispatch(payload["method"], payload["params"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    def _dispatch(self, method: str, params: list) -> object:
        if method == "eth_chainId":
            return hex(CHAIN_ID)
        if method == "eth_blockNumber":
            return hex(self.latest_block)
        if method == "eth_getCode":
            return "0x60806040" if params[0].lower() in self.contracts else "0x"
        if method == "eth_getLogs":
            return self._get_logs(params[0])
        raise RuntimeError(f"sahte zincir bilinmeyen metot: {method}")

    def _get_logs(self, flt: dict) -> list[dict]:
        from_block = int(flt["fromBlock"], 16)
        to_block = int(flt["toBlock"], 16)
        address = (flt.get("address") or "").lower()
        logs = []
        for frm, to, block, value in self.transfers:
            if not (from_block <= block <= to_block):
                continue
            if address and address != self.token:
                continue
            if flt.get("topics") and TRANSFER_TOPIC not in flt["topics"]:
                continue
            self._tx_counter += 1
            logs.append(
                {
                    "address": self.token,
                    "topics": [TRANSFER_TOPIC, pad_topic(frm), pad_topic(to)],
                    "data": hex(value),
                    "blockNumber": hex(block),
                    "transactionHash": "0x" + f"{self._tx_counter:064x}",
                }
            )
        return logs
