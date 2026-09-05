"""Ağ-bağımsız EVM JSON-RPC istemcisi.

Tüm zincir adaptörleri bu istemciyi kullanır; yeni ağ eklemek
sadece bir adaptör dosyası gerektirir (bkz. robinhood.py).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

# ERC-20 Transfer(address,address,uint256) olay imzası
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Public RPC'lerin çoğu tek eth_getLogs çağrısında bu kadar bloktan fazlasına izin vermez.
LOG_CHUNK = 1024


class EvmClient:
    """Tek bir RPC ucuna konuşan minimal JSON-RPC istemcisi."""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        self.url = url
        self._http = httpx.Client(timeout=timeout)
        self._next_id = 0

    def call(self, method: str, params: list[Any]) -> Any:
        """Bir JSON-RPC çağrısı yapar; RPC hatasında RuntimeError fırlatır."""
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        response = self._http.post(self.url, json=payload)
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise RuntimeError(f"{method} hata döndürdü: {data['error']}")
        return data["result"]

    def chain_id(self) -> int:
        return int(self.call("eth_chainId", []), 16)

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def get_logs(
        self, from_block: int, to_block: int, topics: list[str], address: str | None = None
    ) -> list[dict]:
        """Verilen aralıktaki olay loglarını çeker (tek çağrı; aralık LOG_CHUNK'tan büyük olmamalı)."""
        params: dict[str, Any] = {
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "topics": topics,
        }
        if address:
            params["address"] = address
        return self.call("eth_getLogs", [params])

    def iter_logs(
        self, from_block: int, to_block: int, topics: list[str], address: str | None = None
    ) -> Iterator[dict]:
        """Büyük aralıkları LOG_CHUNK parçalarına bölerek logları sırayla verir."""
        start = from_block
        while start <= to_block:
            end = min(start + LOG_CHUNK - 1, to_block)
            yield from self.get_logs(start, end, topics, address)
            start = end + 1

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "EvmClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
