# Ona tili test boti (Milliy sertifikat mock)

Chittak (matemttest) uslubidagi Telegram bot, faqat Ona tili va adabiyot uchun.

## Milliy sertifikat mock — UZBMB rasmiy namunasi bo'yicha
- 45 ta topshiriq, 180 daqiqa
- 1–32 test (A–D), 33–35 moslashtirish (A–F), 36–39 yozma, 40–44 yozma (a, b), 45 esse
- Ballar namunadagidek: 1,1 / 1,7 / 2,5; 40a 1,2, 40b 1,3; 41–44 a 0,8, b 0,9; esse 24 → jami 100
- Rasch bali (T-shkala, 75 gacha) va daraja: A+ 70 · A 65 · B+ 60 · B 55 · C+ 50 · C 46 · NC
  (qatnashchilar natijasidan hisoblanadi — taxminiy, rasmiy emas)

## Imkoniyatlar
- **O'qituvchi**: test yaratadi, A–D variant kalitlarini kiritadi. Yozma savolga 3 tagacha
  to'g'ri javob (sinonim): `chiroyli | go'zal | ko'rkam`. Tinish belgili javoblar (37-savol) ham tekshiriladi.
- Bot test kodi va **kalit PDF** beradi. Ikki rejim: **onlayn** (o'quvchi ilovada belgilaydi) yoki **titul** — titullar PDF chop etiladi, o'qituvchi telefon kamerasida jonli skanerlaydi (titul topilishi bilan o'zi ushlaydi).
- **O'quvchi**: kod → variant → ism → javoblar (yoki `12345*abcd...`). Ball darhol chiqadi.
- **Natijalar PDF**: ball, Rasch bali, daraja, xato savollar. Esse bahosi 0–24 (0,5 qadam).
- Test yakunlanganda har bir qatnashchiga Rasch bali, daraja va to'g'ri javoblar yuboriladi.

## Titul ko'rinishlari (Chittak titullaridan)
- **Standart** — A4, ism, ID, test va yozma javob kataklari (yozmani AI o'qiydi, kalit bilan solishtiriladi)
- **Ixcham** — faqat doirachalar, bitta A4 ga 2–3 ta titul
- **EvalBee** — ism/imtihon/sana qutisi, 5 savollik bloklar
- **DTM** — EvalBee to'ri, ustunlar tepasida sarlavha
Yozma javoblar faqat standart titulda o'qiladi (AI_PROVIDER + kalit kerak).

## Skaner ilovasi (APK)
- Flutter ilova repo ildizida (`lib/main.dart`). Yig'ish:
  `flutter build apk --release --target-platform android-arm,android-arm64 --dart-define=APP_URL=https://zakaz-9s4l.onrender.com/`
  so'ng `build/app/outputs/flutter-apk/app-release.apk` → `onatili_bot/downloads/onatili-skaner.apk` (bot `/download` da beradi).
- Ilovada faqat skaner: Telegram orqali kirish (bot tasdiqlaydi), ochiq titulli testlar, natija botga ham yuboriladi.
- Mini App'da titul testida «Skanerlash» → «Skaner ilovasida ochish»: ilova shu testda, qayta kirishsiz ochiladi.

## Ishga tushirish
```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
# .env faylida BOT_TOKEN ni to'ldiring
.venv\Scripts\python bot.py
```
