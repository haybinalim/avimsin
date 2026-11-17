"""Saf Python Solana adres yardımcıları: base58, PDA ve associated token account (ATA).

Bağımlılık yok (solders gerekmez). ATA = PDA(seeds=[owner, token_program, mint],
program=ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL). PDA, bump 255→0 inerek
sha256(seeds || bump || program_id || "ProgramDerivedAddress") çıktısının ed25519
eğrisi ÜZERİNDE OLMADIĞI ilk değerdir. Doğruluğu tests/test_solana_fast.py içindeki
gerçek zincirden alınmış vektörlerle kanıtlanır.
"""

from __future__ import annotations

import hashlib

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}

ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_PROGRAMS = (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)

_P = 2**255 - 19
_D = (-121665 * pow(121666, -1, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = _ALPHABET[rem] + out
    pad = len(data) - len(data.lstrip(b"\0"))
    return "1" * pad + out


def b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        if ch not in _INDEX:
            raise ValueError(f"geçersiz base58 karakteri: {ch!r}")
        n = n * 58 + _INDEX[ch]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(text) - len(text.lstrip("1"))
    return b"\0" * pad + body


def pubkey_bytes(address: str) -> bytes:
    raw = b58decode(address)
    if len(raw) != 32:
        raise ValueError(f"{address}: Solana adresi 32 bayt olmalı ({len(raw)} bayt)")
    return raw


def is_on_curve(point: bytes) -> bool:
    """32 baytlık sıkıştırılmış noktanın ed25519 eğrisinde olup olmadığı."""
    y = (int.from_bytes(point, "little") & ((1 << 255) - 1)) % _P
    u = (y * y - 1) % _P
    v = (_D * y * y + 1) % _P
    x2 = u * pow(v, -1, _P) % _P
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P == 0:
        return True
    return ((x * _SQRT_M1) ** 2 - x2) % _P == 0


def find_program_address(seeds: list[bytes], program_id: str) -> tuple[str, int]:
    program = pubkey_bytes(program_id)
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(b"".join(seeds) + bytes([bump]) + program + b"ProgramDerivedAddress").digest()
        if not is_on_curve(digest):
            return b58encode(digest), bump
    raise ValueError("geçerli PDA bulunamadı")


def derive_ata(owner: str, mint: str, token_program: str = TOKEN_PROGRAM) -> str:
    """Cüzdanın (owner) mint için associated token account adresi."""
    address, _ = find_program_address(
        [pubkey_bytes(owner), pubkey_bytes(token_program), pubkey_bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM,
    )
    return address
