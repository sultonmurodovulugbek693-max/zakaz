# Ona tili test boti (Milliy sertifikat mock)

Telegram bot va Mini App: o'qituvchi test yaratadi, o'quvchilar onlayn ishlaydi yoki qog'oz titulni
to'ldiradi, o'qituvchi titullarni telefon kamerasida skanerlaydi. Natijalar Rasch bali va daraja bilan,
yozma javoblar va esseni AI tekshiradi.

## Milliy sertifikat mock — UZBMB rasmiy namunasi bo'yicha
- 45 ta topshiriq, 180 daqiqa
- 1–32 test (A–D), 33–35 moslashtirish (A–F), 36–39 yozma, 40–44 yozma (a, b), 45 esse
- Ballar namunadagidek: 1,1 / 1,7 / 2,5; 40a 1,2, 40b 1,3; 41–44 a 0,8, b 0,9; esse 24 → jami 100
- Rasch bali (T-shkala, 75 gacha) va daraja: A+ 70 · A 65 · B+ 60 · B 55 · C+ 50 · C 46 · NC
  (qatnashchilar natijasidan hisoblanadi — taxminiy, rasmiy emas)

## Imkoniyatlar
- **O'qituvchi**: test yaratadi, A–D variant kalitlarini kiritadi. Yozma savolga 3 tagacha to'g'ri javob
  (sinonim): `chiroyli | go'zal | ko'rkam`. Tinish belgili javoblar (37-savol) ham tekshiriladi.
- Ikki rejim: **onlayn** (o'quvchi ilovada belgilaydi) yoki **titul** (qog'ozda, kamerada skanerlanadi).
- **O'quvchi**: kod → variant → ism → javoblar. Ball darhol chiqadi, e'londan keyin natija kartasi.
- **Natijalar**: PDF va Excel — ball, Rasch bali, daraja, xato savollar, savollar tahlili.
- **Natijani tuzatish**: «👥 Natijalar»da o'quvchini bosing — javoblari, kalit bilan solishtiruv,
  AI o'qigan yozma kataklar rasmi. Tuzatilsa, ball va Rasch qayta hisoblanadi.
- **Majburiy obuna**: admin botda kanallar qo'shadi; testga alohida kanal ham biriktirsa bo'ladi.

## Titul va skaner
- Titul ko'rinishlari: **Standart** (A4, ism, ID, test va yozma javob kataklari) — Milliy sertifikat testida
  faqat shu. Oddiy testda yana: **Ixcham** (bitta A4 ga 2–3 ta), **Yirik doirachalar**, **Fan ustunli**.
