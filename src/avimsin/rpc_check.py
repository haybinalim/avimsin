"""RPC uçlarını hız ve doğruluk açısından karşılaştırır.

Her uç için ağ kimliği ve güncel blok/slot sıralı olarak ölçülür:
- ``solana*`` adları: ``getGenesisHash`` + ``getSlot``
- diğer adlar (eski sağlayıcı adları dahil): ``eth_chainId`` + ``eth_blockNumber``

Uçlar `.env` dosyasındaki ``RPC_<AD>=<json-rpc-url>`` ayarlarından okunur.
Hız ve gerilik yalnız aynı protokol/ağ içinde karşılaştırılır. Bu komut
adaptörlerin uç seçimini değiştirmez; otomatik seçim veya failover yapmaz.

Kullanım::

    uv run avimsin-rpc            # uçları ölçer
    uv run avimsin-rpc --usage    # Solana sağlayıcı kota kullanımını gösterir (ağ yok)
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

import httpx

from .chains import rpc_pool
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


def check_endpoint(client: httpx.Client, name: str, url: str, gate=None) -> dict:
    """Ağ kimliği, blok/slot ve iki sıralı çağrının toplam gecikmesini ölçer.

    ``gate(method)`` verilirse her çağrıdan ÖNCE kota ayırır; ayıramazsa
    RpcCheckError verir ve istek gitmez.
    """
    protocol = "solana" if name.startswith("solana") else "evm"
    result = {"name": name, "protocol": protocol, "network_id": None, "ok": False}
    start = time.perf_counter()
    try:
        if protocol == "solana":
            if gate:
                gate("getGenesisHash")
            network = _rpc_call(client, url, "getGenesisHash")
            if not isinstance(network, str) or not network.strip():
                raise RpcCheckError("getGenesisHash: geçersiz ağ kimliği")
            result["network_id"] = network
            if gate:
                gate("getSlot")
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


def _solana_providers() -> list:
    return rpc_pool.build_providers(
        settings.rpc_endpoints,
        getattr(settings, "rpc_options", {}),
        getattr(settings, "solana_min_interval_seconds", 0.9),
    )


def _gate_for(provider, store):
    """Kotalı sağlayıcıda ölçüm çağrıları da sayaca yazılır (aksi halde kota sızar)."""
    if provider.quota is None:
        return None

    def gate(method: str) -> None:
        try:
            ok = store.reserve(provider, provider.cost(method))[0]
        except rpc_pool.RpcUsageError:
            raise RpcCheckError(f"{method}: kota sayacı yazılamadı; istek gönderilmedi") from None
        if not ok:
            raise RpcCheckError(f"{method}: kota sınırı (%{rpc_pool.HARD_LIMIT_PERCENT}) doldu; istek gönderilmedi")

    return gate


def print_usage() -> int:
    """Her Solana sağlayıcısı için kullanım/kota/dönem özeti (URL basılmaz)."""
    try:
        providers = _solana_providers()
    except ValueError as exc:
        print(f"HATA: {exc}")
        return 1
    if not providers:
        print("Tanımlı Solana RPC sağlayıcısı yok.")
        return 1
    store = rpc_pool.UsageStore(getattr(settings, "rpc_usage_db", rpc_pool.DEFAULT_USAGE_DB))
    print(f"{'AD':<24}{'PROFİL':<12}{'KULLANIM/KOTA':<26}{'YÜZDE':<8}{'DÖNEM BİTİŞİ':<18}TÜKENMİŞ")
    print("-" * 96)
    for p in providers:
        if p.quota is None:
            print(f"{p.name:<24}{p.profile:<12}{'kotasız':<26}{'-':<8}{'-':<18}hayır")
            continue
        try:
            used, end = store.read(p)
        except rpc_pool.RpcUsageError:
            print(f"{p.name:<24}{p.profile:<12}sayaç okunamadı")
            continue
        percent = f"%{used * 100 / p.quota:.1f}"
        end_text = datetime.fromtimestamp(end, timezone.utc).strftime("%Y-%m-%d %H:%M") if end else "-"
        exhausted = "evet" if used >= p.limit else "hayır"
        print(f"{p.name:<24}{p.profile:<12}{f'{used}/{p.quota}':<26}{percent:<8}{end_text:<18}{exhausted}")
    return 0


def main() -> None:
    """CLI giriş noktası: tüm RPC uçlarını ölçer ve tablo basar (`--usage`: kota özeti)."""
    if "--usage" in sys.argv[1:]:
        raise SystemExit(print_usage())
    endpoints = settings.rpc_endpoints
    if not endpoints:
        print("`.env` dosyasında `RPC_<AD>=<json-rpc-url>` tanımlı bir uç yok.")
        print("Başlamak için:  cp .env.example .env")
        raise SystemExit(1)

    results: list[dict] = []
    gates: dict = {}
    if any(n.startswith("solana") for n in endpoints):
        try:
            providers = []
            for n, u in endpoints.items():
                if not n.startswith("solana"):
                    continue
                try:
                    providers += rpc_pool.build_providers(
                        {n: u}, getattr(settings, "rpc_options", {}),
                        getattr(settings, "solana_min_interval_seconds", 0.9),
                    )
                except ValueError as exc:
                    # Tanınmayan sağlayıcı: ölçüm sayaçsız yapılır, havuz bunu REDDEDER.
                    print(f"UYARI: {exc}")
            if any(p.quota is not None for p in providers):
                store = rpc_pool.UsageStore(getattr(settings, "rpc_usage_db", rpc_pool.DEFAULT_USAGE_DB))
                store.warn_if_new(providers)
                gates = {p.name: _gate_for(p, store) for p in providers}
        except (ValueError, rpc_pool.RpcUsageError):
            print("HATA: RPC havuz ayarı/kota sayacı kurulamadı; Solana uçları ölçülmedi")
            endpoints = {n: u for n, u in endpoints.items() if not n.startswith("solana")}
    with httpx.Client() as client:
        for name, url in endpoints.items():
            try:
                results.append(check_endpoint(client, name, url, gates.get(name)))
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
