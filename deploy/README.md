# Avımsın VPS dağıtımı

Oracle Free Tier (Ubuntu 22.04) üzerinde 7/24 `avimsin-watch` çalıştırma.
Kaynak kod değişikliği yok — bu klasör kurulum + servis dosyalarıdır.

## Hızlı kurulum (VPS'te)

```bash
git clone https://github.com/haybinalim/avimsin.git /tmp/avimsin-src
cd /tmp/avimsin-src
sudo ./deploy/install.sh                      # varsayılan: /opt/avimsin, robinhood
# ya da:
sudo ./deploy/install.sh --app-dir /opt/avimsin --chain solana --user ubuntu
```

Script şunları yapar (idempotent, tekrar koşulabilir):

1. `python3.12`, `sqlite3`, `uv` kurar (yoksa).
2. Kaynağı `APP_DIR`'a kopyalar, `uv sync --no-dev` ile venv kurar.
3. `.env` yoksa `deploy/avimsin.env.example`'dan oluşturur (`chmod 600`).
4. Template'den `/etc/systemd/system/avimsin-watch.service` üretir,
   `systemd-analyze verify` ile doğrular, `enable --now` ile başlatır.
5. Servisin ayakta olduğunu kontrol eder.

Sonrası:

```bash
sudo nano /opt/avimsin/.env        # TELEGRAM_* ve RPC_* doldur
sudo systemctl restart avimsin-watch
journalctl -u avimsin-watch -f     # canlı log
```

## Dosyalar

| Dosya | İş |
|---|---|
| `avimsin-watch.service.template` | systemd unit şablonu (`APP_DIR`/`CHAIN_NAME` install.sh ile dolar) |
| `avimsin.env.example` | Üretim `.env` örneği (repo kökündeki `.env.example`'dan türetildi; `RPC_SOLANA_MAINNET` yorumu dahil) |
| `install.sh` | Kurulum scripti (root ister, servis `User=` ile userspace'e düşer) |
| `update.sh` | Kodu günceller, venv'i tazeler, servisi restart eder |
| `backup.sh` | `data/avimsin.sqlite`'ı tarihli kopya olarak yedekler |

## Operasyon

```bash
sudo ./deploy/update.sh [--app-dir /opt/avimsin]   # git pull + uv sync + restart
./deploy/backup.sh [--app-dir /opt/avimsin]        # ~/avimsin-backup/*.sqlite
systemctl status avimsin-watch                     # durum
journalctl -u avimsin-watch -e --no-pager | tail -50
```

## Bilinen sınırlar

- SQLite tek dosya: `backup.sh` servisi durdurmadan kopya alır (SQLite sıcak
  yedeği okumada güvenlidir; yazma anında kopya tutarlı-olmayabilir — kritikse
  `systemctl stop` ile durdurup yedekleyin).
- Public RPC rate limit kovası paylaşımlı: VPS'te tek servis koşar; geliştirici
  makinesinden aynı uca paralel tarama yapmayın (PR #13-#16 dersi).
- Solana izlenecekse `.env`'de `RPC_SOLANA_MAINNET` zorunlu — devnet varsayılanı
  üretimde kullanılmaz.