- **ID** — 4 xonali: tepada 4 ta katak (raqam yoziladi), ostida 4 ta tik 0–9 ustun (bo'yaladi).
  Shaxsiy titulda oldindan yozilgan; bo'sh titulni printerda ko'paytirasiz, o'quvchi ID'sini o'zi to'ldiradi.
- Yozma javoblar (36–44) — **har harf alohida katakda** (bosh harflar). AI ularni aniq o'qiydi.
- **Esse (45) qog'ozda emas**: o'quvchi botga test kodini kiritadi va tituldagi ID'si bilan esseni
  telefonda yozadi. Titul skanerlanganda (oldin yoki keyin) esse shu ID'li natijaga o'zi qo'shiladi.
  Bitta Telegram akkaunt — bitta ID; baholangan esse o'zgartirilmaydi.
- **Oflayn skaner** (ilovada yoki Mini App'da):
  1. Titul ramkada qimirlamay tursa — telefonning o'zida o'qiladi va **telefon xotirasiga** saqlanadi
     (~1–2 soniya, internet shart emas). Keyingi titulni ustiga qo'yavering.
  2. Kamera ostidagi ro'yxatda har titulni ochib tekshirish/tuzatish mumkin (⚠️ — xira doirachalar).
  3. «📤 Botga yuborish» — hammasi bittada. Bot chatiga xabar keladi; yozma javoblarni AI fonda
     o'qiydi va tugagach yana xabar beradi.
  4. Skaner sahifasi bir marta internet bilan ochilgach, keyingi safar internetsiz ham ochiladi.

## Skaner ilovasi (APK)
- Flutter ilova repo ildizida (`lib/main.dart`). Ilova sayt sahifasini ochadi — sayt yangilansa,
  ilovani qayta o'rnatish shart emas. Faqat **sayt manzili o'zgarsa** APK qayta yig'iladi:
  `flutter build apk --release --target-platform android-arm,android-arm64 --dart-define=APP_URL=https://<sayt>.onrender.com/`
  so'ng `build/app/outputs/flutter-apk/app-release.apk` → `onatili_bot/downloads/onatili-skaner.apk`
  (bot uni `/download` manzilida beradi).
- Ilovaga Telegram orqali kiriladi (bot tasdiqlaydi). Mini App'da «Skanerlash» → ilova shu testda ochiladi.

## AI (yozma javoblar va esse)
- `AI_PROVIDER=gemini` — Google Gemini, **bepul tarifi bor**. Kalit: https://aistudio.google.com → API Keys.
  Google akkaunti egasi **18 yoshdan katta** bo'lishi shart.
- Asosiy model band (503) yoki limitda (429) bo'lsa — navbat bilan zaxira modellar (`GEMINI_FALLBACK`),
  har birining bepul limiti alohida.
- Yozma javoblar **8 tadan titul bitta so'rovda** o'qiladi (`WRITTEN_BATCH`): 30 titul ≈ 4 so'rov, ~1–2 daqiqa.
- Bepul limit taxminan kuniga bir necha yuz so'rov: 8 titul = 1 so'rov, 1 esse = 1 so'rov.
  Ko'proq kerak bo'lsa — AI Studio'da «Set up billing» (taxminan 1000 titul ≈ 1 $).
- `AI_PROVIDER=claude` — Anthropic Claude (pullik, sifati yuqoriroq).
- AI bahosi — taklif: esseni o'qituvchi tasdiqlaydi, yozma javoblarni «Natijalar»da tuzatsa bo'ladi.

## Xizmat ishlari (o'zi bajariladi)
- **Uyg'oq tutish**: Render'ning bepul tarifida server 15 daqiqa so'rovsiz qolsa uxlaydi va bot javob
  bermay qo'yadi. Bot har 10 daqiqada o'z manziliga murojaat qiladi (`KEEP_ALIVE=0` — o'chirish).
- **Kunlik zaxira nusxa**: har kuni 03:00 (Toshkent) baza nusxasi `ADMIN_IDS` dagi adminlarga fayl bo'lib
  keladi. Hozir olish: botga `/backup`. Tiklash (yangi bazaga): `python restore.py onatili_….json.gz`
- **Xatolar haqida xabar**: botda xato chiqsa yoki AI 1 soatdan beri ishlamasa — adminga Telegram'da
  xabar keladi (ko'pi bilan 5 daqiqada bir marta).
- Yozma kataklar rasmlari 45 kundan keyin o'zi o'chadi (natijalar qoladi).

## Testlar
Kod o'zgartirilgandan keyin hammasi joyidaligini tekshirish (internet va AI kerak emas):
```
cd tests
..\.venv\Scripts\python -m unittest
```

## Botni o'z hisoblariga ulash (yangi egasi uchun)
Barcha kalit va parollar **kodda emas**, faqat sozlamalarda (`.env` yoki Render → Environment).
Botni boshqa odamga topshirganda har birini **yangisiga almashtiring**:

| Sozlama | Qayerdan olinadi |
|---|---|
| `BOT_TOKEN` | Telegram @BotFather → yangi bot (yoki eski botni `/mybots` → Transfer ownership) |
| `ADMIN_IDS` | Yangi egasining Telegram ID'si (botga `/myid` yozing) — nusxa va xato xabarlari shunga keladi |
| `DATABASE_URL` | PostgreSQL (masalan Neon.tech, bepul) — natijalar shu yerda saqlanadi |
| `AI_PROVIDER`, `GEMINI_API_KEY` | O'z Google akkauntidan AI Studio kaliti |
| `WEBAPP_URL` | Render'da shart emas (o'zi aniqlaydi) |

Tartib:
1. GitHub'da repo'ni yangi egasiga o'tkazing (Settings → Transfer) yoki nusxa (fork) oching.
2. Render.com'da **Web Service** yarating: repo → Root directory `onatili_bot`,
   Build: `pip install -r requirements.txt`, Start: `python bot.py`.
3. Render → Environment'ga yuqoridagi sozlamalarni kiriting → Deploy.
4. Sayt manzili o'zgargan bo'lsa — APK'ni yangi `APP_URL` bilan qayta yig'ing (yuqorida).
5. Botga `/start` yozing — Mini App tugmasi o'zi o'rnatiladi.

Eski egasining kalitlarini (Gemini, bot tokeni) topshirgandan keyin o'chirib qo'ying.

## Kompyuterda ishga tushirish
```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env      # so'ng .env ichini to'ldiring
.venv\Scripts\python bot.py
```
