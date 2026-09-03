"""RPC uçlarını hız ve doğruluk açısından karşılaştırır.

Her uç için:
- ``eth_chainId``     → hangi zincire bağlı (doğruluk kontrolü)
- ``eth_blockNumber`` → en güncel blok + gecikme (hız kontrolü)

Uçlar `.env` dosyasından okunur; istediğiniz kadar ekleyebilirsiniz
(``RPC_<AD>=<json-rpc-url>``). Public RPC, Alchemy ve QuickNode'u aynı anda
tanımlayıp yarıştırabilirsiniz — bazı saatlerde biri yavaşlarken diğeri hızlı kalır.

Kullanım::

    uv run avimsin-rpc
"""

from __future__ import annotations

import time

import httpx

from .config import settings


def _rpc_call(client: httpx.Client, url: str, method: str) -> dict:
    """Tek bir JSON-RPC çağrısı yapar ve yanıtı döndürür."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": []}
    response = client.post(url, json=payload, timeout=15)
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message", "bilinmeyen hata"))
    return data


def _hex_to_int(value: str) -> int:
    return int(value, 16)


def check_endpoint(client: httpx.Client, name: str, url: str) -> dict:
    """Bir RPC ucunu ölçer: zincir kimliği, blok numarası ve gecikme."""
    start = time.perf_counter()
    chain = _rpc_call(client, url, "eth_chainId")
    block = _rpc_call(client, url, "eth_blockNumber")
    elapsed_ms = (time.perf_counter() - start) * 1000
    return {
        "name": name,
        "ok": True,
        "chain_id": _hex_to_int(chain["result"]),
        "block": _hex_to_int(block["result"]),
        "latency_ms": round(elapsed_ms, 1),
    }


def _print_results(results: list[dict]) -> None:
    ok_results = sorted((r for r in results if r["ok"]), key=lambda r: r["latency_ms"])

    print(f"{'SIRA':<6}{'UÇ':<24}{'ZİNCİR':<9}{'BLOK':<14}{'GECİKME'}")
    print("-" * 64)
    for i, r in enumerate(ok_results, start=1):
        print(f"{i:<6}{r['name']:<24}{r['chain_id']:<9}{r['block']:<14}{r['latency_ms']} ms")
    for r in results:
        if not r["ok"]:
            print(f"{'-':<6}{r['name']:<24}HATA: {r['error']}")

    print()
    if not ok_results:
        print("Hiçbir uç yanıt vermedi.")
        return

    print(f"En hızlı uç: {ok_results[0]['name']} ({ok_results[0]['latency_ms']} ms)")

    # Doğruluk kontrolü: geri kalmış uçları uyar.
    top_block = max(r["block"] for r in ok_results)
    behind = [r["name"] for r in ok_results if top_block - r["block"] > 2]
    if behind:
        print(f"Dikkat: şu uçlar en güncel bloktan geride: {', '.join(behind)}")


def main() -> None:
    """CLI giriş noktası: tüm RPC uçlarını ölçer ve tablo basar."""
    endpoints = settings.rpc_endpoints
    if not endpoints:
        print("`.env` dosyasında `RPC_<AD>=<json-rpc-url>` tanımlı bir uç yok.")
        print("Başlamak için:  cp .env.example .env")
        raise SystemExit(1)

    results: list[dict] = []
    with httpx.Client() as client:
        for name, url in endpoints.items():
            try:
                results.append(check_endpoint(client, name, url))
            except Exception as exc:  # noqa: BLE001
                results.append({"name": name, "ok": False, "error": str(exc)})

    _print_results(results)


if __name__ == "__main__":
    main()
