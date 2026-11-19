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
        address_signatures: dict[str, list[dict]] | None = None,  # adres -> imzalar (yeniden eskiye)
        first_available: int = 0,
        fill_blocks: bool = False,  # True: işlemsiz her slotta ilgisiz bir imza ("fill<slot>") vardır
    ) -> None:
        self.fill_blocks = fill_blocks
        self.null_first: dict[str, int] = {}  # imza -> getTransaction'ın kaç kez null döneceği
        self.calls: list[tuple[str, list]] = []
        self.address_signatures = address_signatures
        self.first_available = first_available
        self.signatures = signatures
        self.transactions = transactions
        self.owners = owners or {}
        self.decimals = decimals
        self.latest_slot = latest_slot

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        try:
            result = self._dispatch(payload["method"], payload["params"])
        except _RpcFail as exc:
            error = {"code": exc.code, "message": exc.message}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "error": error})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    def count(self, method: str) -> int:
        return sum(1 for m, _ in self.calls if m == method)

    def _all_entries(self) -> list[dict]:
        entries = list(self.signatures)
        for lst in (self.address_signatures or {}).values():
            entries.extend(lst)
        return entries

    def _dispatch(self, method: str, params: list) -> object:
        self.calls.append((method, params))
        if method == "getFirstAvailableBlock":
            return self.first_available
        if method == "getBlock":
            slot = params[0]
            sigs = [e["signature"] for e in self._all_entries() if int(e["slot"]) == slot]
            if not sigs and self.fill_blocks and 0 <= slot <= self.latest_slot:
                sigs = [f"fill{slot}"]
            if not sigs:
                raise _RpcFail(-32007, f"Slot {slot} was skipped, or missing due to ledger jump to recent snapshot")
            return {"signatures": sorted(set(sigs))}
        if method == "getSlot":
            return self.latest_slot
        if method == "getSignaturesForAddress":
            return self._signatures(params[0], params[1] if len(params) > 1 else {})
        if method == "getTransaction":
            if self.null_first.get(params[0], 0) > 0:
                self.null_first[params[0]] -= 1
                return None
            return self.transactions[params[0]]
        if method == "getAccountInfo":
            owner = self.owners.get(params[0])
            return {"value": None if owner is None else {"owner": owner}}
        if method == "getTokenSupply":
            return {"value": {"decimals": self.decimals}}
        raise RuntimeError(f"sahte zincir bilinmeyen metot: {method}")

    def _signatures(self, address: str, opts: dict) -> list[dict]:
        """Adrese ait imzalar (address_signatures verilmişse adres bazlı, yoksa tek liste).

        ``before``: imza bu adresin listesindeyse imza-indeksi bazlıdır (eski davranış);
        değilse (ilgisiz imleç imzası) basitçe slot < imleç slotu süzülür — aynı slottaki
        daha eski indeksli imzalar bu durumda dahil EDİLMEZ (gerçek RPC'den daha kaba).
        """
        if self.address_signatures is not None:
            out = list(self.address_signatures.get(address, []))
        else:
            out = list(self.signatures)
        until = opts.get("until")
        if until is not None:
            uidx = next((i for i, e in enumerate(out) if e["signature"] == until), None)
            if uidx is not None:
                out = out[:uidx]
        before = opts.get("before")
        if before is not None:
            idx = next((i for i, e in enumerate(out) if e["signature"] == before), None)
            if idx is not None:
                out = out[idx + 1 :]
            else:
                cursor_slot = next(
                    (int(e["slot"]) for e in self._all_entries() if e["signature"] == before), None
                )
                if cursor_slot is None and before.startswith("fill"):
                    cursor_slot = int(before[4:])
                if cursor_slot is None:
                    raise _RpcFail(-32602, "Invalid param: before signature not found")
                out = [e for e in out if int(e["slot"]) < cursor_slot]
        return out[: opts.get("limit", 1000)]


class _RpcFail(Exception):
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message


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
