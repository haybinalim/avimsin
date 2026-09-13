"""Sahte Solana JSON-RPC ağı — testlerde SolanaClient'ın konuştuğu uç.

Bellekte imza listesi + işlem gövdeleri tutar; getSlot/
getSignaturesForAddress/getTransaction/getAccountInfo/getTokenSupply
yanıtlarını gerçek biçimde üretir. httpx MockTransport ile SolanaClient'a
takılır (network yok).
"""

from __future__ import annotations

import json

import httpx


class FakeSolanaChain:
    """Bellekte SPL aktivitesi tutan sahte zincir."""

    def __init__(
        self,
        signatures: list[dict],  # {"signature", "slot", "err"} — yeniden eskiye
        transactions: dict[str, dict],  # imza -> getTransaction gövdesi
        owners: dict[str, str | None] | None = None,  # hesap -> owner programı
        decimals: int = 9,
        latest_slot: int = 1_000,
    ) -> None:
        self.signatures = signatures
        self.transactions = transactions
        self.owners = owners or {}
        self.decimals = decimals
        self.latest_slot = latest_slot

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        result = self._dispatch(payload["method"], payload["params"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    def _dispatch(self, method: str, params: list) -> object:
        if method == "getSlot":
            return self.latest_slot
        if method == "getSignaturesForAddress":
            return self._signatures(params[1] if len(params) > 1 else {})
        if method == "getTransaction":
            return self.transactions[params[0]]
        if method == "getAccountInfo":
            owner = self.owners.get(params[0])
            return {"value": None if owner is None else {"owner": owner}}
        if method == "getTokenSupply":
            return {"value": {"decimals": self.decimals}}
        raise RuntimeError(f"sahte zincir bilinmeyen metot: {method}")

    def _signatures(self, opts: dict) -> list[dict]:
        out = list(self.signatures)
        if opts.get("before") is not None:
            idx = next(i for i, e in enumerate(out) if e["signature"] == opts["before"])
            out = out[idx + 1 :]
        return out[: opts.get("limit", 1000)]


def make_balance_tx(
    mint: str,
    pre: list[tuple[str, int]],
    post: list[tuple[str, int]],
    with_owner: bool = True,
) -> dict:
    """pre/post token bakiyelerinden işlem gövdesi kurar.

    pre/post: (owner, ham miktar) listesi. with_owner=False ise owner alanı
    konmaz, adaptör accountKeys indeksinden çözer.
    """
    owners = sorted({o for o, _ in pre} | {o for o, _ in post})
    account_keys = owners  # indeks == owner sırası

    def side(entries: list[tuple[str, int]]) -> list[dict]:
        out = []
        for i, (owner, amount) in enumerate(entries):
            entry: dict = {
                "accountIndex": owners.index(owner),
                "mint": mint,
                "uiTokenAmount": {"amount": str(amount), "decimals": 9},
            }
            if with_owner:
                entry["owner"] = owner
            out.append(entry)
        return out

    return {
        "transaction": {"message": {"accountKeys": account_keys}},
        "meta": {"preTokenBalances": side(pre), "postTokenBalances": side(post)},
    }
