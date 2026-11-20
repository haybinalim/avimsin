# Avımsın VPS dağıtımı

Oracle Free Tier (Ubuntu 22.04) üzerinde 7/24 `avimsin-watch` çalıştırma.
Kaynak kod değişikliği yok — bu klasör kurulum + servis dosyalarıdır.

## Hızlı kurulum (VPS'te)

```bash
git clone https://github.com/haybinalim/avimsin.git /tmp/avimsin-src
cd /tmp/avimsin-src
sudo ./deploy/install.sh                      # varsayılan: /opt/avimsin, solana
# ya da (EVM, Solana bitince):
sudo ./deploy/install.sh --app-dir /opt/avimsin --chain robinhood --user ubuntu
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

## Günlük otomatik keşif (timer)

`avimsin-pipeline.timer` her gün 04:00'te (`Persistent=true`, `RandomizedDelaySec=15m`)
`avimsin-pipeline.service`'i çalıştırır: önce `avimsin-discover`, sonra `avimsin-pipeline`.
`install.sh` bunu kurmaz; izleme servisi kurulduktan sonra elle (APP_DIR örnek `/opt/avimsin`;
`User=` izleme servisiyle aynı kullanıcı olmalı, örnekte `ubuntu`):

```bash
APP_DIR=/opt/avimsin
sed "s|APP_DIR|$APP_DIR|g" $APP_DIR/deploy/avimsin-pipeline.service.template \
  | sudo tee /etc/systemd/system/avimsin-pipeline.service >/dev/null
sudo sed -i "/^\[Service\]/a User=ubuntu" /etc/systemd/system/avimsin-pipeline.service
sudo cp $APP_DIR/deploy/avimsin-pipeline.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/avimsin-pipeline.service /etc/systemd/system/avimsin-pipeline.timer
sudo systemctl daemon-reload
sudo systemctl enable --now avimsin-pipeline.timer
systemctl list-timers avimsin-pipeline.timer     # sonraki çalışma
sudo systemctl start avimsin-pipeline.service    # elle tetikle (uzun sürer: coin başına 3 saate kadar)
journalctl -u avimsin-pipeline -e --no-pager
```

Eşikler ve sınırlar ortam dosyasında (`DISCOVERY_*`, `PIPELINE_COIN_TIMEOUT_SECONDS`;
bkz. `avimsin.env.example`). İzleme ve pipeline aynı SQLite dosyasına yazar: bağlantı
WAL + 30 sn `busy_timeout` ile açılır. Pipeline ve `avimsin-watch` aynı public RPC kovasını
paylaşır; tarama sürerken izleme yavaşlayabilir.

## Dosyalar

| Dosya | İş |
|---|---|
| `avimsin-watch.service.template` | systemd unit şablonu (`APP_DIR`/`CHAIN_NAME` install.sh ile dolar) |
| `avimsin-pipeline.service.template` | Günlük keşif + analiz servisi (`Type=oneshot`; `APP_DIR` yer tutucusu) |
| `avimsin-pipeline.timer` | Her gün 04:00 tetikleyici |
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

- SQLite tek dosya, WAL modunda: `backup.sh` venv'deki `avimsin-backup` (Python `Connection.backup`; sunucuda `sqlite3` komutu gerekmez) ile tutarlı kopya alır
  (düz `cp` WAL'daki son yazmaları kaçırır; elle yedeklerken `-wal`/`-shm` dosyalarını da
  unutmayın).
- Public RPC rate limit kovası paylaşımlı: VPS'te tek servis koşar; geliştirici
  makinesinden aynı uca paralel tarama yapmayın (PR #13-#16 dersi).
- Varsayılan zincir Solana: `.env`'de `RPC_SOLANA_MAINNET` zorunlu — devnet varsayılanı
  üretimde kullanılmaz.
