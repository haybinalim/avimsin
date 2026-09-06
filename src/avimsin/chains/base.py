"""Ağ-bağımsız EVM JSON-RPC istemcisi.

Tüm zincir adaptörleri bu istemciyi kullanır; yeni ağ eklemek
sadece bir adaptör dosyası gerektirir (bkz. robinhood.py).
"""

from __future__ import annotations

from collections.abc import Iterator
import time
from collections import deque
from typing import Any

import httpx

# ERC-20 Transfer(address,address,uint256) olay imzası
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Public RPC'ler geniş aralıklarda "log query timed out" döndürüyor: ölçümde
# 1024 blok timeout'a düşerken 256 blok ~0.6 sn'de döndü (ROBINHOOD token,
# Robinhood public RPC). iter_logs yine de timeout'ta yarıya bölerek
# güvenceye alır.
LOG_CHUNK = 256

# Public RPC'ler rate limitliyor (429); ucu boşaltmadan üst üste istek atmamak için
# chunk'lar arasında beklenir.
CHUNK_DELAY = 0.1

# Rate limit / geçici hata yanıtlarında üst üste deneme sayısı; üst sınır 60 sn bekleme.
# Public RPC'nin arka düğümleri dakikalarca düşebiliyor, pencere bunu kapsamalı.
MAX_RETRIES = 10
BASE_WAIT = 0.5
MAX_WAIT = 60.0
RETRYABLE_STATUS = {429, 502, 503, 504}

# İstemci tarafı zaman aşımında (sunucu ağır sorguya 30 sn'de yanıt vermiyor)
# hızlıca vazgeçilir: bekleme değil aralığı küçültmek ilaçtır, iter_logs bölme yapar.
TRANSPORT_RETRIES = 2

# Rate limit tükenmesi (429, Retry-After başlığısız) için chunk düzeyinde yeniden
# deneme: public RPC ~4 hızlı istekten sonra limitliyor ve pencere birkaç saniye
# içinde yenileniyor. Tükenme çağıranın hatası değil paylaşılan kovanın durumudur;
# bekleyip aynı chunk'ı denemek aralığı küçültmekten iyidir.
CHUNK_RETRIES = 5
CHUNK_RETRY_WAIT = 5.0

# call() tükenmeyle bittiğinde mesaja eklenen işaret: iter_logs bunu görüp
# chunk'ı bekleyip yeniden dener, timed out'tan farklı olarak bölme YAPMAZ.
CHUNK_EXHAUSTED = "chunk rate limiti tükenmiş"

# RPC sunucusunun kendi mesajlarıyla döndürdüğü geçici hatalar: HTTP 200 +
# JSON-RPC hatası olarak gelirler ama yeniden deneyince geçer. "query timed
# out" burada değildir: o geçicilik değil sorgunun bu aralıkta çalışamazlığıdır,
# "i/o timeout" ve iç proxy'nin "Post .../rpc: EOF"'u da aynı aile: arka düğüm
# bağlantıyı sessizce kapattı, yük dengeleyici başka düğüme çevirir.
RETRYABLE_MESSAGES = (
    "rate limit",
    "too many requests",
    "connection refused",
    "connection reset",
    "i/o timeout",
    ": eof",
)


class EvmClient:
    """Tek bir RPC ucuna konuşan minimal JSON-RPC istemcisi."""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        self.url = url
        self._http = httpx.Client(timeout=timeout)
        self._next_id = 0

    def call(self, method: str, params: list[Any]) -> Any:
        """Bir JSON-RPC çağrısı yapar; RPC hatasında RuntimeError fırlatır.

        429/502/503/504 yanıtlarında ``Retry-After`` başlığına uyar, yoksa
        geçici JSON-RPC hatalarında da aynı geri çekilmeyi uygular. İstemci
        tarafı zaman aşımı (httpx.TransportError) birkaç denemeden sonra
        "timed out" içeren RuntimeError'a çevrilir: çağıran (iter_logs)
        aralığı küçülterek yeniden dener.
        """
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        wait = BASE_WAIT
        transport_misses = 0
        for _ in range(MAX_RETRIES):
            try:
                response = self._http.post(self.url, json=payload)
            except httpx.TransportError:
                transport_misses += 1
                if transport_misses > TRANSPORT_RETRIES:
                    raise RuntimeError(f"{method}: RPC zaman aşımına uğradı (timed out)") from None
                time.sleep(wait)
                wait = min(wait * 2, MAX_WAIT)
                continue
            if response.status_code in RETRYABLE_STATUS:
                retry_after = response.headers.get("retry-after", "")
                try:
                    time.sleep(float(retry_after))
                except ValueError:
                    time.sleep(wait)
                    wait = min(wait * 2, MAX_WAIT)
                continue
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                message = str(data["error"].get("message", "")).lower()
                if any(text in message for text in RETRYABLE_MESSAGES):
                    time.sleep(wait)
                    wait = min(wait * 2, MAX_WAIT)
                    continue
                raise RuntimeError(f"{method} hata döndürdü: {data['error']}")
            return data["result"]
        raise RuntimeError(f"{method}: {MAX_RETRIES} denemeden sonra RPC yanıt vermedi ({CHUNK_EXHAUSTED})")

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
        """Büyük aralıkları LOG_CHUNK parçalarına bölerek logları sırayla verir.

        "query timed out" dönen parça, aralık yarıya bölerek yeniden denenir:
        yoğun token'larda geniş aralık sunucunun işleme süresini aşıyor.
        Rate limit tükenmesi (Retry-After'sız 429 serisi) chunk düzeyinde
        bekleyip yeniden denenir: kova birkaç saniyede yenileniyor.
        """
        todo: deque[tuple[int, int]] = deque([(from_block, to_block)])
        while todo:
            start, end = todo.popleft()
            if start > end:
                continue
            next_end = min(start + LOG_CHUNK - 1, end)
            for attempt in range(CHUNK_RETRIES):
                try:
                    logs = self.get_logs(start, next_end, topics, address)
                    break
                except RuntimeError as exc:
                    message = str(exc).lower()
                    if "timed out" in message and start < next_end:
                        mid = (start + next_end) // 2
                        todo.append((next_end + 1, end))
                        todo.appendleft((mid + 1, next_end))
                        todo.appendleft((start, mid))
                        break
                    if CHUNK_EXHAUSTED in message and attempt < CHUNK_RETRIES - 1:
                        time.sleep(CHUNK_RETRY_WAIT * (attempt + 1))
                        continue
                    raise
            else:
                continue
            yield from logs
            todo.append((next_end + 1, end))
            if next_end < end:
                time.sleep(CHUNK_DELAY)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "EvmClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
