"""Solana hızlı tarama: ATA türetimi, slot imleci/ikili arama, erken durma, cüzdan bazlı
filter/score, işlem önbelleği, ilerleme ve ayar testleri (ağ yok; sahte zincir)."""

from __future__ import annotations

import sqlite3
import sys
from contextlib import nullcontext
from dataclasses import replace

import pytest

from avimsin import filter as filter_cli
from avimsin import scan as scan_cli
from avimsin import score as score_cli
from avimsin.chains import solana
from avimsin.chains.solana import SolanaAdapter, SolanaClient
from avimsin.chains.solana_addr import (
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    b58decode,
    b58encode,
    derive_ata,
    is_on_curve,
)
from avimsin.collectors.early_buyers import earliest_buyers
from avimsin.collectors.transfers import TransferEvent
from avimsin.config import Settings
from avimsin.storage.db import connect, save_coin, save_purchase
from tests.fake_solana import FakeSolanaChain, make_balance_tx


def addr(n: int) -> str:
    return b58encode(bytes([n]) * 32)


MINT = addr(200)
POOL = derive_ata(addr(140), addr(200))  # eğri dışı (PDA) gönderen: gerçek alım kaynağı gibi
CREATOR = addr(151)


def sig(name: str, slot: int, err: dict | None = None) -> dict:
    return {"signature": name, "slot": slot, "err": err}


# --- ATA türetimi: gerçek mainnet vektörleri -------------------------------------------

# (mint, owner, ATA, token programı) — api.mainnet-beta'dan alınan işlemlerin
# postTokenBalances (owner + accountIndex → accountKeys) kayıtlarından.
VECTORS = [
    (
        "y1AZt42vceCmStjW4zetK3VoNarC1VxJ5iDjpiupump",
        "bjFSMckeDXPA7bW9nMj99A4FbUKpJYYac3DE8Nkom5S",
        "CxMyFRepxYHQVzbrFDnomHXVCkfwGmyCCDDZNRNhB7ni",
        TOKEN_PROGRAM,
    ),
    (
        "y1AZt42vceCmStjW4zetK3VoNarC1VxJ5iDjpiupump",
        "48pck8wWjKUVBbvBukMqqi8YzF25YMR6DwmJjzvPNar2",
        "EucMweNsy4XcdMA73r9EC9DFvTixxGTsQhnjSMLrQVg",
        TOKEN_PROGRAM,
    ),
    (
        "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump",
        "CQw2BBd1Sk66a4A52vg3ZYfUPNYfXgSPVtM4wNXNpPzf",
        "ACRkWYigbn8KkN8J63xT8DJv3vLGwKD6xW61RCQMxexk",
        TOKEN_2022_PROGRAM,
    ),
]


@pytest.mark.parametrize(("mint", "owner", "ata", "program"), VECTORS)
def test_derive_ata_matches_real_chain(mint, owner, ata, program) -> None:
    assert derive_ata(owner, mint, program) == ata


def test_ata_depends_on_token_program() -> None:
    mint, owner, ata, program = VECTORS[2]
    assert program == TOKEN_2022_PROGRAM
    assert derive_ata(owner, mint, TOKEN_PROGRAM) != ata  # yanlış program başka adres verir


def test_base58_roundtrip_and_curve() -> None:
    raw = b"\0\0" + bytes(range(1, 31))
    assert b58decode(b58encode(raw)) == raw
    assert b58decode(VECTORS[0][2]) and len(b58decode(VECTORS[0][2])) == 32
    # ATA'lar eğri üzerinde değildir; ed25519 taban noktası eğrinin üzerindedir.
    assert not is_on_curve(b58decode(VECTORS[0][2]))
    base_point = bytes.fromhex("5866666666666666666666666666666666666666666666666666666666666666")
    assert is_on_curve(base_point)


# --- slot imleci, ikili arama, erken durma ----------------------------------------------


def make_adapter(chain: FakeSolanaChain) -> SolanaAdapter:
    return SolanaAdapter(SolanaClient("http://sahte", transport=chain.transport(), min_interval=0.0))


