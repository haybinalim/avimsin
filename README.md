# Avımsın (avimsin) 🏹

[![CI](https://github.com/haybinalim/avimsin/actions/workflows/ci.yml/badge.svg)](https://github.com/haybinalim/avimsin/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

> **EN:** Avımsın finds the wallets that bought a coin early, filters out bots and panic sellers, ranks the remaining smart-money wallets by performance and sends a Telegram alert when they buy together above your threshold. Python · on-chain analytics.

**Smart wallet bulucu ve takipçi.** Bir coin'i erken alan cüzdanları bulur, bot
cüzdanlarını ve kağıt-elyaf (panik satan) cüzdanları eler, kalan **smart wallet**
listesini performansa göre sıralar ve belirlediğiniz eşikte toplu alım olduğunda
Telegram'dan haber verir.

> Repo adı `avimsin` — GitHub Türkçe karakter desteklemediği için; proje adı **Avımsın**.

## Ekran Görüntüleri / Screenshots

> Görüntüler **sahte demo verisiyle** alınmıştır: testlerdeki sahte EVM zinciri
> (`tests/fake_evm.py`) üzerinde gerçek `scan → filter → score` CLI'ları çalıştırıldı;
> adresler ve miktarlar uydurmadır, gerçek cüzdan veya RPC verisi değildir.
> *EN: Captured with fake demo data (the test suite's fake EVM chain) — not real wallets.*

**Streamlit paneli** — cüzdan durumu (temiz / bot / kaçak satış) ve smart wallet sıralaması:

![Avımsın Streamlit paneli: 12 temiz, 5 bot, 3 kaçak satış cüzdanı ve skor, winrate, net akış sütunlarıyla smart wallet sıralama tablosu](docs/screenshots/dashboard.png)

**CLI** — `avimsin-filter` özeti ve `avimsin-score` sıralama tablosu:

![Terminalde avimsin-filter özeti (20 cüzdan: 12 temiz, 5 bot, 3 hızlı çıkış) ve avimsin-score ile skorlanmış 10 cüzdanın sıralama tablosu](docs/screenshots/cli-filter-score.png)

## Mevcut doğruluk sınırı

Bu sürüm transfer tabanlı bir göstergedir: token girişi doğrulanmış satın alma,
token çıkışı satış değildir. Dump kuralı ilk geçerli girişten başlayan pencerede
pozitif çıkış/giriş **miktarını** karşılaştırır; fiyat ve zarar koşulu henüz yoktur.
Skorun oranı ekonomik winrate, net token akışı finansal P&L değildir. Gerçek swap
ve maliyet modeli eksiktir.

**Dağıtım ayrımı:** aynı işlemde aynı göndericiden `DISTRIBUTION_MIN_RECEIVERS` (varsayılan 3) veya
daha fazla farklı alıcıya giden transferler içeriden dağıtım (dev/bundle) sayılır; bu alıcılar
erken alıcı olmaz (scan "N dağıtım alıcısı atlandı" yazar). Her cüzdana ayrı işlemle yapılan
dağıtım bu kuralla yakalanmaz. Kural yalnız Solana'da geçerlidir; EVM yolu değişmedi.

**Alım tanımı (Solana):** alım yalnız gönderen token sahibi eğri DIŞI bir adresse (PDA: bonding
curve / havuz vault sahibi) sayılır; eğri üstü normal cüzdandan gelen cüzdandan cüzdana transferdir,
erken alıcı değildir (scan "N cüzdan-cüzdan transferi alım sayılmadı" yazar). Havuzu normal
cüzdan adresiyle çalışan nadir kurulumlar bu yüzden kaçabilir.

**Solana hızlı tarama (public RPC ~1 istek/sn):**
- `avimsin-scan <mint>`: `--from-block` Solana'da opsiyoneldir; verilmezse başlangıç mint'in
  oluşturulduğu slottur (`getBlock` imleç imzası + `getSignaturesForAddress(before=...)` ile
  ikili arama, O(log) istek; tüm geçmiş sayfalanmaz). EVM'de `--from-block` zorunludur.
  İmzalar eskiden yeniye ~1500 slotluk parçalarla açılır; `--count` alıcı bulununca
  `getTransaction` durur. Aynı slot içi sıra, `getSignaturesForAddress` sırasının tersidir.
- `avimsin-filter`/`avimsin-score`: mint'in tüm işlemleri yerine her cüzdanın o mint için
  ATA (associated token account) adresinin imzaları, pencere içinde (pencere sonrası imzalar
  indirilmez) açılır. **Bilinen sınır:** ATA dışı token hesabı kullanan cüzdanın transferleri
  görülmez. Pencerede `SOLANA_MAX_TX_PER_WALLET` (varsayılan 100) imzayı aşan cüzdanın işlemleri
  açılmaz; `bot` / `high_frequency` sayılır. Alıcıların yarısından fazlasının ATA'sında hiç işlem
  yoksa filter UYARI basar (yanlış ATA ya da pencere belirtisi). `getFirstAvailableBlock`'tan
  eski işlemler aranmaz.
- `getTransaction` sonucu (yalnız gereken alanlar) aynı SQLite'ta `sol_tx_cache` tablosunda
  önbelleğe alınır; score, filter'ın indirdiğini yeniden indirmez. Filter önbellek için ayrı
  bağlantı kullandığından pencere kaydını erken commit eder; çalışma hata verirse eski pencere geri yazılır.
  Bilinen sınır: süreç SIGKILL ile öldürülürse (geri yazma çalışamaz) yeni pencere kayıtlı kalabilir;
  `avimsin-filter`'ı yeniden çalıştırın.
- Env: `SOLANA_MIN_INTERVAL_SECONDS` (çağrılar arası asgari süre, varsayılan 0.9; ücretli uçta
  düşürülebilir), `SOLANA_MAX_TX_PER_WALLET` (varsayılan 100). Uzun işlemlerde stderr'e seyrek
  ilerleme satırı basılır ("n/N işlem açıldı"; URL yazılmaz). `watch` bu yoldan etkilenmez.

**Karar ve skor coin bazlıdır:** `coin_verdicts(coin, wallet, ...)` ve
`coin_scores(coin, wallet, ...)` tabloları. `avimsin-filter <coin>` ve
`avimsin-score <coin>` yalnız o coin'in satırlarını yazar/yeniler; başka coin'deki
sonuçlara dokunmaz. Eski `wallets.verdict*` kolonları ve `scores` tablosu silinmedi
ama artık yazılmaz ve okunmaz; eski global kayıtların coin bilgisi yoktur, taşınmaz.
Eski bir veritabanında yeni tablolar boşken CLI ve panel bunu belirten bir not
gösterir: `avimsin-filter` ve `avimsin-score`'u her coin için yeniden çalıştırın.

`score` uygunluğu o coin'in `coin_verdicts` kaydından (`verdict=ok`) okur; yeniden
çalıştırma yalnız bu coin'in skorlarını atomik yeniler (çıktısız/uygunsuz olanları
kaldırır). Uygun olmayan bir karar yalnız o coin'in skorunu siler.
Boş filtre geçmişi temiz sayılmaz. CLI token birimi gösterir; DB'deki net akış
tam tokene yuvarlanır, panel hassasiyeti bu aşamada düzeltilmiş değildir.

**Kalıcı analiz penceresi:** `avimsin-filter` ve `avimsin-score` aynı `analysis_windows(coin,
from_block, to_block, complete)` kaydını kullanır; "en güncel blok" her çalıştırmada
kaymaz. İlk çalıştırmada hedef `ANALYSIS_WINDOW_SECONDS` (varsayılan 86400 = 24 saat),
zincirin blok süresiyle (Solana 0.4 sn, Robinhood 0.25 sn) bloğa çevrilir:
`to = from + pencere`. Hedef en güncel bloğu aşarsa pencere orada kesilir, `complete=0`
kaydedilir ve çıktıya `UYARI: pencere eksik (… / … blok)` yazılır; sonraki çalıştırmada
yetişilmişse hedefe genişler ve `complete=1` olur. `complete=1` pencere bir daha kaymaz.
Pencereyi yalnız `avimsin-filter` oluşturur, genişletir ve günceller; `avimsin-score` salt-okurdur
(kayıt yoksa RPC'den önce hata; eksik pencerede kayıtlı aralıkla çalışır ve uyarır; kayıtla
uyuşmayan açık `--from-block`/`--to-block` hatadır). Filter'da açık `--from-block`/`--to-block`
kaydı günceller (`Pencere güncellendi: eski → yeni`); yalnız `--from-block` verilirse bitiş yeni
başlangıçtan yeniden hesaplanır (eski bitiş korunmaz); `--to-block` en güncel bloktan büyükse hata verilir. Sonuçlar yalnız o pencerenin verisine
dayanır; eksik pencerede skorlar da eksik veriyle hesaplanmıştır. `avimsin-scan` pencereye dokunmaz.

**Solana izleme (varsayılan: cüzdan bazlı).** Her tur her smart wallet için
`getSignaturesForAddress(until=<son imza>)` çağrılır; yalnız yeni imzaların işlemleri açılır
(`sol_tx_cache` kullanılır). Cüzdanın sahibi olduğu token bakiyesi artan (post − pre > 0) HER mint
giriştir; coins tablosunda olması gerekmez, yani taranmamış yeni coinler de görülür. `meta.err`
olan işlemler ile wSOL/USDC/USDT (`WATCH_IGNORED_MINTS`) sayılmaz. Cursor cüzdan başınadır
(`watch_wallet_cursor`); ilk görülen cüzdanda geriye dönük tarama yoktur (cursor en yeni imzaya
konur). Cüzdan başına tur sınırı `WATCH_MAX_TX_PER_WALLET_POLL` (50): aşılırsa en yeni 50 açılır,
kalanlar atlanır (log) ve cursor en yeniye geçer. Cüzdan RPC hatasında yalnız o cüzdanın cursor'ı
ilerlemez. Her tur tek satır özet basar: `tur: N cüzdan, M yeni işlem, K giriş, H hata, süre s`;
tüm cüzdanlar hata verirse tur RPC hatası sayılır (`--once` çıkış kodu 1). Sinyal mesajı mint
adresini ve DexScreener bağlantısını içerir. Eski coin bazlı yol için `WATCH_SOLANA_MODE=coin`;
EVM her zaman coin bazlıdır. Servis `PYTHONUNBUFFERED=1` ile çalışır (journal'da satırlar anında görünür).
Yeniden denemeden sonra da okunamayan işlem uyarıyla atlanır (cüzdan kilitlenmesin diye; scan yoluyla aynı), sayısı özet satırında `atlanan` olarak görünür. Kayıp riski: atlanan işlemdeki bir giriş sinyale girmez.
**Yüksek frekans kuralı (cüzdan modu).** Son `WATCH_BOT_WINDOW_SECONDS` (3600) içinde
`WATCH_BOT_MAX_INFLOWS` (10) sayısından FAZLA farklı giriş kaydı (`watch_inflows`, slot bazlı) olan
cüzdan sinyal sayımına katılmaz; girişleri yine kaydedilir, yalnız sayımdan süzülür. Kalıcı bot
işareti yoktur: pencere kayınca cüzdan kendiliğinden döner. Dışlananların sayısı tur özetinde
(`B bot-dışı`), ilk dışlanmada adresi tek log satırında görünür. Öneri (uygulanmadı): sürekli
dışlanan cüzdanları `coin_verdicts`'e kalıcı `bot` olarak yazan ayrı bir yol eklenebilir.
Aşağıdaki açıklama coin bazlı yol (EVM) içindir.

`watch` yalnız seçilen zincirin açıkça etiketli coinlerini izler. Smart wallet: seçili zincirde en az bir coin'de `verdict=ok`
olan ve o zincirin hiçbir coin'inde `verdict=bot` olmayan cüzdan (dump coin'e özgüdür,
tek başına dışlamaz; zinciri NULL coinler sayılmaz). Küme boşsa log'a uyarı yazılır
(ilk turda ve dolu→boş geçişinde). Mint/burn,
sıfır/negatif ve öz-transferler sayılmaz. Cursor kalıcıdır (`watch_cursor`):
yeniden başlatmada kesinti taranır; `WATCH_MAX_SLOTS_PER_POLL` aşılırsa aralık
atlanır (log satırı) ve cursor güncele çekilir. Eşik, son
`WATCH_SIGNAL_WINDOW_SECONDS` (varsayılan 3600) içindeki farklı cüzdan sayısıyla
turlar arası birikerek değerlendirilir; süre blok sayısına zincir aralığıyla çevrilir.
Aynı küme tekrar bildirilmez; yeni cüzdan katılırsa "güncelleme" gider; ilk tam sinyalden
`WATCH_ALERT_COOLDOWN_SECONDS` (86400) sonra coin sıfırlanır (güncellemeler süreyi uzatmaz). Telegram hatası döngüyü
düşürmez: log satırı yazılır, sinyal sonraki turda yeniden denenir. Sınırlar: gönderim
sinyal penceresinden uzun süre başarısız kalırsa sinyal düşer; bu tablolar ayrık olarak
zincire göre tutulur, transferler yine doğrulanmış alım değildir.
RPC hatası (retry bütçesi bitince) döngüyü düşürmez: log satırına yalnız sınıf adı/durum
kodu yazılır (URL/anahtar yok), tur hepsi-ya-hiç iptal edilir (cursor ilerlemez, hiçbir giriş
yazılmaz), bekleyen sinyaller yine gönderilir. `--once` modunda RPC hatası çıkış kodu 1 verir.
Açılışta ilk `block_number` hatası süreci düşürür (systemd yeniden başlatır).

## Vizyon

Önce Solana, sonra Robinhood ve diğer EVM ağları. Örnek senaryo: popüler bir ağda (önce Solana; sonra örn. Robinhood Chain) bir meme coin çok yükselir.
Avımsın o coini **ilk alan 50 cüzdanı** bulur; içinden:

- **Botları** tespit edip listeden çıkarır,
- Aldıktan sonra **zarardayken yarısından fazlasını satanları** listeden çıkarır,
- Zamanla ekleyeceğiniz kurallarla listeyi temiz tutar.

Kalan smart wallet'lar; **trade sayısı, kazanma oranı, kazanma miktarı ve işlem
sıklığı** gibi kategorilerde sıralanır. Bu cüzdanlardan **en az N tanesi** (sizin
belirleyeceğiniz eşik, ör. 3 veya 5) bir coin aldığında **Telegram bildirimi** gelir.

## Yol Haritası

| Faz | İçerik | Durum |
|-----|--------|-------|
| 0 | Repo kurulumu, Python ortamı, mimari iskelet | ✅ |
| 1 | Veri katmanı: erken alıcılar + transfer/swap geçmişi (Solana öncelikli; Robinhood Chain/EVM RPC desteği korunur) | ✅ (erken alıcılar; swap geçmişi Faz 2'de) |
| 2 | Filtre motoru: bot tespiti, kaçak satış kuralları (büyüyebilir kural seti) | ✅ (fiyat-bağımsız kurallar; P&L bazlı zenginleştirme Faz 3'te) |
| 3 | Skorlama & sıralama: kazanma oranı, P&L, işlem sıklığı | ✅ (fiyat-bağımsız; USD P&L Faz 4+ta) |
| 4 | Streamlit paneli: smart wallet listesini tarayıcıda görüntüleme | ✅ (`uv run streamlit run dashboard/app.py`) |
| 6 | Oracle Free VPS'e 7/24 dağıtım (systemd) + önce Solana, sonra Robinhood ve diğer EVM ağları | ⏳ (VPS paketi: `deploy/` — kurulum `deploy/README.md`; varsayılan zincir Solana, EVM: `--chain robinhood`) |

## Mimari

```
src/avimsin/
├── chains/          # Ağ adaptörleri (evm_base.py, robinhood.py, ... solana.py)
├── collectors/      # Veri toplama: alıcılar, transferler, swap'lar
├── filters/         # Kurallar: bot tespiti, kaçak satış ... (büyüyebilir)
├── scoring/         # Skorlama: winrate, P&L, frekans
├── alerts/          # Telegram bildirimleri (eşik bazlı)
├── storage/         # SQLite kalıcılık
└── config.py        # .env'den ayar okuma
dashboard/           # Streamlit paneli (Faz 4)
data/                # SQLite veritabanı (git'e girmez)
```

**Yeni ağ eklemek** = `chains/` klasörüne yeni bir adaptör dosyası eklemek.
Kod ağ-bağımsız yazılır; EVM ağları ve Solana aynı arayüzü kullanır.

## Kurulum (geliştirici makinesi)

```bash
cd ~/Projects/avimsin
uv sync                # Python 3.12 sanal ortamını kurar, bağımlılıkları yükler
uv run avimsin-rpc     # RPC uçlarını sıralı ölçer; aynı ağ içinde karşılaştırır
uv run avimsin-scan <mint> [--from-block <slot>]   # Solana (varsayılan): erken alıcıları topla (slot opsiyonel)
uv run avimsin-filter <mint>    # alıcıları bot/kaçak satış kurallarından geçir
uv run avimsin-score <mint>     # temiz cüzdanları winrate/P&L/frekans ile sırala
uv run avimsin-watch            # sinyal izleyici (Telegram'a bildirir; varsayılan solana)
uv run avimsin-discover --dry-run   # kurala uyan yeni Solana coin'lerini göster (yazmaz)
uv run avimsin-discover         # adayları discovery_queue'ya ekle
uv run avimsin-pipeline         # kuyruktan en çok 3 coin: scan -> filter -> score + Telegram özeti
uv run avimsin-scan 0x<kontrat> --chain robinhood --from-block <blok>   # EVM örneği
uv run pytest                 # test ağı (sahte EVM/Solana zincirleri, ağ yok)
```

`scan/filter/score/watch` komutları `--chain robinhood|solana` bayrağıyla zincir
seçer (varsayılan: kayıtlı zincir, yoksa `solana`; `watch` için `solana`); Solana ucu `.env`'de `RPC_SOLANA_*` ile verilir,
verilmezse public devnet ucu kullanılır.

`0x` EVM adresi `--chain robinhood` olmadan verilirse (ya da base58 adres
`robinhood` ile) RPC sorgusundan önce net hata verilir. Kayıtlı zincir ile `--chain` uyuşmazsa scan/filter/score RPC sorgusundan önce
durur. Zinciri bilinmeyen eski kayıtlarda `--chain` açıkça verilmelidir;
başarılı işlemde kayıt tamamlanır. Watch bilinmeyen zincirli coinleri atlar.

## Otomatik keşif (günlük tarama)

`avimsin-discover` GeckoTerminal ve DexScreener'dan (anahtarsız) yeni Solana coin'lerini
bulur; kural (ortam değişkenleri): yaş `DISCOVERY_MIN_AGE_DAYS`–`DISCOVERY_MAX_AGE_DAYS` (3–30 gün),
piyasa değeri (yoksa FDV) ≥ `DISCOVERY_MIN_MCAP_USD`, likidite ≥ `DISCOVERY_MIN_LIQUIDITY_USD`,
24s hacim ≥ `DISCOVERY_MIN_VOLUME_24H_USD`; stable/wSOL (`WATCH_IGNORED_MINTS`) hariç. Uyanlar
`discovery_queue` tablosuna eklenir (`coins`'te ya da kuyrukta olan coin atlanır). Bir kaynak
düşerse diğeriyle devam eder (çıkış 0). Not: GeckoTerminal'de `market_cap_usd` çoğu zaman
boştur ve FDV kullanılır; FDV dolaşımdaki arzı hesaba katmadığı için piyasa değerini
şişirebilir (mcap eşiğini geçmesi gerçek mcap'i garanti etmez); ikisi de düşerse ya da ikisi de 0 ham sonuç verirse çıkış 1.

`avimsin-pipeline` kuyruktan en çok `DISCOVERY_MAX_COINS_PER_RUN` (3) coin alır (hacmi
yüksek önce), her biri için scan/filter/score'u ayrı alt süreçte çalıştırır (coin başına
`PIPELINE_COIN_TIMEOUT_SECONDS`, 3 saat). Durumlar: `done`, `done_warn` (alıcıların >%50'si
için pencerede transfer yok: eski coin olabilir), `failed`, `timeout`. Takılan ya da
başarısız coin sıradakini engellemez; `failed`/`timeout` coin kuyrukta yeniden denenmez
(elle yeniden kuyruğa almak için:
`sqlite3 data/avimsin.sqlite "UPDATE discovery_queue SET status='pending' WHERE coin='<mint>'"`
ya da Python ile aynı UPDATE). Tur sonunda tek Telegram özeti gider
(ayar yoksa stdout). Aynı anda tek pipeline (`<db>.pipeline.lock`). Yeni smart wallet'lar
`avimsin-watch` tarafından bir sonraki turda kendiliğinden alınır. Zamanlayıcı: `deploy/README.md`.

## RPC Stratejisi

`avimsin-rpc`, `.env` içindeki tüm `RPC_<AD>` uçlarını **sıralı** yoklar;
paylaşılan public RPC kotası için paralel istek göndermez. `RPC_SOLANA*`
adları Solana `getGenesisHash`/`getSlot`, diğer adlar (eski `RPC_ALCHEMY`,
`RPC_QUICKNODE` dahil) EVM `eth_chainId`/`eth_blockNumber` ile ölçülür.
Tablolar protokol ve ağ kimliğine (genesis hash / chain ID) göre ayrılır.
En hızlı uç ve 2 blok/slot üzerindeki gerilik yalnız **aynı ağ içinde**
raporlanır; ölçülen gecikme iki çağrının toplamıdır, tek seferlik bir gözlemdir.

Komut yalnız raporlar: **otomatik en-hızlı seçimi veya failover yoktur**.
`scan/filter/score/watch` Robinhood adaptörü, ayar adları alfabetik sıralandığında
`robinhood` önekiyle eşleşen **ilk** ucu kullanır (Solana için aşağıdaki havuza bakın).
Eşleşme yoksa Robinhood public mainnet, Solana public devnet kullanılır.
Örneğin `RPC_SOLANA_A_MAINNET`, `RPC_SOLANA_Z_BACKUP`'tan önce seçilir;
ikinci uç otomatik yedek değildir. `RPC_ALCHEMY` gibi genel adlar kontrol
komutunda ölçülür ama bu adaptörler tarafından seçilmez. Bir sağlayıcıyı
kullanmak için doğru ağa ait URL'yi `RPC_ROBINHOOD_ALCHEMY` veya
`RPC_SOLANA_HELIUS` gibi zincir önekli bir adla tanımlayın; seçimi değiştirmek
için eşleşen ayarları düzenleyin. Üretimde Solana devnet yerine mainnet
ucunu açıkça yapılandırın.

### Solana RPC havuzu ve kota sınırı

`RPC_SOLANA_<AD>=url` ile birden çok sağlayıcı tanımlanabilir; istemci hepsini bir havuz
olarak kullanır (ilk alfabetik uç artık tek seçim değildir). Ek ayarlar:

- `RPC_SOLANA_<AD>_PROFILE=helius|chainstack|drpc|alchemy|public` (verilmezse URL'den tahmin;
  tahmin edilemezse `public`). `_QUOTA`, `_RPS`, `_PERIOD_DAYS` profili ezer.
- `RPC_SOLANA_ORDER=ad1,ad2` seçim sırası (varsayılan: helius, chainstack, drpc, alchemy, public).
- `RPC_USAGE_DB` ortak kullanım sayacı (varsayılan `data/rpc_usage.sqlite`; izleme servisi ve
  tarama aynı dosyayı paylaşır).

Her kotalı sağlayıcı **kodla zorlanan sert sınırla** ücretsiz limitin en çok %99'unda kesilir.
Birim istekten ÖNCE atomik ayrılır; sayaç yazılamazsa istek gönderilmez (hata verilir). Dönem
ilk kullanımdan itibaren 30 gündür (takvim ayı değil; sağlayıcı sıfırlama anı doğrulanamadı).
429 gövdesinde "max usage"/kota mesajı varsa sağlayıcı dönem sonuna kadar tükenmiş sayılır.
Tüm kotalılar bitince kotasız `public` uca düşülür (log satırı); o da yoksa net hata verilir.
Varsayılan maliyetler: helius 1 kredi/istek, chainstack 2 RU, drpc 20 CU (doğrulanamadı,
varsayım), alchemy 40 CU (getAccountInfo 10, getSlot 20). Ücretli plana geçmeyin: sayaç yalnız
bizde tutulur, sağlayıcı kalan kotayı API'den vermez. Birden çok kotasız/bilinmeyen uç
tanımlarsanız hepsi `public` sayılır ve sırayla (kendi 0,9 sn aralığıyla) kullanılır.

Sağlayıcı alan adı tanınmıyorsa (helius, chainstack/p2pify, drpc, alchemy ve resmî Solana
public uçları dışında) `RPC_SOLANA_<AD>_PROFILE` açıkça verilmelidir; yoksa hata alınır
(sessizce kotasız sayılmaz). Bilerek kotasız kullanım için `_PROFILE=public` yazın.
`RPC_SOLANA_<AD>_PERIOD_START=YYYY-MM-DD`: panelden hesabın yenilenme gününü girin; dönem
bu günden itibaren `_PERIOD_DAYS` katlarına hizalanır (verilmezse ilk kullanımdan itibaren).
`RPC_USAGE_DB` varsayılanı proje köküne göre mutlak yoldur (`<kök>/data/rpc_usage.sqlite`).
Sayaç DB'si yeni oluşursa "sayaç sıfırdan başladı" uyarısı basılır: dosyayı yedeğe alın
(`deploy/backup.sh` ile birlikte) ve silmeyin; kaybolursa sağlayıcı panelinden kullanımı kontrol edin.

Kullanımı görmek için (ağ isteği yok; URL basılmaz): `uv run avimsin-rpc --usage`.
%80 ve %95'te log uyarısı basılır. `avimsin-rpc` ölçüm çağrıları da sayaca yazılır.

Solana istemcisi normal çağrılar ve tüm retry isteklerinde aynı asgari aralığı
uygular. 429 için 8 ek deneme; transport/502/503/504 için toplam 3 hata bütçesi
vardır. Hata türü değişince bütçeler sıfırlanmaz. Pacing paylaşılan kotanın hiç
dolmayacağını garanti etmez; tükenen bütçe açık hata üretir.

`getTransaction` legacy/v0/v1 için `maxSupportedTransactionVersion=1` ister
([resmî sözleşme](https://solana.com/developers/cookbook/transactions/versions)).
`tests/fixtures/solana_v1.json`, mainnet slot 447606860'tan gerçek JSON-parsed
yanıttır: aynı imza max=0 ile -32015, max=1 ile başarı vermiştir. Fixture testi
token hareketini çözümler; bu, bütün Solana geçmişinin eksiksiz tarandığı veya
gerçek ekonomik P&L hesaplandığı iddiası değildir.

## Geliştirme Akışı

`main` branch'i korunur; doğrudan push kapalıdır. Her değişiklik:

```bash
git switch -c feat/yeni-ozellik   # 1. feature branch
# ... geliştir, commit'le ...
git push -u origin feat/yeni-ozellik
gh pr create                      # Pull Request aç
gh pr merge --squash --delete-branch
```

## Lisans / License

[MIT](LICENSE) © haybinalim
