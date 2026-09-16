"""RPC uçlarını hız ve doğruluk açısından karşılaştırır.

Her uç için ağ kimliği ve güncel blok/slot sıralı olarak ölçülür:
- ``solana*`` adları: ``getGenesisHash`` + ``getSlot``
- diğer adlar (eski sağlayıcı adları dahil): ``eth_chainId`` + ``eth_blockNumber``

Uçlar `.env` dosyasındaki ``RPC_<AD>=<json-rpc-url>`` ayarlarından okunur.
Hız ve gerilik yalnız aynı protokol/ağ içinde karşılaştırılır. Bu komut
adaptörlerin uç seçimini değiştirmez; otomatik seçim veya failover yapmaz.

Kullanım::

    uv run avimsin-rpc
"""

from __future__ import annotations

import time

import httpx

from .config import settings


class RpcCheckError(RuntimeError):
    """URL, anahtar veya sunucu mesajı içermeyen kontrol hatası."""


def _rpc_call(client: httpx.Client, url: str, method: str) -> object:
    """Tek çağrı yapar; yalnız güvenli yöntem/durum/kod bilgisini raporlar."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": []}
    try:
        response = client.post(url, json=payload, timeout=15)
    except (httpx.HTTPError, httpx.InvalidURL):
        raise RpcCheckError(f"{method}: RPC'ye ulaşılamadı") from None
    if not response.is_success:
        raise RpcCheckError(f"{method}: HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        raise RpcCheckError(f"{method}: geçersiz JSON yanıtı") from None
    if not isinstance(data, dict):
        raise RpcCheckError(f"{method}: geçersiz RPC yanıtı")
    if "error" in data:
        error = data["error"]
        code = error.get("code") if isinstance(error, dict) else None
        suffix = f" ({code})" if type(code) is int else ""
        raise RpcCheckError(f"{method}: RPC hatası{suffix}")
    if "result" not in data:
        raise RpcCheckError(f"{method}: eksik sonuç")
    return data["result"]


def _hex_to_int(value: object, method: str) -> int:
    if isinstance(value, str) and value.startswith("0x"):
        try:
            number = int(value, 16)
        except ValueError:
            pass
        else:
            if number >= 0:
                return number
    raise RpcCheckError(f"{method}: geçersiz sayısal sonuç")


def check_endpoint(client: httpx.Client, name: str, url: str) -> dict:
    """Ağ kimliği, blok/slot ve iki sıralı çağrının toplam gecikmesini ölçer."""
    protocol = "solana" if name.startswith("solana") else "evm"
    result = {"name": name, "protocol": protocol, "network_id": None, "ok": False}
    start = time.perf_counter()
    try:
        if protocol == "solana":
            network = _rpc_call(client, url, "getGenesisHash")
            if not isinstance(network, str) or not network.strip():
                raise RpcCheckError("getGenesisHash: geçersiz ağ kimliği")
            result["network_id"] = network
            height = _rpc_call(client, url, "getSlot")
            if type(height) is not int or height < 0:
                raise RpcCheckError("getSlot: geçersiz slot")
        else:
            result["network_id"] = _hex_to_int(
                _rpc_call(client, url, "eth_chainId"), "eth_chainId"
            )
            height = _hex_to_int(
                _rpc_call(client, url, "eth_blockNumber"), "eth_blockNumber"
            )
    except RpcCheckError as exc:
        result["error"] = str(exc)
        return result
    elapsed_ms = (time.perf_counter() - start) * 1000
    result.update(ok=True, height=height, latency_ms=round(elapsed_ms, 1))
    return result


def _print_results(results: list[dict]) -> None:
    groups: dict[tuple[str, object], list[dict]] = {}
    for result in results:
        if result["ok"]:
            key = (result["protocol"], result["network_id"])
            groups.setdefault(key, []).append(result)

    for (protocol, network), members in groups.items():
        ok_results = sorted(members, key=lambda r: r["latency_ms"])
        unit = "slot" if protocol == "solana" else "blok"
        identity = "genesis" if protocol == "solana" else "chain ID"
        print(f"{protocol.upper()} — {identity}: {network}")
        print(f"{'SIRA':<6}{'UÇ':<24}{unit.upper():<14}{'GECİKME'}")
        print("-" * 64)
        for i, r in enumerate(ok_results, start=1):
            print(f"{i:<6}{r['name']:<24}{r['height']:<14}{r['latency_ms']} ms")
        print(f"En hızlı uç: {ok_results[0]['name']} ({ok_results[0]['latency_ms']} ms)")

        # Farklı protokollerin veya ağların yükseklikleri karşılaştırılamaz.
        top_height = max(r["height"] for r in ok_results)
        behind = [r["name"] for r in ok_results if top_height - r["height"] > 2]
        if behind:
            print(f"Dikkat: şu uçlar en güncel {unit}tan geride: {', '.join(behind)}")
        print()

    for r in results:
        if not r["ok"]:
            print(f"{r['protocol'].upper()} {r['name']}: HATA: {r['error']}")
    if not groups:
        print("Hiçbir uç yanıt vermedi.")


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
            except Exception:  # noqa: BLE001 — sonraki uç ölçülür; ham hata gizli kalır
                results.append({
                    "name": name,
                    "protocol": "solana" if name.startswith("solana") else "evm",
                    "network_id": None,
                    "ok": False,
                    "error": "Beklenmeyen RPC kontrol hatası",
                })

    _print_results(results)


if __name__ == "__main__":
    main()