def test_creation_slot_binary_search_is_logarithmic() -> None:
    sigs = [sig("c", 90_000), sig("b", 40_010), sig("a", 40_000)]
    chain = FakeSolanaChain(
        signatures=sigs, transactions={}, latest_slot=100_000,
        address_signatures={MINT: sigs}, fill_blocks=True,
    )
    adapter = make_adapter(chain)
    assert adapter.first_slot(MINT) == 40_000
    assert len(chain.calls) < 80  # ~log2(100000)=17 yoklama x (getBlock + imza) + sabitler
    # Hiçbir yoklama tüm geçmişi sayfalamadı: her imza isteği limit=1 ya da son aralık.
    assert chain.count("getSignaturesForAddress") < 45


def test_creation_slot_none_without_history() -> None:
    chain = FakeSolanaChain(signatures=[], transactions={}, address_signatures={})
    assert make_adapter(chain).first_slot(MINT) is None


def buy_chain(slots: list[int], latest: int = 100_000) -> tuple[FakeSolanaChain, list[str]]:
    wallets = [addr(i + 1) for i in range(len(slots))]
    txs, sigs = {}, []
    for i, (w, slot) in enumerate(zip(wallets, slots)):
        name = f"buy{i}"
        txs[name] = make_balance_tx(MINT, [(POOL, 1000), (w, 0)], [(POOL, 0), (w, 1000)])
        sigs.append(sig(name, slot))
    sigs.sort(key=lambda e: -e["slot"])  # yeniden eskiye
    chain = FakeSolanaChain(
        signatures=sigs, transactions=txs, latest_slot=latest, owners={MINT: TOKEN_PROGRAM},
        address_signatures={MINT: sigs}, fill_blocks=True,
    )
    return chain, wallets


def test_early_stop_opens_only_needed_transactions() -> None:
    chain, wallets = buy_chain([100, 5_000, 40_000, 90_000, 95_000])
    buyers = earliest_buyers(make_adapter(chain), MINT, 100, 2)
    assert [b.wallet for b in buyers] == wallets[:2]
    assert chain.count("getTransaction") == 2  # beşinci işleme kadar değil, yalnız 2
    assert chain.count("getSignaturesForAddress") < 20


def test_chunk_last_slot_record_not_lost() -> None:
    # İlk parça [100, 1599]; 1599 parçanın SON slotu: imleç slot_cursor(b+1) olmalı.
    chain, wallets = buy_chain([100, 1_599, 1_600])
    buyers = earliest_buyers(make_adapter(chain), MINT, 100, 3)
    assert [b.wallet for b in buyers] == wallets


def test_early_stop_same_slot_order_is_reverse_of_listing() -> None:
    chain, wallets = buy_chain([100, 100, 100])
    # getSignaturesForAddress aynı slotta yeniden eskiye döner (buy2, buy1, buy0) → ters çevrilir.
    buyers = earliest_buyers(make_adapter(chain), MINT, 100, 3)
    assert [b.tx for b in buyers] == ["buy2", "buy1", "buy0"]


def test_earliest_buyers_without_stream_uses_token_transfers() -> None:
    class Plain:
        def block_number(self):
            return 10

        def token_transfers(self, token, a, b):
            return [TransferEvent("0xa", "0xb", 1, "t1", 5, token)]

    buyers = earliest_buyers(Plain(), "0xtok", 0, 3)
    assert [b.wallet for b in buyers] == ["0xb"]


# --- scan: --from-block opsiyonel --------------------------------------------------------


def run_cli(monkeypatch, module, path, coin, *args):
    monkeypatch.setattr(sys, "argv", [module.__name__, coin, "--db", str(path), *args])
    module.main()


def test_scan_without_from_block_uses_creation_slot(tmp_path, monkeypatch) -> None:
    chain, wallets = buy_chain([40_000, 40_100, 40_200])
    adapter = make_adapter(chain)
    monkeypatch.setattr(scan_cli, "open_chain", lambda c: nullcontext(adapter))
    path = tmp_path / "t.sqlite"
    run_cli(monkeypatch, scan_cli, path, MINT, "--chain", "solana", "--count", "2")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT first_block FROM coins").fetchone() == (40_000,)
        assert [r[0] for r in conn.execute("SELECT wallet FROM purchases ORDER BY block")] == wallets[:2]
    assert chain.count("getTransaction") == 2


