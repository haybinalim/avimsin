# Devir

Son güncelleme: 2026-11-20

## Açık işler

- [ ] Robinhood Chain coinleri (0x0bd7…, 0x90a7…) yeniden filter/score bekliyor — Solana işi bitince.
- [ ] Sıradaki Solana işi: kullanıcı belirleyecek.

Kasadaki 2026-11-16 notundan taşındı (o tarihten sonra kapanmış olabilir, doğrulanmadı):

- [ ] Solana coin adresleri kullanıcıdan alınıp scan/filter/score.
- [ ] Eski global kayıtlar taşınmadı; her coin için filter+score yeniden çalıştırılmalı (yoksa watcher sinyal vermez).
- [ ] Panel görsel/mobil düzeni doğrulanmadı; `docs/screenshots/dashboard.png` eski.
- [ ] Önerilen: gerçek swap + fiyat modeli (zarar koşullu dump, USD P&L).
- [ ] Açılışta cursor yokken ilk block_number hatası süreci düşürür.
- [ ] Sürekli RPC arızası için Telegram uyarısı yok.
- [ ] Telegram 1 saatten uzun kapalıysa bekleyen sinyal kaybolur.
- [ ] Gerçek Telegram/RPC ile uçtan uca deneme yapılmadı.

## Biten (2026-11-20)

- 10 eski worktree ve birleşmiş dalları temizlendi.
- PR #29 (bağımlılık güncellemesi, yalnız `uv.lock`) güncel main üzerinde 325 test geçti, birleştirildi.
- `.env.example`'a yanlışlıkla yazılan gerçek Telegram token'ı commit edilmeden geri alındı; token BotFather'da yenilendi.
- `.env.example`'a 5 izleme ayarı (`WATCH_SOLANA_MODE`, `WATCH_MAX_TX_PER_WALLET_POLL`, `WATCH_IGNORED_MINTS`, `WATCH_BOT_WINDOW_SECONDS`, `WATCH_BOT_MAX_INFLOWS`) `deploy/avimsin.env.example` ile aynı açıklamalarla eklendi. Not: ajan izin ayarı `.env*` dosyalarını okumayı/düzenlemeyi engelliyor; bu dosyaya ekleme kullanıcı komutuyla yapılır.
