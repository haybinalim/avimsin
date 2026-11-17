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

import sys
import time
from collections.abc import Iterator
from typing import Any

import httpx

from ..config import settings
from ..collectors.transfers import ZERO_ADDRESS, TransferEvent, WalletTransfers
from ..storage.tx_cache import TxCache, slim_transaction
from .protocol import ChainClient  # noqa: F401 — adaptörün doldurduğu sözleşme
from .solana_addr import TOKEN_PROGRAMS, derive_ata

# Sistem Programı: normal cüzdan (EOA benzeri) bu owner'a sahiptir; farklı
# owner'lı hesaplar program/SPL hesabıdır (is_contract → True).
SYSTEM_PROGRAM = "11111111111111111111111111111111"

# Pacing istemci yükünü sınırlar; paylaşımlı public RPC kovası yine 429 verebilir.
# Rate-limit ve transport/5xx bütçeleri ayrı ve çağrı başına sonludur.
MAX_RETRIES = 3
BASE_WAIT = 1.0
MIN_INTERVAL_SECONDS = 0.9
RATE_LIMIT_RETRIES = 8
RATE_LIMIT_WAIT_SECONDS = 20.0
SIG_PAGE_LIMIT = 1000
# İmleç imzası aranırken bakılacak en çok ardışık slot (atlanmış/boş slotlar için).
CURSOR_MAX_SKIP = 50
# Eskiden yeniye ilerlemede parça uzunluğu (slot, ~10 dk) ve boş parçalarda büyüme sınırı.
CHUNK_SLOTS = 1500
CHUNK_MAX_SLOTS = 200_000
# İkili arama bu genişliğe inince bırakılır (sonra imleçten geriye sayfalanır).
CREATION_SEARCH_SLOTS = 1000
PROGRESS_EVERY = 100
# getBlock: atlanmış slot / blok yok — bir sonraki slot denenir.
_SKIPPED_BLOCK_CODES = {-32007, -32009, -32004}


class RpcError(RuntimeError):
    """JSON-RPC `error` yanıtı (kod ayrıştırılabilsin diye ayrı sınıf)."""

    def __init__(self, method: str, error: dict) -> None:
        super().__init__(f"{method} hata döndürdü: {error}")
        self.code = error.get("code") if isinstance(error, dict) else None


def _progress(message: str) -> None:
    """Seyrek ilerleme satırı (stderr); URL/anahtar içermez."""
    print(message, file=sys.stderr, flush=True)