def test_scan_evm_still_requires_from_block(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scan_cli, "open_chain", lambda c: pytest.fail("RPC açılmamalı"))
    with pytest.raises(SystemExit, match="zorunlu"):
        run_cli(monkeypatch, scan_cli, tmp_path / "t.sqlite", "0x" + "ab" * 20, "--chain", "robinhood")


# --- cüzdan bazlı filter / score ---------------------------------------------------------


def wallet_world(extra_in_window: int = 0, silent: bool = False, edge: bool = False):
    """Üç alıcı: w0 temiz, w1 çok işlemli (ATA'sında extra_in_window ek işlem), w2 ATA'sız (silent)."""
    wallets = [addr(1), addr(2), addr(3)]
    txs: dict[str, dict] = {}
    per_wallet: dict[str, list[dict]] = {w: [] for w in wallets}
    all_sigs: list[dict] = []

    def add(name, slot, wallet, pre, post, track=True):
        txs[name] = make_balance_tx(MINT, pre, post)
        entry = sig(name, slot)
        all_sigs.append(entry)
        if track:
            per_wallet[wallet].append(entry)

    for i, w in enumerate(wallets):
        add(f"buy{i}", 100 + i, w, [(POOL, 1000), (w, 0)], [(POOL, 0), (w, 1000)], track=not silent)
    for k in range(extra_in_window):
        add(f"churn{k}", 200 + k, wallets[1], [(POOL, 5), (wallets[1], 1000)], [(POOL, 0), (wallets[1], 1005)])
    for k in range(3):  # pencere SONRASI işlemler (to_block=2000)
        add(f"late{k}", 3000 + k, wallets[0], [(POOL, 5), (wallets[0], 1000)], [(POOL, 0), (wallets[0], 1005)])
    if edge:  # pencerenin SON slotunda (to_block=2000) ATA işlemi
        add("edge", 2000, wallets[0], [(POOL, 7), (wallets[0], 1000)], [(POOL, 0), (wallets[0], 1007)])
    all_sigs.sort(key=lambda e: -e["slot"])
    program = TOKEN_PROGRAM
    ata_sigs = {
        derive_ata(w, MINT, program): sorted(per_wallet[w], key=lambda e: -e["slot"]) for w in wallets
    }
    chain = FakeSolanaChain(
        signatures=all_sigs, transactions=txs, latest_slot=5_000, owners={MINT: program},
        address_signatures={MINT: all_sigs, **ata_sigs}, fill_blocks=True,
    )
    return chain, wallets, ata_sigs


def seed_db(path, wallets):
    conn = connect(path)
    with conn:
        save_coin(conn, MINT, 100, "solana")
        for i, w in enumerate(wallets):
            save_purchase(conn, MINT, w, 100 + i, f"buy{i}")
    conn.close()


def verdicts(path):
    with sqlite3.connect(path) as conn:
        return {r[0]: r[1:] for r in conn.execute("SELECT wallet, verdict, rule, reason FROM coin_verdicts")}


def run_filter(monkeypatch, path, chain):
    adapter = make_adapter(chain)
    monkeypatch.setattr(filter_cli, "open_chain", lambda c: nullcontext(adapter))
    run_cli(monkeypatch, filter_cli, path, MINT, "--chain", "solana", "--from-block", "100", "--to-block", "2000")


def test_scan_buy_visible_in_wallet_ata_signatures() -> None:
    chain, wallets, ata_sigs = wallet_world()
    buyers = earliest_buyers(make_adapter(chain), MINT, 100, 3)
    client = SolanaClient("http://sahte", transport=chain.transport(), min_interval=0.0)
    for b in buyers:
        ata = derive_ata(b.wallet, MINT, TOKEN_PROGRAM)
        assert b.tx in {e["signature"] for e in client.get_signatures_for_address(ata)}


