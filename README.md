# Avımsın (avimsin) 🏹

**Smart wallet bulucu ve takipçi.** Bir coin'i erken alan cüzdanları bulur, bot
cüzdanlarını ve kağıt-elyaf (panik satan) cüzdanları eler, kalan **smart wallet**
listesini performansa göre sıralar ve belirlediğiniz eşikte toplu alım olduğunda
Telegram'dan haber verir.

> Repo adı `avimsin` — GitHub Türkçe karakter desteklemediği için; proje adı **Avımsın**.

## Vizyon

Örnek senaryo: popüler bir ağda (örn. Robinhood Chain) bir meme coin çok yükselir.
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
| 1 | Veri katmanı: erken alıcılar + transfer/swap geçmişi (Robinhood Chain, EVM RPC) | ✅ (erken alıcılar; swap geçmişi Faz 2'de) |
| 2 | Filtre motoru: bot tespiti, kaçak satış kuralları (büyüyebilir kural seti) | ⏳ |
| 3 | Skorlama & sıralama: kazanma oranı, P&L, işlem sıklığı | ⏳ |
| 4 | Streamlit paneli: smart wallet listesini tarayıcıda görüntüleme | ⏳ |
| 5 | Telegram bildirim botu: "≥ N smart wallet aynı coini aldı" eşik uyarısı | ⏳ |
| 6 | Oracle Free VPS'e 7/24 dağıtım (systemd) + Solana ve diğer EVM ağları | ⏳ |

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
uv run avimsin-rpc     # RPC uçlarını hız/doğruluk açısından yarıştırır
uv run avimsin-scan <token> --from-block <blok>   # erken alıcıları topla, SQLite'a yaz
```

## RPC Stratejisi

Verinin en doğru kaynağı zincirin kendisidir. Katmanlı yaklaşım:

1. **Public RPC** (ücretsiz, başlangıç) →
2. **Alchemy / QuickNode** ücretsiz API anahtarları (darboğazda devreye girer) →
3. `avimsin-rpc` komutu tüm uçları aynı anda ölçer; **en hızlısını** kullanır.
   Ağır saatlerde bir sağlayıcı yavaşlarsa diğerini seçebilirsiniz.

## Geliştirme Akışı

`main` branch'i korunur; doğrudan push kapalıdır. Her değişiklik:

```bash
git switch -c feat/yeni-ozellik   # 1. feature branch
# ... geliştir, commit'le ...
git push -u origin feat/yeni-ozellik
gh pr create                      # Pull Request aç
gh pr merge --squash --delete-branch
```
