"""Solana zincir katmanı — JSON-RPC istemcisi + SPL transfer okuma.

SolanaClient ham RPC konuşur; SolanaAdapter onu ChainClient sözleşmesine
sarar. Transfer türetme: mint adresine ``getSignaturesForAddress`` ile
işlem bulunur, her işlem ``getTransaction`` (jsonParsed) ile açılır ve
``preTokenBalances``/``postTokenBalances`` farkından owner bazlı delta'lar
çıkarılır — ATA (associated token account) dolaylılığı otomatik çözülür.

Delta eşleşmesi: tek işlemde gönderen/alıcılar arasında açgözlü eşleştirme
yapılır (büyükten büyüğe, artan parçalar bölünür). Karşılıksız artı delta =
mint (frm=ZERO_ADDRESS), karşılıksız eksi delta = burn (to=ZERO_ADDRESS) —
EVM tarafındaki mint/burn semantiğiyle aynı.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..config import settings
from ..collectors.transfers import ZERO_ADDRESS, TransferEvent
from .protocol import ChainClient  # noqa: F401 — adaptörün doldurduğu sözleşme

# Sistem Programı: normal cüzdan (EOA benzeri) bu owner'a sahiptir; farklı
# owner'lı hesaplar program/SPL hesabıdır (is_contract → True).
SYSTEM_PROGRAM = "11111111111111111111111111111111"

# Public devnet/mainnet uçları periyodik (dakikalık) 429 kovasıyla sınırlar;
# getTransaction gibi ağır çağrı serileri kovayı hızla tüketir. Ardışık
# çağrılar arası asgari bekleme kovayı hiç zorlamaz; 429'da kova soğuyana dek
# bekleyip yeniden denemek geçici tıkanmayı kalıcı hatadan ayırır.
MAX_RETRIES = 3
BASE_WAIT = 1.0
MIN_INTERVAL_SECONDS = 0.9
RATE_LIMIT_RETRIES = 8
RATE_LIMIT_WAIT_SECONDS = 20.0
SIG_PAGE_LIMIT = 1000


class SolanaClient:
    """Tek bir Solana JSON-RPC ucuna konuşan minimal istemci."""

    def __init__(
        self,
        url: str,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        min_interval: float = MIN_INTERVAL_SECONDS,
        rate_limit_retries: int = RATE_LIMIT_RETRIES,
        rate_limit_wait: float = RATE_LIMIT_WAIT_SECONDS,
    ) -> None:
        self.url = url
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self._next_id = 0
        self._last_call = 0.0
        self.min_interval = min_interval
        self.rate_limit_retries = rate_limit_retries
        self.rate_limit_wait = rate_limit_wait

    def call(self, method: str, params: list[Any]) -> Any:
        """Bir JSON-RPC çağrısı yapar; RPC hatasında RuntimeError fırlatır.

        429/5xx ve transport hatalarında yeniden dener: 429, periyodik kova
        tıkanmasıdır — ``rate_limit_wait`` aralıklarla ``rate_limit_retries``
        kez bekleyip tekrar dener (kova dakikalar içinde soğur); 5xx/transport
        hatalarında kısa üstel geri çekilme uygular. Ardışık çağrılara
        ``min_interval`` kadar asgari boşluk koyar ki kova hiç zorlanmasın.
        Kalıcı hatalarda RPC mesajıyla RuntimeError yükseltir.
        """
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        wait = BASE_WAIT
        for attempt in range(MAX_RETRIES):
            if self.min_interval > 0:
                remaining = self.min_interval - (time.monotonic() - self._last_call)
                if remaining > 0:
                    time.sleep(remaining)
            self._last_call = time.monotonic()
            try:
                response = self._http.post(self.url, json=payload)
            except httpx.TransportError:
                if attempt == MAX_RETRIES - 1:
                    raise RuntimeError(f"{method}: RPC'ye ulaşılamadı") from None
                time.sleep(wait)
                wait = min(wait * 2, 60.0)
                continue
            if response.status_code == 429:
                response = self._wait_out_rate_limit(payload)
                if response is None:
                    raise RuntimeError(
                        f"{method}: {self.rate_limit_retries} kez {self.rate_limit_wait}s "
                        "beklemeye rağmen 429 (rate limit)"
                    )
                if response.status_code in {502, 503, 504} and attempt < MAX_RETRIES - 1:
                    time.sleep(wait)
                    wait = min(wait * 2, 60.0)
                    continue
                response.raise_for_status()
                data = response.json()
                if "error" in data:
                    raise RuntimeError(f"{method} hata döndürdü: {data['error']}")
                return data["result"]
            if response.status_code in {502, 503, 504} and attempt < MAX_RETRIES - 1:
                time.sleep(wait)
                wait = min(wait * 2, 60.0)
                continue
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise RuntimeError(f"{method} hata döndürdü: {data['error']}")
            return data["result"]
        raise RuntimeError(f"{method}: {MAX_RETRIES} denemeden sonra RPC yanıt vermedi")

    def _wait_out_rate_limit(self, payload: dict[str, Any]) -> httpx.Response | None:
        """429 kovası soğuyana dek ``rate_limit_retries`` kez bekleyip yeniden dener.

        429 dışı ilk yanıtı döndürür (çağıran normal hata yollarından geçirir);
        kova hiç geçmezse None — çağıran kalıcı 429 RuntimeError'ını yükseltir.
        """
        for _ in range(self.rate_limit_retries):
            time.sleep(self.rate_limit_wait)
            try:
                response = self._http.post(self.url, json=payload)
            except httpx.TransportError:
                continue
            if response.status_code != 429:
                return response
        return None

    def get_slot(self) -> int:
        return int(self.call("getSlot", []))

    def get_signatures_for_address(
        self,
        address: str,
        from_slot: int | None = None,
        until_slot: int | None = None,
        max_signatures: int | None = None,
    ) -> list[dict]:
        """Adrese dokunan işlemleri yeniden eskiye doğru sayfalar.

        ``from_slot`` verilirse daha eski slotlara inmeyi bırakır (sayfalama
        imza bazlıdır; slot filtresi istemci tarafında yapılır). Hatalı
        işlemler de döner — çağıran `err` alanına bakar. ``max_signatures``
        verilirse o kadar imzadan sonra durur: kesilen aralık eksik veri
        demektir, çağıran pencereyi daraltmalıdır.
        """
        out: list[dict] = []
        before: str | None = None
        while True:
            params: dict[str, Any] = {"limit": SIG_PAGE_LIMIT}
            if before is not None:
                params["before"] = before
            batch = self.call("getSignaturesForAddress", [address, params])
            if not batch:
                return out
            for entry in batch:
                slot = int(entry["slot"])
                if until_slot is not None and slot > until_slot:
                    continue
                if from_slot is not None and slot < from_slot:
                    return out  # yeniden eskiye gidiyoruz; aralık bitti
                out.append(entry)
                if max_signatures is not None and len(out) >= max_signatures:
                    return out
            oldest = batch[-1]["slot"]
            if from_slot is not None and int(oldest) < from_slot:
                return out
            before = batch[-1]["signature"]

    def get_transaction(self, signature: str) -> dict:
        """İşlemi jsonParsed kodlamasıyla açar (token bakiyeleri owner'lı gelir)."""
        return self.call(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )

    def get_account_owner(self, address: str) -> str | None:
        """Hesabın owner programını döndürür; hesap yoksa None."""
        info = self.call("getAccountInfo", [address, {"encoding": "jsonParsed"}])
        value = info.get("value")
        if value is None:
            return None
        return value["owner"]

    def get_token_decimals(self, mint: str) -> int:
        """Mint'in ondalık basamağını döndürür (P&L birim çevirisi için)."""
        supply = self.call("getTokenSupply", [mint])
        return int(supply["value"]["decimals"])

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SolanaClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class SolanaAdapter:
    """SolanaClient'ı ChainClient sözleşmesine saran adaptör."""

    def __init__(self, client: SolanaClient) -> None:
        self._client = client

    def block_number(self) -> int:
        return self._client.get_slot()

    def token_transfers(
        self,
        token: str,
        from_slot: int,
        to_slot: int,
        max_signatures: int | None = None,
    ) -> list[TransferEvent]:
        """Mint'in [from_slot, to_slot] aralığındaki SPL transferleri, kronolojik.

        ``max_signatures`` verilirse imza taraması o sayıda durur: kesilen
        aralık eksik veri demektir, çağıran pencereyi daraltmalıdır. Varsayılan
        None = kesme yok (scan/score tam tarama yapar).
        """
        events: list[TransferEvent] = []
        for entry in self._client.get_signatures_for_address(
            token, from_slot=from_slot, until_slot=to_slot, max_signatures=max_signatures
        ):
            if entry.get("err") is not None:
                continue  # başarısız işlem bakiye değiştirmez
            slot = int(entry["slot"])
            tx = self._client.get_transaction(entry["signature"])
            events.extend(self._tx_to_events(tx, token, slot, entry["signature"]))
        events.sort(key=lambda e: (e.block, e.tx))
        return events

    def is_contract(self, wallet: str) -> bool:
        """Hesap program/SPL hesabı mı? (owner = Sistem Programı değilse evet)"""
        owner = self._client.get_account_owner(wallet)
        if owner is None:
            return False  # hesap yok: normal, boş cüzdan
        return owner != SYSTEM_PROGRAM

    def decimals(self, token: str) -> int:
        return self._client.get_token_decimals(token)

    def _tx_to_events(
        self, tx: dict, mint: str, slot: int, signature: str
    ) -> list[TransferEvent]:
        """Tek işlemdeki owner bazlı token delta'larını TransferEvent'lere çevirir."""
        meta = tx.get("meta") or {}
        account_keys = [
            k.get("pubkey", k) if isinstance(k, dict) else k
            for k in (tx.get("transaction", {}).get("message", {}).get("accountKeys") or [])
        ]
        deltas: dict[str, int] = {}
        for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
            for balance in meta.get(side) or []:
                if balance.get("mint") != mint:
                    continue
                owner = balance.get("owner")
                if owner is None:
                    index = balance.get("accountIndex")
                    owner = account_keys[index] if isinstance(index, int) and index < len(account_keys) else None
                if owner is None:
                    continue  # owner'ı çözülemeyen bakiye deltaya katılmaz
                amount = int(balance["uiTokenAmount"]["amount"])
                deltas[owner] = deltas.get(owner, 0) + sign * amount
        deltas = {owner: delta for owner, delta in deltas.items() if delta != 0}

        # Açgözlü eşleştirme: en büyük gönderen en büyük alıcıyla eşleşir,
        # artan parça karşı tarafta bölünür. Cüzdan geçmişinin in/out toplamları
        # her durumda doğru kalır; yalnızca çok-çok işlemlerde karşıt kimliği
        # tahminidir — mevcut filtreler karşıt kimliğine bakmaz.
        sends = sorted(((-d, o) for o, d in deltas.items() if d < 0), reverse=True)
        recvs = sorted(((d, o) for o, d in deltas.items() if d > 0), reverse=True)
        events: list[TransferEvent] = []
        si = ri = 0
        while si < len(sends) and ri < len(recvs):
            amt, sender = sends[si]
            recv, receiver = recvs[ri]
            move = min(amt, recv)
            events.append(
                TransferEvent(frm=sender, to=receiver, block=slot, tx=signature, value=move, token=mint)
            )
            sends[si] = (amt - move, sender)
            recvs[ri] = (recv - move, receiver)
            if sends[si][0] == 0:
                si += 1
            if recvs[ri][0] == 0:
                ri += 1
        for amt, sender in sends[si:]:
            events.append(
                TransferEvent(frm=sender, to=ZERO_ADDRESS, block=slot, tx=signature, value=amt, token=mint)
            )
        for recv, receiver in recvs[ri:]:
            events.append(
                TransferEvent(frm=ZERO_ADDRESS, to=receiver, block=slot, tx=signature, value=recv, token=mint)
            )
        return events

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SolanaAdapter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# Resmî public devnet ucu — duman testi ve geliştirme için; rate limitli.
DEVNET_RPC = "https://api.devnet.solana.com"


def resolve_rpc() -> str:
    """.env'deki RPC_SOLANA_* uçlarından ilk doluyu seçer; yoksa devnet ucu döndürür."""
    for name, url in sorted(settings.rpc_endpoints.items()):
        if name.startswith("solana"):
            return url
    return DEVNET_RPC


def connect() -> SolanaAdapter:
    """Solana istemcisi açar, ucu bir getSlot ile yoklar, adaptör döndürür."""
    adapter = SolanaAdapter(SolanaClient(resolve_rpc()))
    adapter.block_number()  # canary: ölü uçta ilk kullanımda değil burada patla
    return adapter