def test_filter_uses_wallet_signatures_and_skips_post_window(tmp_path, monkeypatch) -> None:
    chain, wallets, ata_sigs = wallet_world()
    path = tmp_path / "t.sqlite"
    seed_db(path, wallets)
    run_filter(monkeypatch, path, chain)
    opened = {p[0] for m, p in chain.calls if m == "getTransaction"}
    assert not any(name.startswith("late") for name in opened)  # pencere sonrası açılmadı
    # Mint adresi hiç sayfalanmadı: yalnız ATA'lar (+ getBlock imleci) sorgulandı.
    for m, p in chain.calls:
        if m == "getSignaturesForAddress":
            assert p[0] != MINT
            assert "before" in p[1]  # imleç: to_block+1 öncesi


def test_filter_no_warning_when_atas_active(tmp_path, monkeypatch, capsys) -> None:
    chain, wallets, _ = wallet_world()
    path = tmp_path / "t.sqlite"
    seed_db(path, wallets)
    run_filter(monkeypatch, path, chain)
    assert "UYARI" not in capsys.readouterr().out
    v = verdicts(path)
    assert v[wallets[0]][0] == "ok"


def test_filter_warns_when_most_atas_silent(tmp_path, monkeypatch, capsys) -> None:
    chain, wallets, _ = wallet_world(silent=True)  # yanlış ATA gibi: hiçbir ATA'da işlem yok
    path = tmp_path / "t.sqlite"
    seed_db(path, wallets)
    run_filter(monkeypatch, path, chain)
    out = capsys.readouterr().out
    assert "UYARI: 3/3" in out
    assert all(v[0] is None for v in verdicts(path).values())  # sessizce "bilinmiyor"