class SolanaClient:
    """Tek bir Solana JSON-RPC ucuna konuşan minimal istemci."""

    def __init__(
        self,
        url: str,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        min_interval: float | None = None,
        rate_limit_retries: int = RATE_LIMIT_RETRIES,
        rate_limit_wait: float = RATE_LIMIT_WAIT_SECONDS,
    ) -> None:
        self.url = url
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self._next_id = 0
        self._last_call: float | None = None
        self.min_interval = settings.solana_min_interval_seconds if min_interval is None else min_interval
        self.tx_cache: TxCache | None = None
        self.rate_limit_retries = rate_limit_retries
        self.rate_limit_wait = rate_limit_wait

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        """Normal ve retry isteklerinin tamamına aynı pacing sınırını uygular."""
        if self._last_call is not None:
            remaining = self.min_interval - (time.monotonic() - self._last_call)
            if remaining > 0:
                time.sleep(remaining)
        self._last_call = time.monotonic()
        return self._http.post(self.url, json=payload)

    def call(self, method: str, params: list[Any]) -> Any:
        """429 ile transport/5xx hatalarını sınırlı bütçelerle yeniden dener."""
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        rate_retries = failures = 0
        wait = BASE_WAIT
        while True:
            try:
                response = self._post(payload)
            except httpx.TransportError:
                failures += 1
                if failures >= MAX_RETRIES:
                    raise RuntimeError(f"{method}: RPC'ye ulaşılamadı") from None
            else:
                if response.status_code == 429:
                    if rate_retries >= self.rate_limit_retries:
                        raise RuntimeError(f"{method}: 429 yeniden deneme bütçesi tükendi")
                    rate_retries += 1
                    time.sleep(self.rate_limit_wait)
                    continue
                if response.status_code in {502, 503, 504}:
                    failures += 1
                    if failures >= MAX_RETRIES:
                        raise RuntimeError(f"{method}: HTTP {response.status_code} yeniden deneme bütçesi tükendi")
                else:
                    response.raise_for_status()
                    data = response.json()
                    if "error" in data:
                        raise RpcError(method, data["error"])
                    return data["result"]
            time.sleep(wait)
            wait = min(wait * 2, 60.0)

    def get_slot(self) -> int:
        return int(self.call("getSlot", []))

    def get_signatures_for_address(
        self,
        address: str,
        from_slot: int | None = None,
        until_slot: int | None = None,
        max_signatures: int | None = None,
        before: str | None = None,
    ) -> list[dict]:
        """Adrese dokunan işlemleri yeniden eskiye doğru sayfalar.

        ``from_slot`` verilirse daha eski slotlara inmeyi bırakır (sayfalama
        imza bazlıdır; slot filtresi istemci tarafında yapılır). Hatalı
        işlemler de döner — çağıran `err` alanına bakar. ``max_signatures``
        verilirse o kadar imzadan sonra durur: kesilen aralık eksik veri
        demektir, çağıran pencereyi daraltmalıdır. ``before`` ilk sayfanın
        imleci: o imzadan ESKİ imzalar döner; adresle ilgisiz herhangi bir
        işlemin imzası olabilir (bkz. slot_cursor) — böylece ``until_slot``
        üstündeki yeni imzalar hiç indirilmez.
        """
        out: list[dict] = []
        pages = 0
        while True:
            params: dict[str, Any] = {"limit": SIG_PAGE_LIMIT}
            if before is not None:
                params["before"] = before
            batch = self.call("getSignaturesForAddress", [address, params])
            pages += 1
            if pages % 20 == 0:
                _progress(f"{len(out)} imza toplandı ({pages} sayfa)")
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
        """İşlemi jsonParsed kodlamasıyla açar (token bakiyeleri owner'lı gelir).

        Önbellek bağlıysa (attach_tx_cache) sonlanmış işlem bir kez indirilir ve
        yalnız gereken alanlar (slot, meta.err, token bakiyeleri, accountKeys) döner.
        """
        if self.tx_cache is not None:
            cached = self.tx_cache.get(signature)
            if cached is not None:
                return cached
        # Resmî sürüm sözleşmesi: legacy/v0/v1 için en yüksek desteklenen sürüm 1.
        # ALT v0'a özgüdür. JSON-parsed bakiye alanları ortak kullanılır.
        tx = self.call(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}],
        )
        if self.tx_cache is not None and tx is not None:
            tx = slim_transaction(tx)
            self.tx_cache.put(signature, tx)
        return tx

    def first_available_block(self) -> int:
        return int(self.call("getFirstAvailableBlock", []))

    def slot_cursor(self, slot: int) -> str | None:
        """``slot``taki (boşsa/atlanmışsa sonraki ilk dolu bloktaki) bir işlem imzası.

        getSignaturesForAddress'in ``before`` imleci olarak kullanılır: imleç adresle
        ilgisiz olabilir ve o imzadan eski imzaları döndürür. Blokta ilk imza alınır;
        böylece sonuç "slot < blok slotu" demektir. En çok CURSOR_MAX_SKIP slot denenir;
        hiç bulunamazsa None (örn. slot en güncelin ötesinde: imleçsiz başlanır).
        """
        for candidate in range(slot, slot + CURSOR_MAX_SKIP):
            try:
                block = self.call(
                    "getBlock",
                    [candidate, {"transactionDetails": "signatures", "rewards": False,
                                 "maxSupportedTransactionVersion": 0}],
                )
            except RpcError as exc:
                if exc.code in _SKIPPED_BLOCK_CODES:
                    continue
                raise
            signatures = (block or {}).get("signatures") or []
            if signatures:
                return signatures[0]
        return None

    def creation_slot(self, address: str) -> int | None:
        """Adresin en eski işleminin slotu (yoksa None); ikili arama, O(log) istek.

        Yoklama: ``slot_cursor(mid)`` imleciyle ``getSignaturesForAddress(limit=1,
        before=imleç)`` boş değilse adresin mid'den eski işlemi vardır. Aralık
        CREATION_SEARCH_SLOTS'a inince imleçten geriye sayfalanıp en eski imzanın
        slotu alınır. Not: getFirstAvailableBlock'tan eski işlemler aranmaz.
        """
        if not self.call("getSignaturesForAddress", [address, {"limit": 1}]):
            return None
        lo = self.first_available_block()
        hi = self.get_slot() + 1  # invaryant: slot < hi işlemi var; slot < lo işlemi yok
        while hi - lo > CREATION_SEARCH_SLOTS:
            mid = (lo + hi) // 2
            cursor = self.slot_cursor(mid)
            if cursor is None:
                raise RuntimeError(f"slot {mid} için imleç imzası bulunamadı")
            probe = self.call("getSignaturesForAddress", [address, {"limit": 1, "before": cursor}])
            if probe:
                hi = mid
            else:
                lo = mid
        cursor = self.slot_cursor(hi)
        entries = self.get_signatures_for_address(address, from_slot=lo, before=cursor)
        if not entries:
            # Aranan aralıkta imza yok: lo altında olabilir; en yakın bilinen alt sınır
            return lo
        return min(int(e["slot"]) for e in entries)

    def iter_signatures_ascending(
        self,
        address: str,
        from_slot: int,
        to_slot: int,
        chunk: int = CHUNK_SLOTS,
        max_chunk: int = CHUNK_MAX_SLOTS,
    ) -> Iterator[dict]:
        """[from_slot, to_slot] imzalarını ESKİDEN YENİYE tembel verir.

        Aralık ``chunk`` slotluk parçalara bölünür; her parça ``slot_cursor(b+1)``
        imleciyle geriye sayfalanıp çevrilir. Tüketici durunca sonraki parçalar
        istenmez (erken durma). Boş parçada parça uzunluğu ikiye katlanır
        (max_chunk'a kadar), dolu parçadan sonra tabana döner. Aynı slot içi sıra,
        getSignaturesForAddress dönüş sırasının tersidir (eski → yeni).
        """
        a = from_slot
        size = chunk
        while a <= to_slot:
            b = min(a + size - 1, to_slot)
            cursor = self.slot_cursor(b + 1)
            entries = self.get_signatures_for_address(
                address, from_slot=a, until_slot=b, before=cursor
            )
            if entries:
                size = chunk
                yield from reversed(entries)
            else:
                size = min(size * 2, max_chunk)
            a = b + 1

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
        if self.tx_cache is not None:
            self.tx_cache.close()

    def __enter__(self) -> "SolanaClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class SolanaAdapter:
    """SolanaClient'ı ChainClient sözleşmesine saran adaptör."""

    def __init__(self, client: SolanaClient) -> None:
        self._client = client
        self._token_programs: dict[str, str] = {}

    def attach_tx_cache(self, path: str) -> None:
        """getTransaction önbelleğini ``path`` SQLite dosyasına bağlar (bkz. storage/tx_cache.py)."""
        self._client.tx_cache = TxCache(path)

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

    def first_slot(self, token: str) -> int | None:
        """Mint'in en eski işleminin slotu (oluşturma); işlem yoksa None."""
        return self._client.creation_slot(token)

    def iter_token_transfers(
        self, token: str, from_slot: int, to_slot: int
    ) -> Iterator[TransferEvent]:
        """Mint transferlerini KRONOLOJİK ve tembel verir (erken durma için).

        İmzalar eskiden yeniye parça parça getirilir (iter_signatures_ascending),
        her işlem yalnız tüketici istedikçe getTransaction ile açılır; tüketici
        durunca (örn. yeterli alıcı bulununca) gerisi hiç indirilmez. Aynı slot içi
        sıra, getSignaturesForAddress sırasının tersidir (eski → yeni); token_transfers
        ise aynı slot içinde imzaya göre sıralar.
        """
        opened = 0
        for entry in self._client.iter_signatures_ascending(token, from_slot, to_slot):
            if entry.get("err") is not None:
                continue  # başarısız işlem bakiye değiştirmez
            tx = self._client.get_transaction(entry["signature"])
            opened += 1
            if opened % PROGRESS_EVERY == 0:
                _progress(f"{opened} işlem açıldı (slot {entry['slot']})")
            yield from self._tx_to_events(tx, token, int(entry["slot"]), entry["signature"])

    def _token_program(self, mint: str) -> str:
        program = self._token_programs.get(mint)
        if program is None:
            program = self._client.get_account_owner(mint)
            if program not in TOKEN_PROGRAMS:
                raise RuntimeError(f"{mint} SPL mint değil ya da bulunamadı (owner: {program})")
            self._token_programs[mint] = program
        return program

    def wallet_transfers(
        self, token: str, wallets: list[str], from_slot: int, to_slot: int
    ) -> WalletTransfers:
        """Cüzdanların mint'e ait transferleri: yalnız cüzdanın ATA imzaları açılır.

        ATA = PDA(owner, token_program, mint). [from_slot, to_slot] içindeki imzalar,
        ``slot_cursor(to_slot+1)`` imleciyle alınır (pencereden yeni imzalar indirilmez).
        Cüzdan başına pencere içi imza sayısı SOLANA_MAX_TX_PER_WALLET'i aşarsa o
        cüzdanın işlemleri açılmaz (``capped``). Bilinen sınır: ATA dışı token hesabı
        kullanan cüzdanın transferleri görülmez.
        """
        program = self._token_program(token)
        limit = settings.solana_max_tx_per_wallet
        cursor = self._client.slot_cursor(to_slot + 1)
        signatures: dict[str, int] = {}  # imza -> slot (tekilleştirilmiş)
        counts: dict[str, int] = {}
        capped: set[str] = set()
        for wallet in wallets:
            ata = derive_ata(wallet, token, program)
            entries = self._client.get_signatures_for_address(
                ata, from_slot=from_slot, until_slot=to_slot, max_signatures=limit + 1, before=cursor
            )
            counts[wallet] = len(entries)
            if len(entries) > limit:
                capped.add(wallet)
                continue
            for entry in entries:
                if entry.get("err") is None:
                    signatures[entry["signature"]] = int(entry["slot"])
        events: list[TransferEvent] = []
        total = len(signatures)
        for n, (signature, slot) in enumerate(signatures.items(), start=1):
            tx = self._client.get_transaction(signature)
            events.extend(self._tx_to_events(tx, token, slot, signature))
            if n % PROGRESS_EVERY == 0:
                _progress(f"{n}/{total} işlem açıldı")
        events.sort(key=lambda e: (e.block, e.tx))
        return WalletTransfers(events=events, capped=capped, signature_counts=counts, limit=limit)

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