def test_max_tx_per_wallet_marks_bot_without_opening(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(solana, "settings", replace(solana.settings, solana_max_tx_per_wallet=5))
    chain, wallets, _ = wallet_world(extra_in_window=8)
    path = tmp_path / "t.sqlite"
    seed_db(path, wallets)
    run_filter(monkeypatch, path, chain)
    verdict, rule, reason = verdicts(path)[wallets[1]]
    assert (verdict, rule) == ("bot", "high_frequency")
    assert "≥6" in reason
    opened = {p[0] for m, p in chain.calls if m == "getTransaction"}
    assert not any(name.startswith("churn") for name in opened)


def test_score_reuses_cache_and_never_writes_window(tmp_path, monkeypatch) -> None:
    chain, wallets, _ = wallet_world()
    path = tmp_path / "t.sqlite"
    seed_db(path, wallets)
    run_filter(monkeypatch, path, chain)
    after_filter = chain.count("getTransaction")
    assert after_filter > 0
    with sqlite3.connect(path) as conn:
        window_before = conn.execute("SELECT * FROM analysis_windows").fetchall()
        assert conn.execute("SELECT COUNT(*) FROM sol_tx_cache").fetchone()[0] == after_filter
    adapter = make_adapter(chain)
    monkeypatch.setattr(score_cli, "open_chain", lambda c: nullcontext(adapter))
    run_cli(monkeypatch, score_cli, path, MINT, "--chain", "solana")
    assert chain.count("getTransaction") == after_filter  # score yeniden indirmedi
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM analysis_windows").fetchall() == window_before


def test_tx_cache_second_run_makes_no_get_transaction(tmp_path) -> None:
    chain, wallets, _ = wallet_world()
    path = str(tmp_path / "c.sqlite")
    connect(path).close()
    for expected_calls in (None, 0):
        adapter = make_adapter(chain)
        adapter.attach_tx_cache(path)
        before = chain.count("getTransaction")
        result = adapter.wallet_transfers(MINT, wallets, 100, 2000)
        adapter.close()
        made = chain.count("getTransaction") - before
        if expected_calls is not None:
            assert made == expected_calls
        else:
            assert made == 3
        assert len(result.events) == 3
    with sqlite3.connect(path) as conn:
        body = conn.execute("SELECT body FROM sol_tx_cache LIMIT 1").fetchone()[0]
    assert "uiTokenAmount" in body and "version" not in body


def test_filter_failure_restores_window(tmp_path, monkeypatch) -> None:
    chain, wallets, _ = wallet_world()
    path = tmp_path / "t.sqlite"
    seed_db(path, wallets)
    adapter = make_adapter(chain)

    def boom(*a, **k):
        raise RuntimeError("rpc düştü")

    monkeypatch.setattr(adapter, "wallet_transfers", boom)
    monkeypatch.setattr(filter_cli, "open_chain", lambda c: nullcontext(adapter))
    with pytest.raises(RuntimeError):
        run_cli(monkeypatch, filter_cli, path, MINT, "--chain", "solana", "--from-block", "100", "--to-block", "2000")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM analysis_windows").fetchone()[0] == 0


def test_wallet_transfers_window_end_slot_and_cursor_calls() -> None:
    chain, wallets, _ = wallet_world(edge=True)
    result = make_adapter(chain).wallet_transfers(MINT, wallets, 100, 2000)
    assert "edge" in {e.tx for e in result.events}  # to_slot'taki işlem dahil
    ata_calls = [p for m, p in chain.calls if m == "getSignaturesForAddress" and p[0] != MINT]
    assert ata_calls
    assert any(m == "getBlock" and p[0] == 2001 for m, p in chain.calls)  # imleç to_slot+1'den
    assert {a for a, _ in ata_calls} == {derive_ata(w, MINT, TOKEN_PROGRAM) for w in wallets}
    first_per_address = {}
    for address, opts in ata_calls:
        first_per_address.setdefault(address, opts)
    assert all(o.get("before") == "fill2001" for o in first_per_address.values())  # ilk sayfa imleçli
    for address, opts in ata_calls:
        # Sunucunun döndürdüğü imzaların hiçbiri to_slot'tan yeni değil: yeni imza hiç indirilmedi.
        assert all(e["slot"] <= 2000 for e in chain._signatures(address, opts))
    opened = {p[0] for m, p in chain.calls if m == "getTransaction"}
    assert not any(name.startswith("late") for name in opened)


# --- ilerleme ve ayar --------------------------------------------------------------------


def test_progress_lines_on_stderr_without_url(monkeypatch, capsys) -> None:
    monkeypatch.setattr(solana, "PROGRESS_EVERY", 2)
    chain, wallets, _ = wallet_world(extra_in_window=4)
    adapter = make_adapter(chain)
    adapter.wallet_transfers(MINT, wallets, 100, 2000)
    captured = capsys.readouterr()
    assert "işlem açıldı" in captured.err
    assert "http" not in captured.err and "http" not in captured.out


def test_min_interval_env_and_default(monkeypatch) -> None:
    assert Settings.from_env().solana_min_interval_seconds == 0.9
    monkeypatch.setenv("SOLANA_MIN_INTERVAL_SECONDS", "0.25")
    monkeypatch.setenv("SOLANA_MAX_TX_PER_WALLET", "7")
    s = Settings.from_env()
    assert (s.solana_min_interval_seconds, s.solana_max_tx_per_wallet) == (0.25, 7)
    monkeypatch.setattr(solana, "settings", s)
    with SolanaClient("http://x") as client:
        assert client.min_interval == 0.25


# --- getTransaction null ----------------------------------------------------------------


def test_null_transaction_skipped_counted_not_cached(tmp_path, monkeypatch, capsys) -> None:
    waits = []
    monkeypatch.setattr(solana.time, "sleep", waits.append)
    chain, wallets, _ = wallet_world()
    chain.null_first["buy1"] = 99  # kalıcı null
    path = str(tmp_path / "c.sqlite")
    connect(path).close()
    adapter = make_adapter(chain)
    adapter.attach_tx_cache(path)
    result = adapter.wallet_transfers(MINT, wallets, 100, 2000)
    assert {e.tx for e in result.events} == {"buy0", "buy2"}  # buy1 atlandı, çökmedi
    assert adapter.unreadable == ["buy1"]
    assert waits == [2.0, 4.0]  # 2 yeniden deneme
    assert sum(1 for m, p in chain.calls if m == "getTransaction" and p[0] == "buy1") == 3
    adapter.close()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sol_tx_cache WHERE signature='buy1'").fetchone()[0] == 0
    from avimsin.chains import report_unreadable

    report_unreadable(adapter)
    captured = capsys.readouterr()
    assert "UYARI: 1 işlem RPC'den okunamadı" in captured.out and "buy1" in captured.err


def test_null_then_filled_on_retry_is_used(monkeypatch) -> None:
    monkeypatch.setattr(solana.time, "sleep", lambda s: None)
    chain, wallets, _ = wallet_world()
    chain.null_first["buy1"] = 1
    adapter = make_adapter(chain)
    result = adapter.wallet_transfers(MINT, wallets, 100, 2000)
    assert {e.tx for e in result.events} == {"buy0", "buy1", "buy2"}
    assert adapter.unreadable == []


def test_ata_progress_every_ten_wallets(capsys) -> None:
    chain, _, _ = wallet_world()
    ws = [addr(i + 10) for i in range(20)]
    make_adapter(chain).wallet_transfers(MINT, ws, 100, 2000)
    assert "ATA imzaları: 10/20 cüzdan" in capsys.readouterr().err


# --- ATA dışı hesap ve alım tanımı --------------------------------------------------------


def _tx_with_accounts(accounts, mint, pre, post):
    """accounts: token hesap adresleri; pre/post: (hesap_indeksi, owner, miktar)."""

    def side(entries):
        return [
            {"accountIndex": i, "mint": mint, "owner": o, "uiTokenAmount": {"amount": str(a)}}
            for i, o, a in entries
        ]

    return {
        "transaction": {"message": {"accountKeys": accounts}},
        "meta": {"preTokenBalances": side(pre), "postTokenBalances": side(post)},
    }


def test_non_ata_token_account_found_via_purchase_tx_and_dedup() -> None:
    wallet = addr(7)
    ata = derive_ata(wallet, MINT, TOKEN_PROGRAM)
    odd = addr(88)  # ATA olmayan token hesabı
    pool_ta = addr(89)
    txs = {
        "buy": _tx_with_accounts(
            [pool_ta, odd], MINT, [(0, POOL, 1000), (1, wallet, 0)], [(0, POOL, 0), (1, wallet, 1000)]
        ),
        "dup": _tx_with_accounts(
            [pool_ta, odd, ata], MINT,
            [(0, POOL, 50), (1, wallet, 1000), (2, wallet, 0)],
            [(0, POOL, 0), (1, wallet, 1000), (2, wallet, 50)],
        ),
    }
    buy, dup = sig("buy", 100), sig("dup", 120)
    chain = FakeSolanaChain(
        signatures=[dup, buy], transactions=txs, latest_slot=5_000, owners={MINT: TOKEN_PROGRAM},
        address_signatures={MINT: [dup, buy], ata: [dup], odd: [dup, buy]}, fill_blocks=True,
    )
    adapter = make_adapter(chain)
    without = adapter.wallet_transfers(MINT, [wallet], 100, 2000)
    assert {e.tx for e in without.events} == {"dup"}  # yalnız ATA: alım görünmez
    result = adapter.wallet_transfers(MINT, [wallet], 100, 2000, {wallet: ["buy"]})
    txs_seen = [e.tx for e in result.events]
    assert "buy" in txs_seen
    assert txs_seen.count("dup") == 1  # iki hesapta görünen imza tek sayıldı


def test_wallet_to_wallet_transfer_is_not_a_purchase() -> None:
    from avimsin.collectors.early_buyers import DistributionSkips

    on_curve = next(addr(n) for n in range(1, 120) if is_on_curve(bytes([n]) * 32))
    assert not is_on_curve(b58decode(POOL))  # PDA vektörü: türetilmiş ATA eğri dışıdır
    txs = {
        "w2w": make_balance_tx(MINT, [(on_curve, 100), (addr(60), 0)], [(on_curve, 0), (addr(60), 100)]),
        "buy": make_balance_tx(MINT, [(POOL, 100), (addr(61), 0)], [(POOL, 0), (addr(61), 100)]),
    }
    sigs = [sig("buy", 300), sig("w2w", 200)]
    chain = FakeSolanaChain(
        signatures=sigs, transactions=txs, latest_slot=5_000,
        address_signatures={MINT: sigs}, fill_blocks=True,
    )
    skips = DistributionSkips()
    buyers = earliest_buyers(make_adapter(chain), MINT, 100, 5, skips)
    assert [b.wallet for b in buyers] == [addr(61)]
    assert skips.wallet_to_wallet == 1
