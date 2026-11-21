"""
Baza: testlar, variantlar (kalitlar), natijalar, esselar, oflayn o'quvchilar, jonli holat, kanallar.

Ikki rejim:
  • DATABASE_URL (postgresql://...) berilgan bo'lsa — PostgreSQL. Productionda SHU kerak:
    Render'da fayl tizimi vaqtinchalik, SQLite fayli har deployda o'chib ketadi.
  • aks holda — SQLite fayli (DB_PATH), kompyuterda sinash uchun.

Barcha so'rovlar "?" belgisi bilan yoziladi; PostgreSQL uchun avtomatik "%s" ga aylantiriladi.
"""

import json
import os
import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

from config import DB_PATH

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))

if POSTGRES:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    IntegrityError = (psycopg.errors.IntegrityError, sqlite3.IntegrityError)
else:
    IntegrityError = (sqlite3.IntegrityError,)

# Ustun turlari: Telegram ID'lar 32 bitga sig'maydi (BIGINT); kasr ball — aniq (DOUBLE PRECISION)
ID = "BIGSERIAL PRIMARY KEY" if POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
BIG = "BIGINT" if POSTGRES else "INTEGER"
FLOAT = "DOUBLE PRECISION" if POSTGRES else "REAL"

SCHEMA = [
    f"""CREATE TABLE IF NOT EXISTS users (
        tg_id          {BIG} PRIMARY KEY,
        full_name      TEXT,
        username       TEXT,
        last_name_used TEXT,
        created_at     TEXT
    )""",
    f"""CREATE TABLE IF NOT EXISTS tests (
        id          {ID},
        code        TEXT UNIQUE NOT NULL,
        owner_id    {BIG} NOT NULL,
        title       TEXT NOT NULL,
        fmt         TEXT NOT NULL,
        count       INTEGER NOT NULL DEFAULT 0,
        items       TEXT NOT NULL,
        closed      INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT,
        essay_topic TEXT,
        theme       TEXT,
        mode        TEXT NOT NULL DEFAULT 'online',
        channels    TEXT
    )""",
    f"""CREATE TABLE IF NOT EXISTS variants (
        test_id {BIG} NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
        name    TEXT NOT NULL,
        answers TEXT NOT NULL,
        PRIMARY KEY (test_id, name)
    )""",
    f"""CREATE TABLE IF NOT EXISTS attempts (
        id              {ID},
        test_id         {BIG} NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
        tg_id           {BIG} NOT NULL,
        student_name    TEXT NOT NULL,
        variant         TEXT NOT NULL,
        answers         TEXT NOT NULL,
        test_correct    INTEGER NOT NULL,
        written_correct INTEGER NOT NULL,
        auto_total      INTEGER NOT NULL,
        wrong           TEXT NOT NULL,
        points          {FLOAT} NOT NULL DEFAULT 0,
        essay           {FLOAT},
        created_at      TEXT,
        UNIQUE (test_id, tg_id)
    )""",
    # Esse (45-savol): matn yoki qo'lyozma rasmlari (Telegram file_id), AI bahosi.
    # status: none — esse yo'q; manual — AI o'chiq; pending — AI navbatida; graded — baholandi; failed — xato
    f"""CREATE TABLE IF NOT EXISTS essays (
        id         {ID},
        attempt_id {BIG} UNIQUE NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
        text       TEXT,
        photos     TEXT NOT NULL DEFAULT '[]',
        status     TEXT NOT NULL,
        ai_score   {FLOAT},
        ai_result  TEXT,
        provider   TEXT,
        cost       {FLOAT} NOT NULL DEFAULT 0,
        tries      INTEGER NOT NULL DEFAULT 0,
        error      TEXT,
        created_at TEXT,
        graded_at  TEXT
    )""",
    # Oflayn test o'quvchilari: raqam titul kodida bosiladi, skaner varaq kimniki ekanini shundan taniydi
    f"""CREATE TABLE IF NOT EXISTS students (
        id         {ID},
        test_id    {BIG} NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
        number     INTEGER NOT NULL,
        name       TEXT NOT NULL,
        variant    TEXT NOT NULL,
        attempt_id {BIG} REFERENCES attempts(id) ON DELETE SET NULL,
        UNIQUE (test_id, number)
    )""",
    # «O'quvchisiman» arizasi: o'quvchi tumanidagi kursni topib, ustozga ariza (ism, ixtiyoriy rasm) yuboradi;
    # ustoz tasdiqlasa — o'quvchisi bo'ladi (teacher_students)
    f"""CREATE TABLE IF NOT EXISTS join_requests (
        id         {ID},
        teacher_id {BIG} NOT NULL,
        student_id {BIG} NOT NULL,
        first_name TEXT NOT NULL,
        last_name  TEXT NOT NULL,
        photo      TEXT,
        mime       TEXT,
        status     TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT,
        decided_at TEXT,
        UNIQUE (teacher_id, student_id)
    )""",
    # Begona o'quvchining esse to'lovi (admin narxi): chekni AI tekshiradi — to'g'ri bo'lsa «paid», bo'lmasa admin ko'radi
    f"""CREATE TABLE IF NOT EXISTS essay_payments (
        id           {ID},
        test_id      {BIG} NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
        student_id   {BIG} NOT NULL,
        amount       INTEGER NOT NULL,
        status       TEXT NOT NULL DEFAULT 'open',
        receipt      TEXT,
        mime         TEXT,
        receipt_hash TEXT,
        txn          TEXT,
        note         TEXT,
        created_at   TEXT,
        decided_at   TEXT,
        UNIQUE (test_id, student_id)
    )""",
    # AI ishlatilishi (admin /ai): kun × tur (written/receipt/essay) × model — so'rovlar, limit xatolari, tokenlar, narx
    """CREATE TABLE IF NOT EXISTS ai_usage (
        day        TEXT NOT NULL,
        kind       TEXT NOT NULL,
        model      TEXT NOT NULL,
        calls      INTEGER NOT NULL DEFAULT 0,
        failures   INTEGER NOT NULL DEFAULT 0,
        limited    INTEGER NOT NULL DEFAULT 0,
        tokens_in  BIGINT NOT NULL DEFAULT 0,
        tokens_out BIGINT NOT NULL DEFAULT 0,
        cost       REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (day, kind, model)
    )""",
    # O'quvchi qog'ozga yozgan essesi rasmi (onlayn mock): AI o'qib matnga aylantiradi, o'quvchi tuzatib yuboradi
    f"""CREATE TABLE IF NOT EXISTS essay_scans (
        student_id {BIG} NOT NULL,
        test_id    {BIG} NOT NULL,
        status     TEXT NOT NULL DEFAULT 'collecting',
        ai         TEXT NOT NULL DEFAULT 'gemini',
        pages      TEXT,
        text       TEXT,
        model      TEXT,
        error      TEXT,
        updated_at TEXT,
        PRIMARY KEY (student_id, test_id)
    )""",
    # Eski: ustozga mock arizasi (endi ishlatilmaydi — ma'lumot saqlanib qoladi)
    f"""CREATE TABLE IF NOT EXISTS mock_requests (
        id         {ID},
        test_id    {BIG} NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
        student_id {BIG} NOT NULL,
        name       TEXT NOT NULL,
        phone      TEXT,
        receipt    TEXT,
        mime       TEXT,
        status     TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT,
        decided_at TEXT,
        UNIQUE (test_id, student_id)
    )""",
    # Ustozga ulangan o'quvchilar: ustoz kreditidan esse tekshirtiradi (test bo'lmasa ham — «✍️ Esse tekshirish»)
    f"""CREATE TABLE IF NOT EXISTS teacher_students (
        teacher_id {BIG} NOT NULL,
        student_id {BIG} NOT NULL,
        created_at TEXT,
        PRIMARY KEY (teacher_id, student_id)
    )""",
    # O'quvchining mustaqil essesi (mashq): AI 75 ballik shkalada tekshiradi, ustoz kreditidan
    f"""CREATE TABLE IF NOT EXISTS practice_essays (
        id         {ID},
        teacher_id {BIG} NOT NULL,
        student_id {BIG} NOT NULL,
        topic      TEXT,
        text       TEXT NOT NULL,
        status     TEXT NOT NULL DEFAULT 'pending',
        ai_score   {FLOAT},
        ai_result  TEXT,
        cost       {FLOAT} NOT NULL DEFAULT 0,
        tries      INTEGER NOT NULL DEFAULT 0,
        error      TEXT,
        created_at TEXT,
        graded_at  TEXT
    )""",
    # Ikki varaqli titul: 2-varaq (yozma javoblar) 1-varaqdan oldin skanerlansa — rasm o'quvchi ID'si bo'yicha
    # shu yerda kutadi; 1-varaq saqlanganda natijaga biriktiriladi
    f"""CREATE TABLE IF NOT EXISTS written_pending (
        test_id    {BIG} NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
        number     INTEGER NOT NULL,
        image      TEXT NOT NULL,
        mime       TEXT NOT NULL DEFAULT 'image/jpeg',
        created_at TEXT,
        PRIMARY KEY (test_id, number)
    )""",
    # Telefondan yuborilgan titullar: uid bo'yicha — tarmoq uzilib qayta yuborilsa ikkinchi natija ochilmaydi
    f"""CREATE TABLE IF NOT EXISTS scan_uploads (
        uid        TEXT PRIMARY KEY,
        test_id    {BIG} NOT NULL,
        student_id {BIG},
        response   TEXT,
        created    {FLOAT} NOT NULL
    )""",
    # Onlayn test jonli holati: kim ochgan, nechta belgilagan, oxirgi faolligi (unix soniya)
    f"""CREATE TABLE IF NOT EXISTS live (
        test_id    {BIG} NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
        tg_id      {BIG} NOT NULL,
        name       TEXT NOT NULL,
        answered   INTEGER NOT NULL DEFAULT 0,
        total      INTEGER NOT NULL DEFAULT 0,
        started_at {BIG} NOT NULL,
        seen_at    {BIG} NOT NULL,
        PRIMARY KEY (test_id, tg_id)
    )""",
    # Majburiy obuna kanallari (admin bot ichidan qo'shadi)
    f"""CREATE TABLE IF NOT EXISTS channels (
        chat_id  {BIG} PRIMARY KEY,
        title    TEXT NOT NULL,
        url      TEXT NOT NULL,
        added_by {BIG},
        added_at TEXT
    )""",
    # Oflayn skaner: titulning yozma javob kataklari rasmi (base64) — AI fonda o'qiydi va natijani qayta hisoblaydi.
    # status: pending — navbatda; done — o'qildi; failed — o'qib bo'lmadi. notified — o'qituvchiga xabar ketganmi
    f"""CREATE TABLE IF NOT EXISTS written_jobs (
        attempt_id {BIG} PRIMARY KEY REFERENCES attempts(id) ON DELETE CASCADE,
        test_id    {BIG} NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
        image      TEXT,
        mime       TEXT NOT NULL DEFAULT 'image/jpeg',
        status     TEXT NOT NULL DEFAULT 'pending',
        tries      INTEGER NOT NULL DEFAULT 0,
        error      TEXT,
        result     TEXT,
        notified   INTEGER NOT NULL DEFAULT 0,
        created_at TEXT
    )""",
    # Titulli (qog'oz) testning essesi: o'quvchi botda ID'si bilan yozadi. Titul skanerlanganda
    # shu ID'li o'quvchining natijasiga biriktiriladi (oldin ham, keyin ham yuborilishi mumkin)
    f"""CREATE TABLE IF NOT EXISTS offline_essays (
        test_id    {BIG} NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
        number     INTEGER NOT NULL,
        tg_id      {BIG} NOT NULL,
        name       TEXT,
        text       TEXT NOT NULL,
        created_at TEXT,
        PRIMARY KEY (test_id, number)
    )""",
    # O'qituvchining sinflari: o'quvchiga doimiy ID beriladi (o'qituvchining barcha sinflari bo'yicha takrorlanmaydi).
    # Titulli testga sinf qo'shilganda o'quvchilar shu ID'lar bilan test ro'yxatiga ko'chiriladi
    f"""CREATE TABLE IF NOT EXISTS classes (
        id         {ID},
        owner_id   {BIG} NOT NULL,
        name       TEXT NOT NULL,
        created_at TEXT
    )""",
    f"""CREATE TABLE IF NOT EXISTS class_students (
        id       {ID},
        class_id {BIG} NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
        owner_id {BIG} NOT NULL,
        number   INTEGER NOT NULL,
        name     TEXT NOT NULL,
        UNIQUE (owner_id, number)
    )""",
    # Har o'qituvchiga berilgan oxirgi ID — o'chirilgan o'quvchining ID'si boshqasiga qayta berilmasin
    f"""CREATE TABLE IF NOT EXISTS id_counters (
        owner_id {BIG} PRIMARY KEY,
        last     INTEGER NOT NULL
    )""",
    # O'qituvchi obunasi: tarif va tugash sanasi (admin beradi; birinchi testda — bepul sinov)
    f"""CREATE TABLE IF NOT EXISTS subscriptions (
        tg_id      {BIG} PRIMARY KEY,
        plan       TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        updated_at TEXT,
        reminded   TEXT
    )""",
    # Oylik AI sarfi (o'qituvchi bo'yicha): esse va yozma javoblar (titul). Limit shu yerda tekshiriladi
    f"""CREATE TABLE IF NOT EXISTS usage (
        tg_id    {BIG} NOT NULL,
        month    TEXT NOT NULL,
        essays   INTEGER NOT NULL DEFAULT 0,
        written  INTEGER NOT NULL DEFAULT 0,
        notified INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (tg_id, month)
    )""",
    # Ilovaga (APK) Telegram orqali kirish: kind = login (bot tasdiqlashini kutadi) | session (kirilgan)
    f"""CREATE TABLE IF NOT EXISTS app_tokens (
        token   TEXT PRIMARY KEY,
        kind    TEXT NOT NULL,
        tg_id   {BIG},
        session TEXT,
        created {FLOAT} NOT NULL
    )""",
    # Qo'shimcha esse paketi (sotib olingan, muddatsiz): oylik AI limiti tugaganda shundan olinadi
    f"""CREATE TABLE IF NOT EXISTS credits (
        tg_id      {BIG} PRIMARY KEY,
        essays     INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT
    )""",
    # Admin bot orqali o'zgartiradigan sozlamalar: narxlar, chegirmalar, karta raqami, sinov kunlari
    """CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    # Obuna to'lovi: o'quvchi/o'qituvchi kartaga o'tkazadi, chekni botga yuboradi, admin tasdiqlaydi.
    # status: pending — chek kutilmoqda; sent — chek yuborilgan, admin ko'rmoqda; paid — tasdiqlangan; rejected — rad etilgan
    f"""CREATE TABLE IF NOT EXISTS payments (
        id         {ID},
        tg_id      {BIG} NOT NULL,
        period     TEXT NOT NULL,
        amount     {BIG} NOT NULL,
        status     TEXT NOT NULL DEFAULT 'pending',
        receipt    TEXT,
        created_at TEXT,
        decided_at TEXT,
        admin_id   {BIG}
    )""",
    # Oflayn «Ona tili» kursiga ro'yxatdan o'tish: o'quvchi ilovada ism, telefon, yosh va manzilini
    # qoldiradi — ariza adminga boradi, keyin ular bevosita (botdan tashqari) muloqot qiladi.
    # Bitta foydalanuvchi — bitta ariza (qaytadan yuborsa, eskisi yangilanadi)
    f"""CREATE TABLE IF NOT EXISTS registrations (
        id         {ID},
        tg_id      {BIG} UNIQUE NOT NULL,
        role       TEXT NOT NULL DEFAULT 'student',
        first_name TEXT,
        last_name  TEXT,
        full_name  TEXT NOT NULL,
        phone      TEXT NOT NULL,
        age        INTEGER,
        region     TEXT,
        district   TEXT,
        address    TEXT,
        created_at TEXT,
        updated_at TEXT
    )""",
    # O'qituvchi dars beradigan joylar («joy ochadi»): bir nechta tuman bo'lishi mumkin.
    # O'quvchi shu tumanni tanlasa — arizasi o'sha tumandagi o'qituvchilarga boradi
    f"""CREATE TABLE IF NOT EXISTS teacher_places (
        id         {ID},
        tg_id      {BIG} NOT NULL,
        region     TEXT NOT NULL,
        district   TEXT NOT NULL,
        created_at TEXT,
        UNIQUE (tg_id, region, district)
    )""",
    # Skanerlangan titul surati (Telegram'da — file_id) va doirachalar joyi: natijada yashil / qizil belgilar bilan
    f"""CREATE TABLE IF NOT EXISTS attempt_sheets (
        attempt_id {BIG} PRIMARY KEY REFERENCES attempts(id) ON DELETE CASCADE,
        file_id    TEXT,
        marks      TEXT,
        created_at TEXT
    )""",
    # O'quvchi tarifi (FREE_IDS ustoz sotadi): muddat tugaguncha shu ustozning hamma mocklari bepul (o'z o'quvchisidek)
    f"""CREATE TABLE IF NOT EXISTS student_passes (
        teacher_id {BIG} NOT NULL,
        student_id {BIG} NOT NULL,
        expires_at TEXT NOT NULL,
        PRIMARY KEY (teacher_id, student_id)
    )""",
    # Tarif to'lovi: karta → chek botga → AI (to'g'ri — paid, shubhali — review, ustoz qaror qiladi)
    f"""CREATE TABLE IF NOT EXISTS pass_payments (
        id           {ID},
        teacher_id   {BIG} NOT NULL,
        student_id   {BIG} NOT NULL,
        days         INTEGER NOT NULL,
        amount       INTEGER NOT NULL,
        status       TEXT NOT NULL DEFAULT 'open',
        receipt      TEXT,
        mime         TEXT,
        receipt_hash TEXT,
        txn          TEXT,
        note         TEXT,
        created_at   TEXT,
        decided_at   TEXT
    )""",
    # «Bugungi mock»: o'quvchi «Qatnashaman» bosdi — mock ochilganda bot eslatadi, kartada «N kishi qatnashmoqchi»
    f"""CREATE TABLE IF NOT EXISTS mock_signups (
        test_id    {BIG} NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
        tg_id      {BIG} NOT NULL,
        name       TEXT,
        created_at TEXT,
        PRIMARY KEY (test_id, tg_id)
    )""",
    # Mock savollari: PDF va rasmlar (Telegram'da saqlanadi — file_id); o'quvchi PDF yoki rasmni tanlab ko'radi
    f"""CREATE TABLE IF NOT EXISTS test_files (
        id         {ID},
        test_id    {BIG} NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
        kind       TEXT NOT NULL,
        file_id    TEXT NOT NULL,
        mime       TEXT,
        name       TEXT,
        size       INTEGER NOT NULL DEFAULT 0,
        ord        INTEGER NOT NULL DEFAULT 0,
        created_at TEXT
    )""",
    # Begona o'quvchining esse krediti — har ustozda alohida (pul o'sha ustoz kartasiga tushgan): 1 kredit = 1 esse
    f"""CREATE TABLE IF NOT EXISTS essay_wallets (
        teacher_id {BIG} NOT NULL,
        student_id {BIG} NOT NULL,
        balance    INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT,
        PRIMARY KEY (teacher_id, student_id)
    )""",
    # Kredit paketi to'lovi: o'quvchi paket tanlaydi → ustoz kartasiga o'tkazadi → chekni botga yuboradi →
    # AI tekshiradi (to'g'ri — paid, shubhali — review, ustoz qaror qiladi). status: open | review | paid | rejected
    f"""CREATE TABLE IF NOT EXISTS wallet_payments (
        id           {ID},
        teacher_id   {BIG} NOT NULL,
        student_id   {BIG} NOT NULL,
        essays       INTEGER NOT NULL,
        amount       INTEGER NOT NULL,
        status       TEXT NOT NULL DEFAULT 'open',
        receipt      TEXT,
        mime         TEXT,
        receipt_hash TEXT,
        txn          TEXT,
        note         TEXT,
        created_at   TEXT,
        decided_at   TEXT
    )""",
    # Begona o'qituvchining daromadi: begona o'quvchi to'lovi platforma kartasiga tushgan, ulushi (share) shu yerda.
    # source: essay (essay_payments) | wallet (wallet_payments). To'lov bekor qilinsa — yozuv o'chadi
    f"""CREATE TABLE IF NOT EXISTS teacher_earnings (
        id          {ID},
        teacher_id  {BIG} NOT NULL,
        source      TEXT NOT NULL,
        payment_id  {BIG} NOT NULL,
        student_id  {BIG},
        amount      INTEGER NOT NULL,
        share       INTEGER NOT NULL,
        created_at  TEXT,
        UNIQUE (source, payment_id)
    )""",
    # «💸 Yechib olish»: o'qituvchi so'raydi → admin kartasiga o'tkazadi → «paid». status: pending | paid | rejected
    f"""CREATE TABLE IF NOT EXISTS withdrawals (
        id          {ID},
        teacher_id  {BIG} NOT NULL,
        amount      INTEGER NOT NULL,
        card        TEXT,
        holder      TEXT,
        status      TEXT NOT NULL DEFAULT 'pending',
        created_at  TEXT,
        decided_at  TEXT,
        decided_by  {BIG}
    )""",
]

# Eski bazalar uchun keyin qo'shilgan ustunlar
MIGRATIONS = [
    ("practice_essays", "kind", "TEXT NOT NULL DEFAULT 'student'"),   # student — ustoz o'quvchisi, public — begona
    ("practice_essays", "photos", "TEXT"),                             # [[file_id, mime], ...] — rasm yoki PDF esse
    ("practice_essays", "price", "INTEGER NOT NULL DEFAULT 0"),        # begona to'lagan narx (0 — bepul)
    ("practice_essays", "receipt", "TEXT"),                            # chek (Telegram file_id)
    ("attempts", "points", f"{FLOAT} NOT NULL DEFAULT 0"),
    ("tests", "essay_topic", "TEXT"),
    ("tests", "theme", "TEXT"),
    ("tests", "mode", "TEXT NOT NULL DEFAULT 'online'"),
    ("tests", "channels", "TEXT"),
    ("tests", "sheet_style", "TEXT"),  # titul ko'rinishi: standard | compact | large | columns
    ("users", "role", "TEXT"),  # oxirgi tanlangan ekran: student | teacher
    ("registrations", "first_name", "TEXT"),  # ism va familiya alohida so'raladi
    ("registrations", "last_name", "TEXT"),
    ("registrations", "role", "TEXT NOT NULL DEFAULT 'student'"),  # o'quvchi | o'qituvchi
    ("registrations", "region", "TEXT"),  # manzil endi ro'yxatdan tanlanadi (places.py)
    ("registrations", "district", "TEXT"),
    ("payments", "essays", "INTEGER"),  # esse paketi (period = 'pack'): nechta esse — buyurtma paytidagi hajm
    ("payments", "receipt_hash", "TEXT"),  # chek rasmi izi — bitta chek ikki to'lovga ishlatilmasin
    ("payments", "txn", "TEXT"),  # chekdagi tranzaksiya raqami (AI o'qigan)
    ("payments", "note", "TEXT"),  # AI xulosasi: nima uchun avtomatik tasdiqlanmadi
    ("attempts", "notified", "INTEGER NOT NULL DEFAULT 0"),  # e'londa natija xabari yuborilgan (qayta e'londa takrorlanmaydi)
    ("tests", "sheets_sent", "INTEGER NOT NULL DEFAULT 0"),  # titul PDF'i botga yuborilgan (1-qadam bajarilgan)
    ("tests", "announcing", "INTEGER NOT NULL DEFAULT 0"),
    ("tests", "price", "INTEGER NOT NULL DEFAULT 0"),
    ("tests", "check_mode", "TEXT NOT NULL DEFAULT 'free'"),  # oflayn yozma javoblar: free — Gemini/qo'lda, paid — Claude (kreditdan)  # begona o'quvchi shu mock essesini tekshirish uchun to'laydi (so'm)  # e'lon xabarlari ketmoqda — server qayta ishga tushsa davom etadi
    ("offline_essays", "verified", "INTEGER NOT NULL DEFAULT 0"),  # tituldagi esse kodi bilan yuborilgan
    ("practice_essays", "wallet", "INTEGER NOT NULL DEFAULT 0"),  # begonaning esse krediti bilan (tekshirilmasa — qaytadi)
    # «Bugungi mock» jadvali (Toshkent vaqti, «YYYY-MM-DD HH:MM»): ochiladi → kirish to'xtaydi → yopiladi
    ("tests", "opens_at", "TEXT"),
    ("tests", "entry_until", "TEXT"),
    ("tests", "closes_at", "TEXT"),
    ("tests", "public_mock", "INTEGER NOT NULL DEFAULT 0"),  # hamma o'quvchi bosh sahifasida (FREE_IDS / admin)
    ("tests", "mock_no", "INTEGER"),                         # «Bugungi mock №26»
    ("tests", "mock_state", "TEXT NOT NULL DEFAULT ''"),     # '' → soon (30 daq qoldi) → opened → done (natija va sertifikat)
    # Jadvalli mockda price — «🤖 AI tekshiradi» narxi, price_teacher — «👩‍🏫 Ustoz tekshiradi» narxi (0 — bepul)
    ("tests", "price_teacher", "INTEGER NOT NULL DEFAULT 0"),
    ("essay_payments", "choice", "TEXT"),
    ("student_passes", "stage", "INTEGER NOT NULL DEFAULT 0"),
    # Esse uchun yechilgan kredit: wallet — o'quvchining ustozdagi krediti, teacher — ustozning AI krediti.
    # AI tekshira olmasa (failed) — shu kredit qaytariladi va belgi o'chadi (ikki marta qaytmasin)
    ("essays", "charge", "TEXT"),
    # Pul kimga tushadi: platform — platforma kartasiga (begona o'qituvchi, ulushi teacher_earnings'da), NULL — ustozga
    ("essay_payments", "payee", "TEXT"),
    ("wallet_payments", "payee", "TEXT"),
    ("tests", "channel_posts", "TEXT NOT NULL DEFAULT ''"),  # kanalga yuborilgan e'lonlar: new,day,hour,open,entry,results  # eslatma: 0 — yo'q, 1 — «1 kun qoldi», 2 — «tugadi»  # mock oldidan o'quvchi tanlovi: ai | teacher (essesini kim tekshiradi)
]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------- Ulanish ----------

class _Connection:
    """sqlite3 va psycopg uchun bitta ko'rinish: execute("... ? ...", params) -> qatorlar dict ko'rinishida."""

    def __init__(self, raw, postgres: bool):
        self.raw, self.postgres = raw, postgres

    def execute(self, sql: str, params: tuple | list = ()):
        if self.postgres:
            sql = sql.replace("?", "%s")
        return self.raw.execute(sql, params)


_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ConnectionPool(
                DATABASE_URL, min_size=1, max_size=8, open=True, timeout=30,
                # prepare_threshold=None — tayyorlangan so'rovlar keshlanmaydi: deployda jadvalga ustun qo'shilsa,
                # eski ulanishlar «cached plan must not change result type» xatosini bermaydi (Neon pooler ham shuni xohlaydi)
                kwargs={"row_factory": dict_row, "autocommit": False, "prepare_threshold": None},
                check=ConnectionPool.check_connection,  # uzilib qolgan ulanish qayta ochiladi
            )
        return _pool


@contextmanager
def connect():
    """Tranzaksiya: muvaffaqiyatli bo'lsa saqlanadi, xato bo'lsa bekor qilinadi."""
    if POSTGRES:
        with _get_pool().connection() as raw:  # chiqishda commit / xatoda rollback
            yield _Connection(raw, True)
        return
    raw = sqlite3.connect(DB_PATH)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    try:
        yield _Connection(raw, False)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def _row(row) -> dict | None:
    return dict(row) if row is not None else None


def init() -> None:
    _forget_settings()  # yangi (yoki tiklangan) baza — xotiradagi sozlamalar eskirgan
    with connect() as conn:
        for statement in SCHEMA:
            conn.execute(statement)
        for table, column, ddl in MIGRATIONS:
            if POSTGRES:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}")
            else:
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if column not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        # Titul ko'rinishlarining oldingi nomlari → hozirgilari
        conn.execute("UPDATE tests SET sheet_style = 'large' WHERE sheet_style = 'evalbee'")
        conn.execute("UPDATE tests SET sheet_style = 'columns' WHERE sheet_style = 'dtm'")


def ping() -> None:
    """Bazaga eng oddiy so'rov — /health?db=1 tarmoq kechikishini o'lchaydi."""
    with connect() as conn:
        conn.execute("SELECT 1").fetchone()


def backend_name() -> str:
    return "PostgreSQL" if POSTGRES else f"SQLite ({DB_PATH})"


# ---------- Foydalanuvchilar ----------

def remember_user(tg_id: int, full_name: str, username: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (tg_id, full_name, username, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET full_name = excluded.full_name, username = excluded.username",
            (tg_id, full_name, username, _now()),
        )


def get_role(tg_id: int) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT role FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
    return row["role"] if row else None


def set_role(tg_id: int, role: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE users SET role = ? WHERE tg_id = ?", (role, tg_id))


def create_handoff(token: str, tg_id: int, session: str) -> None:
    """Mini App'dan APK'ga o'tish: allaqachon tasdiqlangan kirish (Telegram imzosi bilan kelgan foydalanuvchi)."""
    create_login(token)
    confirm_login(token, tg_id, session)


def last_name_used(tg_id: int) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT last_name_used FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
    return row["last_name_used"] if row else None


def set_last_name_used(tg_id: int, name: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE users SET last_name_used = ? WHERE tg_id = ?", (name, tg_id))


# ---------- Ilovaga kirish (APK) ----------

LOGIN_TTL = 10 * 60          # kirish havolasi 10 daqiqa amal qiladi
SESSION_TTL = 180 * 24 * 3600  # ilovadagi sessiya ~6 oy


def create_login(token: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM app_tokens WHERE kind = 'login' AND created < ?", (time.time() - LOGIN_TTL,))
        conn.execute("INSERT INTO app_tokens (token, kind, created) VALUES (?, 'login', ?)", (token, time.time()))


def login_pending(token: str) -> bool:
    """Kirish havolasi hali tasdiqlanmagan va eskirmagan."""
    with connect() as conn:
        row = conn.execute("SELECT created, tg_id FROM app_tokens WHERE token = ? AND kind = 'login'",
                           (token,)).fetchone()
    return bool(row) and row["tg_id"] is None and time.time() - row["created"] <= LOGIN_TTL


def cancel_login(token: str) -> None:
    """«Yo'q, men emas» — tasdiqlanmagan kirish havolasi o'chiriladi."""
    with connect() as conn:
        conn.execute("DELETE FROM app_tokens WHERE token = ? AND kind = 'login' AND tg_id IS NULL", (token,))


def delete_user_sessions(tg_id: int) -> int:
    """/chiqish — foydalanuvchining barcha ilova sessiyalari bekor qilinadi. Qaytaradi: nechta."""
    with connect() as conn:
        return conn.execute("DELETE FROM app_tokens WHERE kind = 'session' AND tg_id = ?", (tg_id,)).rowcount or 0


def confirm_login(token: str, tg_id: int, session: str) -> bool:
    """Bot /start app_<token> ni oldi: kirish tasdiqlanadi va sessiya yaratiladi."""
    with connect() as conn:
        row = conn.execute("SELECT created, tg_id FROM app_tokens WHERE token = ? AND kind = 'login'",
                           (token,)).fetchone()
        if not row or row["tg_id"] is not None or time.time() - row["created"] > LOGIN_TTL:
            return False
        if not conn.execute("UPDATE app_tokens SET tg_id = ?, session = ? WHERE token = ? AND tg_id IS NULL",
                            (tg_id, session, token)).rowcount:  # ikki marta bosilsa — bitta sessiya
            return False
        conn.execute("INSERT INTO app_tokens (token, kind, tg_id, created) VALUES (?, 'session', ?, ?)",
                     (session, tg_id, time.time()))
    return True


def take_login(token: str) -> str | None:
    """Ilova kutayotgan kirish: tasdiqlangan bo'lsa sessiya tokeni (bir marta beriladi)."""
    with connect() as conn:
        row = conn.execute("SELECT session FROM app_tokens WHERE token = ? AND kind = 'login'", (token,)).fetchone()
        if row and row["session"]:
            conn.execute("DELETE FROM app_tokens WHERE token = ?", (token,))
            return row["session"]
    return None


def session_user(session: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT s.tg_id, s.created, u.full_name, u.username FROM app_tokens s "
            "LEFT JOIN users u ON u.tg_id = s.tg_id WHERE s.token = ? AND s.kind = 'session'", (session,)).fetchone()
    if not row or time.time() - row["created"] > SESSION_TTL:
        return None
    return {"id": row["tg_id"], "name": row["full_name"] or "Foydalanuvchi", "username": row["username"]}


def delete_session(session: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM app_tokens WHERE token = ? AND kind = 'session'", (session,))


# ---------- Testlar ----------

def _test_dict(row) -> dict | None:
    if row is None:
        return None
    test = dict(row)
    test["items"] = json.loads(test["items"])
    test["channels"] = json.loads(test.get("channels") or "[]")
    return test


def create_test(owner_id: int, title: str, fmt: str, count: int, items: list[dict],
                keys: dict[str, dict[str, str]], essay_topic: str | None = None) -> dict:
    with connect() as conn:
        for _ in range(1000):  # 90 000 ta kod: band bo'lsa qayta tanlanadi, hammasi band bo'lsa cheksiz aylanmaydi
            code = str(random.randint(10000, 99999))
            if not conn.execute("SELECT 1 FROM tests WHERE code = ?", (code,)).fetchone():
                break
        else:
            raise RuntimeError("Bo'sh test kodi topilmadi")
        test_id = conn.execute(
            "INSERT INTO tests (code, owner_id, title, fmt, count, items, created_at, essay_topic) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (code, owner_id, title, fmt, count, json.dumps(items, ensure_ascii=False), _now(), essay_topic),
        ).fetchone()["id"]
        for name, answers in keys.items():
            conn.execute(
                "INSERT INTO variants (test_id, name, answers) VALUES (?, ?, ?)",
                (test_id, name, json.dumps(answers, ensure_ascii=False)),
            )
    return get_test(test_id)


def get_test(test_id: int) -> dict | None:
    with connect() as conn:
        return _test_dict(conn.execute("SELECT * FROM tests WHERE id = ?", (test_id,)).fetchone())


def get_test_by_code(code: str) -> dict | None:
    with connect() as conn:
        return _test_dict(conn.execute("SELECT * FROM tests WHERE code = ?", (code,)).fetchone())


def list_tests(owner_id: int, limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT t.*, (SELECT COUNT(*) FROM attempts a WHERE a.test_id = t.id) AS students "
            "FROM tests t WHERE owner_id = ? ORDER BY id DESC LIMIT ?",
            (owner_id, limit),
        ).fetchall()
    return [_test_dict(row) for row in rows]


def set_closed(test_id: int, closed: bool) -> None:
    with connect() as conn:
        conn.execute("UPDATE tests SET closed = ? WHERE id = ?", (int(closed), test_id))


def delete_test(test_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM tests WHERE id = ?", (test_id,))


def set_keys(test_id: int, keys: dict[str, dict[str, str]]) -> None:
    """O'qituvchi kalitni tuzatdi — variantlar kalitlari almashtiriladi."""
    with connect() as conn:
        for name, answers in keys.items():
            conn.execute("UPDATE variants SET answers = ? WHERE test_id = ? AND name = ?",
                         (json.dumps(answers, ensure_ascii=False), test_id, name))


def get_keys(test_id: int) -> dict[str, dict[str, str]]:
    """Variantlar kalitlari: {"A": {"1": "B", ...}, "B": {...}}."""
    with connect() as conn:
        rows = conn.execute("SELECT name, answers FROM variants WHERE test_id = ? ORDER BY name", (test_id,)).fetchall()
    return {row["name"]: json.loads(row["answers"]) for row in rows}


# ---------- Natijalar ----------

def _attempt_dict(row) -> dict | None:
    if row is None:
        return None
    attempt = dict(row)
    attempt["answers"] = json.loads(attempt["answers"])
    attempt["wrong"] = json.loads(attempt["wrong"])
    return attempt


def save_attempt(test_id: int, tg_id: int, student_name: str, variant: str, answers: dict, result: dict) -> dict:
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO attempts (test_id, tg_id, student_name, variant, answers, test_correct, written_correct, "
            "auto_total, wrong, points, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
            (test_id, tg_id, student_name, variant, json.dumps(answers, ensure_ascii=False),
             result["test_correct"], result["written_correct"], result["auto_total"],
             json.dumps(result["wrong"]), result["points"], _now()),
        ).fetchone()
    return _attempt_dict(row)


def update_attempt_result(attempt_id: int, answers: dict, result: dict, variant: str | None = None) -> None:
    """Javoblar o'zgardi (AI yozma javoblarni o'qidi yoki o'qituvchi tuzatdi) — natija qayta hisoblanib saqlanadi."""
    with connect() as conn:
        conn.execute(
            "UPDATE attempts SET answers = ?, test_correct = ?, written_correct = ?, auto_total = ?, wrong = ?, "
            "points = ?, variant = COALESCE(?, variant) WHERE id = ?",
            (json.dumps(answers, ensure_ascii=False), result["test_correct"], result["written_correct"],
             result["auto_total"], json.dumps(result["wrong"]), result["points"], variant, attempt_id),
        )


def get_attempt(attempt_id: int) -> dict | None:
    with connect() as conn:
        return _attempt_dict(conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone())


def find_attempt(test_id: int, tg_id: int) -> dict | None:
    with connect() as conn:
        return _attempt_dict(
            conn.execute("SELECT * FROM attempts WHERE test_id = ? AND tg_id = ?", (test_id, tg_id)).fetchone()
        )


def mark_sheets_sent(test_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE tests SET sheets_sent = 1 WHERE id = ?", (test_id,))


def set_announcing(test_id: int, value: bool) -> None:
    with connect() as conn:
        conn.execute("UPDATE tests SET announcing = ? WHERE id = ?", (1 if value else 0, test_id))


def announcing_tests() -> list[dict]:
    """E'loni tugamay qolgan testlar (server qayta ishga tushgan) — davom ettiriladi."""
    with connect() as conn:
        return [_test_dict(r) for r in conn.execute("SELECT * FROM tests WHERE announcing = 1").fetchall()]


UPLOAD_KEEP = 14 * 24 * 3600


def get_upload(uid: str, test_id: int) -> dict | None:
    """Telefondagi titul avval yuborilganmi: {student_id, response (yoki None — yarmida uzilgan)}."""
    with connect() as conn:
        row = conn.execute("SELECT student_id, response FROM scan_uploads WHERE uid = ? AND test_id = ?",
                           (uid, test_id)).fetchone()
    if not row:
        return None
    return {"student_id": row["student_id"], "response": json.loads(row["response"]) if row["response"] else None}


def save_upload(uid: str, test_id: int, student_id: int, response: dict | None = None) -> None:
    with connect() as conn:
        if random.random() < 0.02:  # eskilarini vaqti-vaqti bilan tozalash
            conn.execute("DELETE FROM scan_uploads WHERE created < ?", (time.time() - UPLOAD_KEEP,))
        conn.execute(
            "INSERT INTO scan_uploads (uid, test_id, student_id, response, created) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(uid) DO UPDATE SET student_id = excluded.student_id, response = excluded.response",
            (uid, test_id, student_id, json.dumps(response, ensure_ascii=False) if response else None, time.time()))


def mark_notified(attempt_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE attempts SET notified = 1 WHERE id = ?", (attempt_id,))


def owner_totals(owner_id: int) -> dict[int, list[float]]:
    """{test_id: [har qatnashchining jami bali]} — o'qituvchining barcha testlari bitta so'rovda (bosh sahifa uchun)."""
    with connect() as conn:
        rows = conn.execute("SELECT a.test_id, a.points, a.essay FROM attempts a JOIN tests t ON t.id = a.test_id "
                            "WHERE t.owner_id = ?", (owner_id,)).fetchall()
    out: dict[int, list[float]] = {}
    for row in rows:
        out.setdefault(row["test_id"], []).append(round((row["points"] or 0) + (row["essay"] or 0), 1))
    return out


def attempts_by_test(owner_id: int) -> dict[int, list[dict]]:
    """{test_id: [natijalar]} — o'qituvchining barcha testlari bitta so'rovda (har test uchun alohida so'rov
    masofadagi bazada «📊 Natijalar»ni sekinlashtirardi)."""
    with connect() as conn:
        rows = conn.execute("SELECT a.* FROM attempts a JOIN tests t ON t.id = a.test_id WHERE t.owner_id = ? "
                            "ORDER BY a.id", (owner_id,)).fetchall()
    out: dict[int, list[dict]] = {}
    for row in rows:
        attempt = _attempt_dict(row)
        out.setdefault(attempt["test_id"], []).append(attempt)
    return out


def list_attempts(test_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM attempts WHERE test_id = ? ORDER BY id", (test_id,)).fetchall()
    return [_attempt_dict(row) for row in rows]


# ---------- Jonli kuzatuv ----------

def live_ping(test_id: int, tg_id: int, name: str, answered: int, total: int) -> None:
    now = int(time.time())
    with connect() as conn:
        conn.execute(
            "INSERT INTO live (test_id, tg_id, name, answered, total, started_at, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(test_id, tg_id) DO UPDATE SET name = excluded.name, answered = excluded.answered, "
            "total = excluded.total, seen_at = excluded.seen_at",
            (test_id, tg_id, name, answered, total, now, now),
        )


def live_list(test_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM live WHERE test_id = ? ORDER BY started_at", (test_id,)).fetchall()
    return [dict(row) for row in rows]


def set_essay(attempt_id: int, score: float) -> None:
    with connect() as conn:
        conn.execute("UPDATE attempts SET essay = ? WHERE id = ?", (score, attempt_id))


# ---------- Esselar ----------

def _essay_dict(row) -> dict | None:
    if row is None:
        return None
    essay = dict(row)
    essay["photos"] = json.loads(essay["photos"] or "[]")
    essay["ai_result"] = json.loads(essay["ai_result"]) if essay["ai_result"] else None
    return essay


def create_essay(attempt_id: int, text: str | None, photos: list[str], status: str, charge: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO essays (attempt_id, text, photos, status, created_at, charge) VALUES (?, ?, ?, ?, ?, ?)",
            (attempt_id, text, json.dumps(photos), status, _now(), charge),
        )


def set_essay_charge(attempt_id: int, charge: str | None) -> None:
    """Shu esse uchun yechilgan kredit (wallet | teacher | None) — AI tekshira olmasa qaytariladi."""
    with connect() as conn:
        conn.execute("UPDATE essays SET charge = ? WHERE attempt_id = ?", (charge, attempt_id))


def get_essay(attempt_id: int) -> dict | None:
    with connect() as conn:
        return _essay_dict(conn.execute("SELECT * FROM essays WHERE attempt_id = ?", (attempt_id,)).fetchone())


def essays_for_test(test_id: int) -> dict[int, dict]:
    """{attempt_id: esse} — testning barcha esselari."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT e.* FROM essays e JOIN attempts a ON a.id = e.attempt_id WHERE a.test_id = ?", (test_id,)
        ).fetchall()
    return {row["attempt_id"]: _essay_dict(row) for row in rows}


def pending_essays(limit: int = 5) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT e.*, t.essay_topic AS topic, t.id AS test_id FROM essays e "
            "JOIN attempts a ON a.id = e.attempt_id JOIN tests t ON t.id = a.test_id "
            "WHERE e.status = 'pending' ORDER BY e.id LIMIT ?",
            (limit,),
        ).fetchall()
    return [_essay_dict(row) | {"topic": row["topic"], "test_id": row["test_id"]} for row in rows]


def save_essay_result(attempt_id: int, result: dict) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE essays SET status = 'graded', ai_score = ?, ai_result = ?, provider = ?, cost = cost + ?, "
            "graded_at = ?, error = NULL WHERE attempt_id = ?",
            (result["score"], json.dumps(result, ensure_ascii=False), result.get("provider"),
             result.get("cost", 0), _now(), attempt_id),
        )


def essay_failed(attempt_id: int, error: str, give_up: bool) -> str | None:
    """Urinish yozildi. give_up — esse «failed» bo'ladi va qaytariladigan kredit (charge) qaytariladi:
    belgi shu yerda o'chadi, keyingi «qayta urinish»dan keyin yana failed bo'lsa — ikkinchi marta qaytmaydi."""
    with connect() as conn:
        charge = None
        if give_up:
            row = conn.execute("SELECT charge FROM essays WHERE attempt_id = ?", (attempt_id,)).fetchone()
            charge = row["charge"] if row else None
        conn.execute(
            "UPDATE essays SET tries = tries + 1, error = ?, "
            "status = CASE WHEN ? = 1 THEN 'failed' ELSE status END, "
            "charge = CASE WHEN ? = 1 THEN NULL ELSE charge END WHERE attempt_id = ?",
            (error[:500], int(give_up), int(give_up), attempt_id),
        )
    return charge


def restore_essay(attempt_id: int, essay: dict | None, score: float | None) -> None:
    """Titul qayta skanerlandi (natija yangi qatorga): eski esse — matni, rasmlari, AI bahosi va o'qituvchi
    bahosi — yangi natijaga o'tadi. Esse qayta tekshirilmaydi (kredit ikkinchi marta yechilmaydi)."""
    with connect() as conn:
        if essay:
            conn.execute("DELETE FROM essays WHERE attempt_id = ?", (attempt_id,))
            conn.execute(
                "INSERT INTO essays (attempt_id, text, photos, status, ai_score, ai_result, provider, cost, tries, error, "
                "created_at, graded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (attempt_id, essay["text"], json.dumps(essay["photos"] or []), essay["status"], essay["ai_score"],
                 json.dumps(essay["ai_result"], ensure_ascii=False) if essay["ai_result"] else None, essay["provider"],
                 essay["cost"] or 0, essay["tries"] or 0, essay["error"], essay["created_at"], essay["graded_at"]),
            )
        if score is not None:
            conn.execute("UPDATE attempts SET essay = ? WHERE id = ?", (score, attempt_id))


def add_essay_cost(attempt_id: int, cost: float) -> None:
    with connect() as conn:
        conn.execute("UPDATE essays SET cost = cost + ? WHERE attempt_id = ?", (cost, attempt_id))


def create_written_job(attempt_id: int, test_id: int, image_b64: str, mime: str, status: str = "pending") -> None:
    """Titulning yozma kataklari rasmi. status: pending — AI o'qiydi; manual — o'qituvchi rasmga qarab belgilaydi
    («✍️ Yozmalarni tekshirish»). Qo'lda tekshiriladigan rasm «AI o'qidi» xabarini chiqarmaydi (notified = 1)."""
    with connect() as conn:
        conn.execute("DELETE FROM written_jobs WHERE attempt_id = ?", (attempt_id,))
        conn.execute(
            "INSERT INTO written_jobs (attempt_id, test_id, image, mime, status, notified, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (attempt_id, test_id, image_b64, mime, status, 1 if status == "manual" else 0, _now()),
        )


# ---------- Ustoz — o'quvchi ulanishi va mustaqil esse ----------

def link_student(teacher_id: int, student_id: int) -> bool:
    """Ulash. Qaytaradi: yangi ulandimi (avval ulangan bo'lsa — False)."""
    with connect() as conn:
        return bool(conn.execute(
            "INSERT INTO teacher_students (teacher_id, student_id, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(teacher_id, student_id) DO NOTHING", (teacher_id, student_id, _now())).rowcount)


def unlink_student(teacher_id: int, student_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM teacher_students WHERE teacher_id = ? AND student_id = ?", (teacher_id, student_id))


def is_linked(teacher_id: int, student_id: int) -> bool:
    with connect() as conn:
        return bool(conn.execute("SELECT 1 FROM teacher_students WHERE teacher_id = ? AND student_id = ?",
                                 (teacher_id, student_id)).fetchone())


def linked_students(teacher_id: int, month: str) -> list[dict]:
    """Ustozning o'quvchilari: ism, username va shu oyda nechta esse tekshirtirgani."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT l.student_id, l.created_at, u.full_name, u.username, "
            "(SELECT COUNT(*) FROM practice_essays p WHERE p.teacher_id = l.teacher_id AND p.student_id = l.student_id "
            " AND p.created_at LIKE ? AND p.status <> 'failed') AS used "
            "FROM teacher_students l LEFT JOIN users u ON u.tg_id = l.student_id WHERE l.teacher_id = ? "
            "ORDER BY l.created_at DESC", (f"{month}%", teacher_id)).fetchall()
    return [dict(row) for row in rows]


def teachers_of(student_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT l.teacher_id, u.full_name, u.username FROM teacher_students l LEFT JOIN users u ON u.tg_id = l.teacher_id "
            "WHERE l.student_id = ? ORDER BY l.created_at", (student_id,)).fetchall()
    return [dict(row) for row in rows]


def practice_used(teacher_id: int, student_id: int, month: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM practice_essays WHERE teacher_id = ? AND student_id = ? AND created_at LIKE ? "
            "AND status <> 'failed'", (teacher_id, student_id, f"{month}%")).fetchone()["n"]


def create_practice(teacher_id: int, student_id: int, topic: str | None, text: str, kind: str = "student",
                    photos: list | None = None, status: str = "pending", price: int = 0) -> int:
    with connect() as conn:
        return conn.execute(
            "INSERT INTO practice_essays (teacher_id, student_id, topic, text, status, created_at, kind, photos, price) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (teacher_id, student_id, topic, text, status, _now(), kind,
             json.dumps(photos) if photos else None, price)).fetchone()["id"]


def _essay_text(text: str | None, ai_result: str | None) -> str:
    """Esse matni: yozilgani yoki (rasm/PDF bo'lsa) AI o'qib chiqqani."""
    if text and text.strip():
        return text.strip()
    try:
        return str((json.loads(ai_result) if ai_result else {}).get("transcription") or "").strip()
    except (TypeError, ValueError):
        return ""


def top_essays(min_raw: float, limit: int = 60) -> list[dict]:
    """Eng yaxshi esselar (butun bot bo'yicha): mustaqil (p) va mock (m) esselari, balli yuqoridan.
    min_raw — 24 ballik shkalada (75 ballikda 66 → 19,5)."""
    with connect() as conn:
        practice = conn.execute(
            "SELECT p.id, p.ai_score AS raw, p.topic, p.text, p.ai_result, u.full_name AS name, p.graded_at AS at "
            "FROM practice_essays p LEFT JOIN users u ON u.tg_id = p.student_id "
            "WHERE p.status = 'graded' AND p.ai_score >= ?", (min_raw,)).fetchall()
        mock = conn.execute(
            "SELECT e.id, COALESCE(a.essay, e.ai_score) AS raw, t.essay_topic AS topic, e.text, e.ai_result, "
            "a.student_name AS name, e.graded_at AS at FROM essays e JOIN attempts a ON a.id = e.attempt_id "
            "JOIN tests t ON t.id = a.test_id WHERE (e.status = 'graded' OR a.essay IS NOT NULL) "
            "AND COALESCE(a.essay, e.ai_score) >= ?", (min_raw,)).fetchall()
    rows = [{"src": "p", **dict(r)} for r in practice] + [{"src": "m", **dict(r)} for r in mock]
    out = []
    for row in rows:
        row["text"] = _essay_text(row["text"], row.pop("ai_result"))
        if row["text"]:
            out.append(row)
    out.sort(key=lambda r: (-(r["raw"] or 0), r["at"] or ""))
    return out[:limit]


def public_free_used(teacher_id: int, student_id: int) -> int:
    """Begona o'quvchi shu ustozda nechta bepul esse tekshirtirgan (muvaffaqiyatsizlari hisobga kirmaydi)."""
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM practice_essays WHERE teacher_id = ? AND student_id = ? AND kind = 'public' "
            "AND price = 0 AND wallet = 0 AND status NOT IN ('failed', 'awaiting_credit')", (teacher_id, student_id)).fetchone()["n"]


def set_practice_status(practice_id: int, status: str, receipt: str | None = None) -> None:
    with connect() as conn:
        conn.execute("UPDATE practice_essays SET status = ?, receipt = COALESCE(?, receipt) WHERE id = ?",
                     (status, receipt, practice_id))


def practice_awaiting_pay(student_id: int) -> dict | None:
    """O'quvchining to'lovi kutilayotgan oxirgi essesi (chekni keyinroq yuborsa ham topiladi)."""
    with connect() as conn:
        return _practice(conn.execute(
            "SELECT * FROM practice_essays WHERE student_id = ? AND status = 'awaiting_pay' ORDER BY id DESC LIMIT 1",
            (student_id,)).fetchone())


def _practice(row) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["ai_result"] = json.loads(item["ai_result"]) if item["ai_result"] else None
    item["photos"] = json.loads(item["photos"]) if item.get("photos") else []
    return item


def get_practice(practice_id: int) -> dict | None:
    with connect() as conn:
        return _practice(conn.execute("SELECT * FROM practice_essays WHERE id = ?", (practice_id,)).fetchone())


def practice_of_student(student_id: int, limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM practice_essays WHERE student_id = ? ORDER BY id DESC LIMIT ?",
                            (student_id, limit)).fetchall()
    return [_practice(row) for row in rows]


def pending_practice(limit: int = 5) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM practice_essays WHERE status = 'pending' ORDER BY id LIMIT ?", (limit,)).fetchall()
    return [_practice(row) for row in rows]


def save_practice_result(practice_id: int, result: dict) -> None:
    with connect() as conn:
        conn.execute("UPDATE practice_essays SET status = 'graded', ai_score = ?, ai_result = ?, cost = cost + ?, "
                     "graded_at = ?, error = NULL WHERE id = ?",
                     (result["score"], json.dumps(result, ensure_ascii=False), result.get("cost", 0), _now(), practice_id))


def practice_failed(practice_id: int, error: str, give_up: bool) -> None:
    with connect() as conn:
        conn.execute("UPDATE practice_essays SET tries = tries + 1, error = ?, status = ? WHERE id = ?",
                     (error[:300], "failed" if give_up else "pending", practice_id))


# ---------- «O'quvchisiman» arizalari va login-parollar ----------

def save_join_request(teacher_id: int, student_id: int, first_name: str, last_name: str,
                      photo: str | None, mime: str | None) -> int | None:
    """Ariza (qayta yuborilsa — yangilanadi va yana «kutilmoqda» bo'ladi; ulanganlik webapp'da tekshiriladi)."""
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO join_requests (teacher_id, student_id, first_name, last_name, photo, mime, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?) ON CONFLICT(teacher_id, student_id) DO UPDATE SET "
            "first_name = excluded.first_name, last_name = excluded.last_name, photo = excluded.photo, mime = excluded.mime, "
            "status = 'pending', created_at = excluded.created_at, decided_at = NULL "
            "RETURNING id",
            (teacher_id, student_id, first_name, last_name, photo, mime, _now())).fetchone()
    return row["id"] if row else None


def get_join_request(request_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM join_requests WHERE id = ?", (request_id,)).fetchone())


def join_status(teacher_id: int, student_id: int) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT status FROM join_requests WHERE teacher_id = ? AND student_id = ?",
                           (teacher_id, student_id)).fetchone()
    return row["status"] if row else None


def join_requests_for(teacher_id: int, limit: int = 100) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT j.id, j.student_id, j.first_name, j.last_name, j.status, j.created_at, j.photo IS NOT NULL AS has_photo, "
            "u.username FROM join_requests j LEFT JOIN users u ON u.tg_id = j.student_id WHERE j.teacher_id = ? "
            "ORDER BY CASE j.status WHEN 'pending' THEN 0 ELSE 1 END, j.id DESC LIMIT ?", (teacher_id, limit)).fetchall()
    return [{**dict(row), "has_photo": bool(row["has_photo"])} for row in rows]


def decide_join_request(request_id: int, status: str) -> bool:
    with connect() as conn:
        return bool(conn.execute("UPDATE join_requests SET status = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
                                 (status, _now(), request_id)).rowcount)


# ---------- O'quvchi tarifi: muddatli — ustozning hamma mocklari bepul ----------

def pass_until(teacher_id: int, student_id: int) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT expires_at FROM student_passes WHERE teacher_id = ? AND student_id = ?",
                           (teacher_id, student_id)).fetchone()
    return row["expires_at"] if row else None


def _extend_pass(conn, teacher_id: int, student_id: int, days: int, now_at: str) -> str:
    """Muddat qo'shiladi: amaldagisi bo'lsa — uning oxiridan, tugagan bo'lsa — hozirdan. now_at: «YYYY-MM-DD HH:MM»."""
    row = conn.execute("SELECT expires_at FROM student_passes WHERE teacher_id = ? AND student_id = ?",
                       (teacher_id, student_id)).fetchone()
    start = max(row["expires_at"], now_at) if row else now_at
    until = (datetime.strptime(start[:16], "%Y-%m-%d %H:%M") + timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    conn.execute("INSERT INTO student_passes (teacher_id, student_id, expires_at, stage) VALUES (?, ?, ?, 0) "
                 "ON CONFLICT(teacher_id, student_id) DO UPDATE SET expires_at = excluded.expires_at, stage = 0",
                 (teacher_id, student_id, until))  # uzaytirildi — eslatmalar qaytadan
    return until


def last_pass_days(teacher_id: int, student_id: int) -> int:
    """Oxirgi sotib olingan tarif necha kunlik (ilovadagi halqa: qolgan kun / jami)."""
    with connect() as conn:
        row = conn.execute("SELECT days FROM pass_payments WHERE teacher_id = ? AND student_id = ? AND status = 'paid' "
                           "ORDER BY id DESC LIMIT 1", (teacher_id, student_id)).fetchone()
    return row["days"] if row else 0


def passes_to_remind(soon_at: str, now_at: str) -> list[dict]:
    """Eslatma vaqti kelgan tariflar: 1 kun qolgan (stage 0) yoki tugagan (stage < 2)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT p.teacher_id, p.student_id, p.expires_at, p.stage, u.full_name AS teacher_name FROM student_passes p "
            "LEFT JOIN users u ON u.tg_id = p.teacher_id "
            "WHERE (p.stage = 0 AND p.expires_at <= ? AND p.expires_at > ?) OR (p.stage < 2 AND p.expires_at <= ?)",
            (soon_at, now_at, now_at)).fetchall()
    return [dict(row) for row in rows]


def set_pass_stage(teacher_id: int, student_id: int, stage: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE student_passes SET stage = ? WHERE teacher_id = ? AND student_id = ?", (stage, teacher_id, student_id))


_PASS_COLS = "id, teacher_id, student_id, days, amount, status, txn, note, created_at, decided_at"


def start_pass_payment(teacher_id: int, student_id: int, days: int, amount: int) -> dict:
    """Tarif tanlandi: shu ustozdagi ochiq (chek yuborilmagan) to'lov almashtiriladi; chek tekshirilayotgani — qaytadi."""
    with connect() as conn:
        busy = _row(conn.execute(f"SELECT {_PASS_COLS} FROM pass_payments WHERE teacher_id = ? AND student_id = ? "
                                 "AND status = 'review' ORDER BY id DESC LIMIT 1", (teacher_id, student_id)).fetchone())
        if busy:
            return busy
        conn.execute("DELETE FROM pass_payments WHERE teacher_id = ? AND student_id = ? AND status IN ('open', 'rejected') "
                     "AND receipt IS NULL", (teacher_id, student_id))
        conn.execute("UPDATE pass_payments SET status = 'closed' WHERE teacher_id = ? AND student_id = ? "
                     "AND status IN ('open', 'rejected')", (teacher_id, student_id))
        return _row(conn.execute(
            f"INSERT INTO pass_payments (teacher_id, student_id, days, amount, status, created_at) VALUES (?, ?, ?, ?, 'open', ?) "
            f"RETURNING {_PASS_COLS}", (teacher_id, student_id, days, amount, _now())).fetchone())


def open_pass_payment(student_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute(f"SELECT {_PASS_COLS} FROM pass_payments WHERE student_id = ? "
                                 "AND status IN ('open', 'rejected', 'review') ORDER BY id DESC LIMIT 1", (student_id,)).fetchone())


def get_pass_payment(payment_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute(
            "SELECT p.*, u.full_name, u.username FROM pass_payments p LEFT JOIN users u ON u.tg_id = p.student_id "
            "WHERE p.id = ?", (payment_id,)).fetchone())


def save_pass_receipt(payment_id: int, receipt: str, mime: str, receipt_hash: str, txn: str | None, note: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE pass_payments SET receipt = ?, mime = ?, receipt_hash = ?, txn = ?, note = ?, status = 'review' "
                     "WHERE id = ?", (receipt, mime, receipt_hash, txn, note, payment_id))


def pay_pass_payment(payment_id: int, allowed: tuple[str, ...], now_at: str) -> str | None:
    """Tasdiqlandi: «paid» va tarif muddati uzayadi — bitta tranzaksiyada. Qaytaradi — yangi muddat (yoki None)."""
    marks = ", ".join("?" * len(allowed))
    with connect() as conn:
        pay = _row(conn.execute(f"SELECT * FROM pass_payments WHERE id = ? AND status IN ({marks})",
                                (payment_id, *allowed)).fetchone())
        if not pay or not conn.execute(f"UPDATE pass_payments SET status = 'paid', decided_at = ? WHERE id = ? "
                                       f"AND status IN ({marks})", (_now(), payment_id, *allowed)).rowcount:
            return None
        return _extend_pass(conn, pay["teacher_id"], pay["student_id"], pay["days"], now_at)


def reject_pass_payment(payment_id: int, allowed: tuple[str, ...]) -> bool:
    """Rad etish; AI tasdiqlagani bekor qilinsa — berilgan kunlar qaytarib olinadi."""
    marks = ", ".join("?" * len(allowed))
    with connect() as conn:
        pay = _row(conn.execute(f"SELECT * FROM pass_payments WHERE id = ? AND status IN ({marks})",
                                (payment_id, *allowed)).fetchone())
        if not pay or not conn.execute(f"UPDATE pass_payments SET status = 'rejected', decided_at = ? WHERE id = ? "
                                       f"AND status IN ({marks})", (_now(), payment_id, *allowed)).rowcount:
            return False
        if pay["status"] == "paid":
            row = conn.execute("SELECT expires_at FROM student_passes WHERE teacher_id = ? AND student_id = ?",
                               (pay["teacher_id"], pay["student_id"])).fetchone()
            if row:
                back = datetime.strptime(row["expires_at"][:16], "%Y-%m-%d %H:%M") - timedelta(days=pay["days"])
                conn.execute("UPDATE student_passes SET expires_at = ? WHERE teacher_id = ? AND student_id = ?",
                             (back.strftime("%Y-%m-%d %H:%M"), pay["teacher_id"], pay["student_id"]))
        return True


# ---------- «Bugungi mock»: jadval, qatnashuvchilar, savol fayllari ----------

def set_schedule(test_id: int, opens_at: str | None, entry_until: str | None, closes_at: str | None, public: bool) -> dict:
    """Jadval (None — jadvalsiz oddiy test). Hammaga ko'rinadigan mockka birinchi marta raqam beriladi (№26)."""
    with connect() as conn:
        conn.execute("UPDATE tests SET opens_at = ?, entry_until = ?, closes_at = ?, public_mock = ?, mock_state = '' "
                     "WHERE id = ?", (opens_at, entry_until, closes_at, 1 if public else 0, test_id))
        if public:
            conn.execute("UPDATE tests SET mock_no = (SELECT COALESCE(MAX(mock_no), 0) + 1 FROM tests) "
                         "WHERE id = ? AND mock_no IS NULL", (test_id,))
        return dict(conn.execute("SELECT id, opens_at, entry_until, closes_at, public_mock, mock_no FROM tests WHERE id = ?",
                                 (test_id,)).fetchone())


def public_mocks(since: str, now: str, limit: int = 3) -> list[dict]:
    """Bosh sahifadagi mocklar: yopilishi `since` dan keyin. Avval ochiq va bo'lajaklari (eng yaqini birinchi), keyin
    yaqinda yopilganlari (eng oxirgisi birinchi) — yopilganlar yangi mockni ro'yxatdan siqib chiqarmasin."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT t.id, t.code, t.title, t.owner_id, t.price, t.closed, t.opens_at, t.entry_until, t.closes_at, t.mock_no, "
            "t.public_mock, t.price_teacher, "
            "t.mock_state, u.full_name AS owner_name, (SELECT COUNT(*) FROM mock_signups s WHERE s.test_id = t.id) AS signups "
            "FROM tests t LEFT JOIN users u ON u.tg_id = t.owner_id "
            "WHERE t.public_mock = 1 AND t.closes_at >= ? "
            "ORDER BY CASE WHEN t.closes_at >= ? THEN 0 ELSE 1 END, CASE WHEN t.closes_at >= ? THEN t.opens_at END, "
            "t.closes_at DESC LIMIT ?", (since, now, now, limit)).fetchall()
    return [dict(row) for row in rows]


def mock_signup(test_id: int, tg_id: int, name: str | None, on: bool) -> None:
    with connect() as conn:
        if on:
            conn.execute("INSERT INTO mock_signups (test_id, tg_id, name, created_at) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(test_id, tg_id) DO NOTHING", (test_id, tg_id, name, _now()))
        else:
            conn.execute("DELETE FROM mock_signups WHERE test_id = ? AND tg_id = ?", (test_id, tg_id))


def mock_signups(test_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT s.tg_id, COALESCE(u.full_name, s.name) AS name FROM mock_signups s "
                            "LEFT JOIN users u ON u.tg_id = s.tg_id WHERE s.test_id = ? ORDER BY s.created_at DESC",
                            (test_id,)).fetchall()
    return [dict(row) for row in rows]


def signed_mocks(tg_id: int) -> set[int]:
    with connect() as conn:
        return {row["test_id"] for row in conn.execute("SELECT test_id FROM mock_signups WHERE tg_id = ?", (tg_id,)).fetchall()}


def mocks_to_remind(soon_at: str, now_at: str) -> list[dict]:
    """Eslatma vaqti kelgan mocklar: 30 daqiqa qolgan (state '') yoki ochilgan (state '' / 'soon')."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tests WHERE opens_at IS NOT NULL AND closed = 0 AND closes_at > ? AND "
            "((mock_state = '' AND opens_at <= ?) OR (mock_state IN ('', 'soon') AND opens_at <= ?))",
            (now_at, soon_at, now_at)).fetchall()
    return [dict(row) for row in rows]


def mocks_to_finish(before: str) -> list[dict]:
    """Yopilganiga vaqt o'tgan, natijasi hali e'lon qilinmagan mocklar."""
    with connect() as conn:
        rows = conn.execute("SELECT id FROM tests WHERE closes_at IS NOT NULL AND closes_at <= ? AND mock_state <> 'done'",
                            (before,)).fetchall()
    return [get_test(row["id"]) for row in rows]


def had_mock(owner_id: int, student_id: int, except_test: int) -> bool:
    """O'quvchi shu ustozning boshqa vaqtli mockida qatnashganmi yoki joy olganmi («birinchi mock bepul» uchun)."""
    with connect() as conn:
        attempt = conn.execute("SELECT 1 FROM attempts a JOIN tests t ON t.id = a.test_id WHERE t.owner_id = ? AND t.opens_at IS NOT NULL "
                               "AND a.tg_id = ? AND t.id <> ? LIMIT 1", (owner_id, student_id, except_test)).fetchone()
        paid = conn.execute("SELECT 1 FROM essay_payments p JOIN tests t ON t.id = p.test_id WHERE t.owner_id = ? "
                            "AND t.opens_at IS NOT NULL AND p.student_id = ? AND p.status = 'paid' AND t.id <> ? LIMIT 1",
                            (owner_id, student_id, except_test)).fetchone()
    return bool(attempt or paid)


def mocks_to_warn_entry(now_at: str, warn_at: str) -> list[dict]:
    """Kirish yopilishiga 15 daqiqa qolgan ochiq mocklar (hali kirmaganlarga eslatma)."""
    with connect() as conn:
        rows = conn.execute("SELECT * FROM tests WHERE opens_at IS NOT NULL AND closed = 0 AND mock_state = 'opened' "
                            "AND entry_until <= ? AND entry_until > ?", (warn_at, now_at)).fetchall()
    return [dict(row) for row in rows]


def channel_mocks(since: str) -> list[dict]:
    """Kanal e'lonlari uchun: hammaga ko'rinadigan mocklar (yopilganiga 2 kun bo'lmagan)."""
    with connect() as conn:
        rows = conn.execute("SELECT * FROM tests WHERE public_mock = 1 AND closes_at >= ?", (since,)).fetchall()
    return [dict(row) for row in rows]


def set_channel_posts(test_id: int, posts: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE tests SET channel_posts = ? WHERE id = ?", (posts, test_id))


def set_mock_state(test_id: int, state: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE tests SET mock_state = ? WHERE id = ?", (state, test_id))


def set_mock_prices(test_id: int, price_ai: int, price_teacher: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE tests SET price = ?, price_teacher = ? WHERE id = ?", (price_ai, price_teacher, test_id))


def unrated_teacher_essays(test_id: int) -> int:
    """«Ustoz tekshirsin» tanlangan, hali baholanmagan esselar (natija shular baholangach chiqadi)."""
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM essays e JOIN attempts a ON a.id = e.attempt_id "
                            "WHERE a.test_id = ? AND e.status = 'manual' AND a.essay IS NULL", (test_id,)).fetchone()["n"]


def pending_essays_of_test(test_id: int) -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM essays e JOIN attempts a ON a.id = e.attempt_id "
                            "WHERE a.test_id = ? AND e.status = 'pending'", (test_id,)).fetchone()["n"]


def live_started(test_id: int, tg_id: int) -> int | None:
    """O'quvchi mockni qachon ochgan (unix vaqt) — kirish vaqti o'tgach ham davom ettira oladi."""
    with connect() as conn:
        row = conn.execute("SELECT started_at FROM live WHERE test_id = ? AND tg_id = ?", (test_id, tg_id)).fetchone()
    return row["started_at"] if row else None


def add_test_file(test_id: int, kind: str, file_id: str, mime: str, name: str, size: int) -> dict:
    with connect() as conn:
        order = conn.execute("SELECT COALESCE(MAX(ord), 0) + 1 AS n FROM test_files WHERE test_id = ?", (test_id,)).fetchone()["n"]
        return dict(conn.execute(
            "INSERT INTO test_files (test_id, kind, file_id, mime, name, size, ord, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "RETURNING id, kind, name, size, ord", (test_id, kind, file_id, mime, name, size, order, _now())).fetchone())


def test_files(test_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT id, kind, file_id, mime, name, size, ord FROM test_files WHERE test_id = ? ORDER BY ord, id",
                            (test_id,)).fetchall()
    return [dict(row) for row in rows]


def delete_test_file(test_id: int, file_id: int) -> bool:
    with connect() as conn:
        return bool(conn.execute("DELETE FROM test_files WHERE test_id = ? AND id = ?", (test_id, file_id)).rowcount)


# ---------- Begona o'quvchining esse to'lovi (ustoz kartasiga; chekni AI tekshiradi) ----------

def set_test_price(test_id: int, price: int) -> None:
    """Begona o'quvchi uchun shu mock essesini tekshirish narxi (0 — to'lovsiz)."""
    with connect() as conn:
        conn.execute("UPDATE tests SET price = ? WHERE id = ?", (max(0, price), test_id))


_PAY_COLS = "id, test_id, student_id, amount, status, txn, note, created_at, decided_at, choice, payee"


def essay_payment_of(test_id: int, student_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute(f"SELECT {_PAY_COLS} FROM essay_payments WHERE test_id = ? AND student_id = ?",
                                 (test_id, student_id)).fetchone())


def start_essay_payment(test_id: int, student_id: int, amount: int, choice: str | None = None,
                        payee: str | None = None) -> dict:
    """To'lov boshlanadi (yoki rad etilgani qayta ochiladi). To'langan / adminda turgani o'zgarmaydi.
    amount = 0 — bepul tanlov (jadvalli mock): darhol «paid». choice — esseni kim tekshiradi (ai | teacher).
    payee = "platform" — pul platforma kartasiga (begona o'qituvchi), None — ustoz kartasiga."""
    status = "paid" if amount == 0 else "open"
    with connect() as conn:
        conn.execute(
            "INSERT INTO essay_payments (test_id, student_id, amount, status, created_at, choice, payee) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(test_id, student_id) DO UPDATE SET amount = excluded.amount, status = excluded.status, "
            "created_at = excluded.created_at, choice = excluded.choice, payee = excluded.payee "
            "WHERE essay_payments.status IN ('open', 'rejected')",
            (test_id, student_id, amount, status, _now(), choice, payee))
        return _row(conn.execute(f"SELECT {_PAY_COLS} FROM essay_payments WHERE test_id = ? AND student_id = ?",
                                 (test_id, student_id)).fetchone())


def open_essay_payment(student_id: int) -> dict | None:
    """Botga chek yuborilganda: o'quvchining eng oxirgi to'lanmagan esse to'lovi."""
    with connect() as conn:
        return _row(conn.execute(
            "SELECT p.id, p.test_id, p.amount, p.status, p.created_at, p.payee, t.title, t.code FROM essay_payments p "
            "JOIN tests t ON t.id = p.test_id "
            "WHERE p.student_id = ? AND p.status IN ('open', 'rejected', 'review') ORDER BY p.created_at DESC, p.id DESC LIMIT 1",
            (student_id,)).fetchone())


def get_essay_payment(payment_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute(
            "SELECT p.*, t.title, t.code, t.owner_id, u.full_name, u.username FROM essay_payments p "
            "JOIN tests t ON t.id = p.test_id LEFT JOIN users u ON u.tg_id = p.student_id WHERE p.id = ?",
            (payment_id,)).fetchone())


def save_essay_receipt(payment_id: int, receipt: str, mime: str, receipt_hash: str, txn: str | None,
                       note: str, status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE essay_payments SET receipt = ?, mime = ?, receipt_hash = ?, txn = ?, note = ?, status = ?, "
                     "decided_at = ? WHERE id = ?",
                     (receipt, mime, receipt_hash, txn, note, status, _now() if status == "paid" else None, payment_id))


def receipt_used(receipt_hash: str, txn: str | None, payment_id: int, table: str = "essay_payments") -> bool:
    """Shu chek (rasm yoki tranzaksiya raqami) boshqa to'lovda ishlatilganmi — mock esse to'lovlari va kredit
    paketlari birga tekshiriladi (bitta chek ikkalasiga ishlatilmasin). table — shu to'lov qaysi jadvalda."""
    # Obuna to'lovlari (payments) ham: u yerda «sent» — admin ko'rib chiqmoqda
    tables = {"essay_payments": "'paid', 'review'", "wallet_payments": "'paid', 'review'", "payments": "'paid', 'sent'",
              "pass_payments": "'paid', 'review'"}  # o'quvchi tariflari
    with connect() as conn:
        for name, statuses in tables.items():
            if conn.execute(
                f"SELECT 1 FROM {name} WHERE id <> ? AND status IN ({statuses}) AND "
                "(receipt_hash = ? OR (? <> '' AND txn = ?)) LIMIT 1",
                (payment_id if name == table else -1, receipt_hash, txn or "", txn or "")).fetchone():
                return True
    return False


# ---------- Begonaning esse krediti (ustoz bo'yicha hamyon) ----------

def wallet_balance(teacher_id: int, student_id: int) -> int:
    with connect() as conn:
        row = conn.execute("SELECT balance FROM essay_wallets WHERE teacher_id = ? AND student_id = ?",
                           (teacher_id, student_id)).fetchone()
    return row["balance"] if row else 0


def wallet_add(teacher_id: int, student_id: int, essays: int) -> int:
    """Kredit qo'shish (manfiy — ayirish, 0 dan pastga tushmaydi). Qaytaradi — yangi qoldiq."""
    with connect() as conn:
        return _wallet_add(conn, teacher_id, student_id, essays)


def _wallet_add(conn, teacher_id: int, student_id: int, essays: int) -> int:
    if essays < 0:  # ayirish — bor qoldiqdan, 0 dan pastga emas
        conn.execute("UPDATE essay_wallets SET balance = CASE WHEN balance + ? < 0 THEN 0 ELSE balance + ? END, "
                     "updated_at = ? WHERE teacher_id = ? AND student_id = ?",
                     (essays, essays, _now(), teacher_id, student_id))
        row = conn.execute("SELECT balance FROM essay_wallets WHERE teacher_id = ? AND student_id = ?",
                           (teacher_id, student_id)).fetchone()
        return row["balance"] if row else 0
    return conn.execute(
        "INSERT INTO essay_wallets (teacher_id, student_id, balance, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(teacher_id, student_id) DO UPDATE SET balance = essay_wallets.balance + excluded.balance, "
        "updated_at = excluded.updated_at RETURNING balance", (teacher_id, student_id, essays, _now())).fetchone()["balance"]


def wallet_use(teacher_id: int, student_id: int) -> bool:
    """1 kredit yechiladi — yetmasa False (bitta so'rovda: ikki esse bitta kreditni ikki marta olmaydi)."""
    with connect() as conn:
        return bool(conn.execute(
            "UPDATE essay_wallets SET balance = balance - 1, updated_at = ? WHERE teacher_id = ? AND student_id = ? "
            "AND balance >= 1", (_now(), teacher_id, student_id)).rowcount)


def remember_public(teacher_id: int, student_id: int) -> None:
    """Begona o'quvchi ustozning ommaviy havolasini ochdi — ilovadagi «✍️ Esse tekshirish»da shu ustoz chiqadi."""
    with connect() as conn:
        conn.execute("INSERT INTO essay_wallets (teacher_id, student_id, balance, updated_at) VALUES (?, ?, 0, ?) "
                     "ON CONFLICT(teacher_id, student_id) DO NOTHING", (teacher_id, student_id, _now()))


def wallet_use_n(teacher_id: int, student_id: int, credits: int) -> bool:
    """N kredit birdan (mock narxi kreditda) — yetmasa hech narsa yechilmaydi."""
    with connect() as conn:
        return bool(conn.execute(
            "UPDATE essay_wallets SET balance = balance - ?, updated_at = ? WHERE teacher_id = ? AND student_id = ? "
            "AND balance >= ?", (credits, _now(), teacher_id, student_id, credits)).rowcount)


def mark_mock_paid(test_id: int, student_id: int, amount: int, choice: str, note: str) -> None:
    """Mock kredit bilan to'landi: to'lov yozuvi darhol «paid» (chek kerak emas)."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO essay_payments (test_id, student_id, amount, status, created_at, decided_at, choice, note) "
            "VALUES (?, ?, ?, 'paid', ?, ?, ?, ?) ON CONFLICT(test_id, student_id) DO UPDATE SET amount = excluded.amount, "
            "status = 'paid', decided_at = excluded.decided_at, choice = excluded.choice, note = excluded.note",
            (test_id, student_id, amount, _now(), _now(), choice, note))


def claude_spent_since(day: str) -> dict:
    """Claude sarfi (shu kundan beri): jami $ va esse tekshiruvlari soni / narxi — «nechta esse qoldi» uchun."""
    with connect() as conn:
        rows = conn.execute("SELECT kind, SUM(calls) AS calls, SUM(cost) AS cost FROM ai_usage WHERE day >= ? "
                            "AND LOWER(model) LIKE '%claude%' GROUP BY kind", (day,)).fetchall()
    out = {"cost": 0.0, "essay_calls": 0, "essay_cost": 0.0}
    for row in rows:
        out["cost"] += float(row["cost"] or 0)
        if row["kind"] == "essay":
            out["essay_calls"] += int(row["calls"] or 0)
            out["essay_cost"] += float(row["cost"] or 0)
    return out


def wallets_of_student(student_id: int) -> list[dict]:
    """Begona o'quvchi tanigan ustozlar (ommaviy havola orqali) va har biridagi krediti."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT w.teacher_id, w.balance, u.full_name FROM essay_wallets w LEFT JOIN users u ON u.tg_id = w.teacher_id "
            "WHERE w.student_id = ? ORDER BY w.balance DESC, w.updated_at DESC", (student_id,)).fetchall()
    return [dict(row) for row in rows]


_WALLET_COLS = "id, teacher_id, student_id, essays, amount, status, txn, note, created_at, decided_at, payee"


def start_wallet_payment(teacher_id: int, student_id: int, essays: int, amount: int, payee: str | None = None) -> dict:
    """Paket tanlandi: shu ustozdagi ochiq (chek yuborilmagan yoki rad etilgan) to'lov yangilanadi, bo'lmasa yangisi.
    Chek tekshirilayotgan (review) to'lov o'zgarmaydi — o'shani qaytaradi."""
    with connect() as conn:
        busy = _row(conn.execute(
            f"SELECT {_WALLET_COLS} FROM wallet_payments WHERE teacher_id = ? AND student_id = ? AND status = 'review' "
            "ORDER BY id DESC LIMIT 1", (teacher_id, student_id)).fetchone())
        if busy:
            return busy
        conn.execute("DELETE FROM wallet_payments WHERE teacher_id = ? AND student_id = ? AND status IN ('open', 'rejected') "
                     "AND receipt IS NULL", (teacher_id, student_id))
        conn.execute("UPDATE wallet_payments SET status = 'closed' WHERE teacher_id = ? AND student_id = ? "
                     "AND status IN ('open', 'rejected')", (teacher_id, student_id))
        return _row(conn.execute(
            f"INSERT INTO wallet_payments (teacher_id, student_id, essays, amount, status, created_at, payee) "
            f"VALUES (?, ?, ?, ?, 'open', ?, ?) RETURNING {_WALLET_COLS}",
            (teacher_id, student_id, essays, amount, _now(), payee)).fetchone())


def open_wallet_payment(student_id: int) -> dict | None:
    """Botga chek yuborilganda: o'quvchining eng oxirgi to'lanmagan kredit to'lovi."""
    with connect() as conn:
        return _row(conn.execute(
            f"SELECT {_WALLET_COLS} FROM wallet_payments WHERE student_id = ? AND status IN ('open', 'rejected', 'review') "
            "ORDER BY id DESC LIMIT 1", (student_id,)).fetchone())


def get_wallet_payment(payment_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute(
            "SELECT p.*, u.full_name, u.username, t.full_name AS teacher_name FROM wallet_payments p "
            "LEFT JOIN users u ON u.tg_id = p.student_id LEFT JOIN users t ON t.tg_id = p.teacher_id WHERE p.id = ?",
            (payment_id,)).fetchone())


def save_wallet_receipt(payment_id: int, receipt: str, mime: str, receipt_hash: str, txn: str | None, note: str) -> None:
    """Chek saqlanadi va «review» (ustoz ko'radi). AI to'g'ri desa — keyin pay_wallet_payment bilan «paid»."""
    with connect() as conn:
        conn.execute("UPDATE wallet_payments SET receipt = ?, mime = ?, receipt_hash = ?, txn = ?, note = ?, status = 'review' "
                     "WHERE id = ?", (receipt, mime, receipt_hash, txn, note, payment_id))


def pay_wallet_payment(payment_id: int, allowed: tuple[str, ...]) -> int | None:
    """To'lov tasdiqlandi: holat «paid» va o'quvchiga kredit — bitta tranzaksiyada (ikki marta qo'shilmaydi).
    Qaytaradi — yangi qoldiq; holat mos kelmasa (allaqachon ko'rilgan) — None."""
    marks = ", ".join("?" * len(allowed))
    with connect() as conn:
        pay = _row(conn.execute(f"SELECT * FROM wallet_payments WHERE id = ? AND status IN ({marks})",
                                (payment_id, *allowed)).fetchone())
        if not pay or not conn.execute(
                f"UPDATE wallet_payments SET status = 'paid', decided_at = ? WHERE id = ? AND status IN ({marks})",
                (_now(), payment_id, *allowed)).rowcount:
            return None
        return _wallet_add(conn, pay["teacher_id"], pay["student_id"], pay["essays"])


def reject_wallet_payment(payment_id: int, allowed: tuple[str, ...]) -> bool:
    """Rad etish (yoki AI tasdiqlaganini bekor qilish — berilgan kredit qaytarib olinadi, ishlatilgani qoladi)."""
    marks = ", ".join("?" * len(allowed))
    with connect() as conn:
        pay = _row(conn.execute(f"SELECT * FROM wallet_payments WHERE id = ? AND status IN ({marks})",
                                (payment_id, *allowed)).fetchone())
        if not pay or not conn.execute(
                f"UPDATE wallet_payments SET status = 'rejected', decided_at = ? WHERE id = ? AND status IN ({marks})",
                (_now(), payment_id, *allowed)).rowcount:
            return False
        if pay["status"] == "paid":
            _wallet_add(conn, pay["teacher_id"], pay["student_id"], -pay["essays"])
        return True


def wallet_payments_for_teacher(teacher_id: int, limit: int = 30) -> list[dict]:
    """Ustozga kelgan kredit to'lovlari (chek yuborilganlari): tekshirilmaganlari birinchi."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT p.id, p.essays, p.amount, p.status, p.note, p.created_at, p.student_id, p.payee, u.full_name, "
            "u.username FROM wallet_payments p LEFT JOIN users u ON u.tg_id = p.student_id "
            "WHERE p.teacher_id = ? AND p.receipt IS NOT NULL AND p.status <> 'closed' "
            "ORDER BY CASE p.status WHEN 'review' THEN 0 ELSE 1 END, p.id DESC LIMIT ?", (teacher_id, limit)).fetchall()
    return [dict(row) for row in rows]


# ---------- Begona o'qituvchining daromadi va «💸 Yechib olish» ----------

def add_earning(teacher_id: int, source: str, payment_id: int, student_id: int | None, amount: int, share: int) -> bool:
    """To'lov tasdiqlandi — o'qituvchi ulushi balansga (bitta to'lov ikki marta yozilmaydi). True — yangi yozildi."""
    with connect() as conn:
        return bool(conn.execute(
            "INSERT INTO teacher_earnings (teacher_id, source, payment_id, student_id, amount, share, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source, payment_id) DO NOTHING",
            (teacher_id, source, payment_id, student_id, amount, share, _now())).rowcount)


def remove_earning(source: str, payment_id: int) -> int:
    """To'lov bekor qilindi — ulush balansdan olinadi. Qaytaradi: olingan summa (0 — yozilmagan edi)."""
    with connect() as conn:
        row = conn.execute("SELECT share FROM teacher_earnings WHERE source = ? AND payment_id = ?",
                           (source, payment_id)).fetchone()
        if not row:
            return 0
        conn.execute("DELETE FROM teacher_earnings WHERE source = ? AND payment_id = ?", (source, payment_id))
        return row["share"]


def _earnings_balance(conn, teacher_id: int) -> int:
    earned = conn.execute("SELECT COALESCE(SUM(share), 0) AS n FROM teacher_earnings WHERE teacher_id = ?",
                          (teacher_id,)).fetchone()["n"]
    taken = conn.execute("SELECT COALESCE(SUM(amount), 0) AS n FROM withdrawals WHERE teacher_id = ? "
                         "AND status IN ('pending', 'paid')", (teacher_id,)).fetchone()["n"]
    return int(earned) - int(taken)


def earnings_balance(teacher_id: int) -> int:
    """Yechib olsa bo'ladigan summa: jami ulush − (so'ralgan + o'tkazilgan)."""
    with connect() as conn:
        return _earnings_balance(conn, teacher_id)


def earnings_summary(teacher_id: int, month: str) -> dict:
    """Ilovadagi «💰 Daromad»: balans, shu oy (nechta to'lov, qancha), jami, kutilayotgan so'rov va oxirgi yechishlar.
    month — «2026-11» (created_at shu bilan boshlanadi)."""
    with connect() as conn:
        month_row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(share), 0) AS share FROM teacher_earnings "
            "WHERE teacher_id = ? AND created_at LIKE ?", (teacher_id, f"{month}%")).fetchone()
        total = conn.execute("SELECT COALESCE(SUM(share), 0) AS n FROM teacher_earnings WHERE teacher_id = ?",
                             (teacher_id,)).fetchone()["n"]
        history = conn.execute(
            "SELECT id, amount, card, status, created_at, decided_at FROM withdrawals WHERE teacher_id = ? "
            "ORDER BY id DESC LIMIT 10", (teacher_id,)).fetchall()
        balance = _earnings_balance(conn, teacher_id)
    history = [dict(row) for row in history]
    pending = next((w for w in history if w["status"] == "pending"), None)
    return {"balance": balance, "month_count": int(month_row["n"]), "month_share": int(month_row["share"]),
            "total": int(total), "pending": pending, "history": history}


def recent_earnings(teacher_id: int, limit: int = 10) -> list[dict]:
    """Yechib olish arizasi uchun: o'qituvchi balansini tashkil qilgan oxirgi to'lovlar (bekor qilinganlari o'chgan)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT e.source, e.payment_id, e.amount, e.share, e.created_at, u.full_name FROM teacher_earnings e "
            "LEFT JOIN users u ON u.tg_id = e.student_id WHERE e.teacher_id = ? ORDER BY e.id DESC LIMIT ?",
            (teacher_id, limit)).fetchall()
    return [dict(row) for row in rows]


def request_withdrawal(teacher_id: int, amount: int, card: str, holder: str) -> dict | None:
    """Yechish so'rovi (kutilayotgani bo'lmasa va balans yetsa). None — mumkin emas (chaqirgan tomon sababini aytadi)."""
    with connect() as conn:
        if conn.execute("SELECT 1 FROM withdrawals WHERE teacher_id = ? AND status = 'pending' LIMIT 1",
                        (teacher_id,)).fetchone():
            return None
        if amount <= 0 or _earnings_balance(conn, teacher_id) < amount:
            return None
        return _row(conn.execute(
            "INSERT INTO withdrawals (teacher_id, amount, card, holder, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?) RETURNING *", (teacher_id, amount, card, holder, _now())).fetchone())


def get_withdrawal(withdrawal_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute(
            "SELECT w.*, u.full_name, u.username FROM withdrawals w LEFT JOIN users u ON u.tg_id = w.teacher_id "
            "WHERE w.id = ?", (withdrawal_id,)).fetchone())


def decide_withdrawal(withdrawal_id: int, status: str, admin_id: int) -> bool:
    """Admin: «paid» (pul o'tkazildi) yoki «rejected» (summa balansga qaytadi). Faqat kutilayotgan so'rov."""
    with connect() as conn:
        return bool(conn.execute(
            "UPDATE withdrawals SET status = ?, decided_at = ?, decided_by = ? WHERE id = ? AND status = 'pending'",
            (status, _now(), admin_id, withdrawal_id)).rowcount)


def pending_withdrawals() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT w.*, u.full_name, u.username FROM withdrawals w LEFT JOIN users u ON u.tg_id = w.teacher_id "
            "WHERE w.status = 'pending' ORDER BY w.id").fetchall()
    return [dict(row) for row in rows]


def platform_income(month: str) -> dict:
    """Admin uchun: shu oy platformaga tushgan begona to'lovlar (jami, o'qituvchilar ulushi, sof qism)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS amount, COALESCE(SUM(share), 0) AS share "
            "FROM teacher_earnings WHERE created_at LIKE ?", (f"{month}%",)).fetchone()
    return {"count": int(row["n"]), "amount": int(row["amount"]), "share": int(row["share"]),
            "net": int(row["amount"]) - int(row["share"])}


def practice_awaiting_credit(teacher_id: int, student_id: int) -> list[dict]:
    """Kredit kutayotgan esselar (o'quvchi kredit olganda — eng eskisidan boshlab tekshiriladi)."""
    with connect() as conn:
        rows = conn.execute("SELECT * FROM practice_essays WHERE teacher_id = ? AND student_id = ? "
                            "AND status = 'awaiting_credit' ORDER BY id", (teacher_id, student_id)).fetchall()
    return [_practice(row) for row in rows]


def set_practice_wallet(practice_id: int, status: str) -> None:
    """Esse kredit bilan to'landi: holat (pending) va «kredit bilan» belgisi (tekshirilmasa — kredit qaytadi)."""
    with connect() as conn:
        conn.execute("UPDATE practice_essays SET status = ?, wallet = 1 WHERE id = ?", (status, practice_id))


ESSAY_SCAN_PAGES = 4


def start_essay_scan(student_id: int, test_id: int, ai: str) -> None:
    """Esse rasmlarini yig'ish boshlanadi (oldingisi tozalanadi)."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO essay_scans (student_id, test_id, status, ai, pages, text, model, error, updated_at) "
            "VALUES (?, ?, 'collecting', ?, '[]', NULL, NULL, NULL, ?) ON CONFLICT(student_id, test_id) DO UPDATE SET "
            "status = 'collecting', ai = excluded.ai, pages = '[]', text = NULL, model = NULL, error = NULL, "
            "updated_at = excluded.updated_at", (student_id, test_id, ai, _now()))


def collecting_essay_scan(student_id: int) -> dict | None:
    """Botga yuborilgan rasm — esse sahifasimi: o'quvchining rasm yig'ilayotgan essesi (eng oxirgisi)."""
    with connect() as conn:
        return _row(conn.execute(
            "SELECT s.student_id, s.test_id, s.ai, s.pages, t.title, t.code FROM essay_scans s JOIN tests t ON t.id = s.test_id "
            "WHERE s.student_id = ? AND s.status = 'collecting' ORDER BY s.updated_at DESC LIMIT 1", (student_id,)).fetchone())


def add_essay_scan_page(student_id: int, test_id: int, image_b64: str, mime: str) -> int:
    """Sahifa qo'shiladi — jami nechta (ESSAY_SCAN_PAGES dan oshmaydi)."""
    with connect() as conn:
        row = conn.execute("SELECT pages FROM essay_scans WHERE student_id = ? AND test_id = ?", (student_id, test_id)).fetchone()
        pages = json.loads(row["pages"] or "[]") if row else []
        if len(pages) < ESSAY_SCAN_PAGES:
            pages.append([image_b64, mime])
            conn.execute("UPDATE essay_scans SET pages = ?, updated_at = ? WHERE student_id = ? AND test_id = ?",
                         (json.dumps(pages), _now(), student_id, test_id))
    return len(pages)


def get_essay_scan(student_id: int, test_id: int) -> dict | None:
    with connect() as conn:
        row = _row(conn.execute("SELECT * FROM essay_scans WHERE student_id = ? AND test_id = ?",
                                (student_id, test_id)).fetchone())
    if row:
        row["pages"] = json.loads(row["pages"] or "[]")
    return row


def set_essay_scan(student_id: int, test_id: int, status: str, text: str | None = None, model: str | None = None,
                   error: str | None = None, keep_pages: bool = False) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE essay_scans SET status = ?, text = ?, model = ?, error = ?, updated_at = ?"
            + ("" if keep_pages else ", pages = '[]'") + " WHERE student_id = ? AND test_id = ?",
            (status, text, model, error, _now(), student_id, test_id))


def essay_payments_for_owner(owner_id: int, limit: int = 30) -> list[dict]:
    """Ustozning mocklariga kelgan esse to'lovlari (chek yuborilganlari): ko'rib chiqilmaganlari birinchi."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT p.id, p.amount, p.status, p.note, p.created_at, p.student_id, p.payee, t.title, t.code, u.full_name, "
            "u.username FROM essay_payments p JOIN tests t ON t.id = p.test_id LEFT JOIN users u ON u.tg_id = p.student_id "
            "WHERE t.owner_id = ? AND p.receipt IS NOT NULL "
            "ORDER BY CASE p.status WHEN 'review' THEN 0 ELSE 1 END, p.id DESC LIMIT ?", (owner_id, limit)).fetchall()
    return [dict(row) for row in rows]


def record_ai_usage(kind: str, model: str, usage: dict | None, limited: bool = False) -> None:
    day = datetime.now().strftime("%Y-%m-%d")
    ok = usage is not None
    usage = usage or {}
    with connect() as conn:
        conn.execute(
            "INSERT INTO ai_usage (day, kind, model, calls, failures, limited, tokens_in, tokens_out, cost) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(day, kind, model) DO UPDATE SET "
            "calls = ai_usage.calls + excluded.calls, failures = ai_usage.failures + excluded.failures, "
            "limited = ai_usage.limited + excluded.limited, tokens_in = ai_usage.tokens_in + excluded.tokens_in, "
            "tokens_out = ai_usage.tokens_out + excluded.tokens_out, cost = ai_usage.cost + excluded.cost",
            (day, kind, model[:60], 1 if ok else 0, 0 if ok else 1, 1 if limited else 0,
             int(usage.get("in") or 0), int(usage.get("out") or 0), float(usage.get("cost") or 0)))


def ai_usage_since(day: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT kind, model, SUM(calls) AS calls, SUM(failures) AS failures, SUM(limited) AS limited, "
            "SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out, SUM(cost) AS cost FROM ai_usage "
            "WHERE day >= ? GROUP BY kind, model ORDER BY kind, model", (day,)).fetchall()
    return [dict(row) for row in rows]


def ai_waiting_by_owner() -> list[dict]:
    """AI navbatida kutayotganlar ustoz bo'yicha: yozma javoblar (titullar) va esselar."""
    with connect() as conn:
        written = conn.execute(
            "SELECT t.owner_id, COUNT(*) AS n FROM written_jobs w JOIN tests t ON t.id = w.test_id "
            "WHERE w.status = 'pending' GROUP BY t.owner_id").fetchall()
        essays = conn.execute(
            "SELECT t.owner_id, COUNT(*) AS n FROM essays e JOIN attempts a ON a.id = e.attempt_id "
            "JOIN tests t ON t.id = a.test_id WHERE e.status = 'pending' GROUP BY t.owner_id").fetchall()
    out: dict[int, dict] = {}
    for row in written:
        out.setdefault(row["owner_id"], {"owner_id": row["owner_id"], "written": 0, "essays": 0})["written"] = row["n"]
    for row in essays:
        out.setdefault(row["owner_id"], {"owner_id": row["owner_id"], "written": 0, "essays": 0})["essays"] = row["n"]
    return list(out.values())


def decide_essay_payment(payment_id: int, status: str, allowed: tuple[str, ...]) -> bool:
    marks = ", ".join("?" * len(allowed))
    with connect() as conn:
        return bool(conn.execute(f"UPDATE essay_payments SET status = ?, decided_at = ? WHERE id = ? AND status IN ({marks})",
                                 (status, _now(), payment_id, *allowed)).rowcount)








def save_written_pending(test_id: int, number: int, image_b64: str, mime: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO written_pending (test_id, number, image, mime, created_at) VALUES (?, ?, ?, ?, ?) "
                     "ON CONFLICT(test_id, number) DO UPDATE SET image = excluded.image, mime = excluded.mime, "
                     "created_at = excluded.created_at", (test_id, number, image_b64, mime, _now()))


def take_written_pending(test_id: int, number: int) -> dict | None:
    """Kutayotgan 2-varaq rasmi (olinadi va o'chiriladi)."""
    with connect() as conn:
        row = conn.execute("SELECT image, mime FROM written_pending WHERE test_id = ? AND number = ?",
                           (test_id, number)).fetchone()
        if row:
            conn.execute("DELETE FROM written_pending WHERE test_id = ? AND number = ?", (test_id, number))
    return _row(row)


def written_images_for_test(test_id: int) -> set[int]:
    """Yozma kataklar rasmi saqlangan natijalar — qo'lda tekshirish ekrani shularni ko'rsatadi."""
    with connect() as conn:
        rows = conn.execute("SELECT attempt_id FROM written_jobs WHERE test_id = ? AND image IS NOT NULL",
                            (test_id,)).fetchall()
    return {row["attempt_id"] for row in rows}


def pending_written(limit: int = 5) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM written_jobs WHERE status = 'pending' ORDER BY created_at, attempt_id LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def written_done(attempt_id: int, result: dict) -> None:
    """O'qildi. Rasm qoladi — o'qituvchi AI o'qiganini rasm bilan solishtirib tuzata oladi (~60 KB)."""
    with connect() as conn:
        conn.execute(
            "UPDATE written_jobs SET status = 'done', error = NULL, result = ? WHERE attempt_id = ?",
            (json.dumps(result, ensure_ascii=False), attempt_id),
        )


def written_failed(attempt_id: int, error: str, give_up: bool) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE written_jobs SET tries = tries + 1, error = ?, "
            "status = CASE WHEN ? = 1 THEN 'failed' ELSE status END WHERE attempt_id = ?",
            (error[:500], int(give_up), attempt_id),
        )


def get_written_job(attempt_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM written_jobs WHERE attempt_id = ?", (attempt_id,)).fetchone())


def written_jobs_for_test(test_id: int) -> dict[int, str]:
    """{attempt_id: holat} — natijalar ro'yxatida «AI o'qimoqda / o'qiy olmadi» belgisi uchun."""
    with connect() as conn:
        rows = conn.execute("SELECT attempt_id, status FROM written_jobs WHERE test_id = ?", (test_id,)).fetchall()
    return {row["attempt_id"]: row["status"] for row in rows}


def purge_written_images(days: int = 45) -> int:
    """Eski titullarning yozma kataklar rasmlari o'chiriladi (natija qoladi) — baza to'lib ketmasin."""
    before = datetime.fromtimestamp(time.time() - days * 86400).strftime("%Y-%m-%d %H:%M")
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE written_jobs SET image = NULL WHERE image IS NOT NULL AND status <> 'pending' AND created_at < ?",
            (before,),
        )
        return cursor.rowcount or 0


# ---------- Zaxira nusxa (backup) ----------

# Tiklash tartibi muhim: avval bog'lanadigan jadvallar. app_tokens (kirish sessiyalari — maxfiy) va
# live (vaqtinchalik) nusxaga kirmaydi; yozma kataklar rasmlari ham (hajmi katta, natija o'zi saqlangan)
BACKUP_TABLES = {
    "users": "*",
    "channels": "*",
    "tests": "*",
    "variants": "*",
    "attempts": "*",
    "essays": "*",
    "students": "*",
    "written_jobs": "attempt_id, test_id, mime, status, tries, error, result, notified, created_at",
    "offline_essays": "*",
    "classes": "*",
    "class_students": "*",
    "id_counters": "*",
    "subscriptions": "*",
    "usage": "*",
    "credits": "*",
    "settings": "*",
    "payments": "*",
    "registrations": "*",
    "teacher_places": "*",
    "teacher_students": "*",
    "practice_essays": "*",
    # Rasmlar (o'quvchi surati, to'lov cheki) nusxaga kirmaydi — hajmi katta; chek izi (hash, tranzaksiya) qoladi
    "join_requests": "id, teacher_id, student_id, first_name, last_name, status, created_at, decided_at",
    "essay_payments": "id, test_id, student_id, amount, status, receipt_hash, txn, note, created_at, decided_at, payee",
    "ai_usage": "*",
    "essay_wallets": "*",  # begonalarning esse krediti (pul to'langan — yo'qolmasin)
    "wallet_payments": "id, teacher_id, student_id, essays, amount, status, receipt_hash, txn, note, created_at, decided_at, "
                       "payee",
    "teacher_earnings": "*",  # o'qituvchilar balansi (platformaga tushgan puldan ulushi)
    "withdrawals": "*",
    "mock_signups": "*",
    "attempt_sheets": "*",
    "student_passes": "*",
    "pass_payments": "id, teacher_id, student_id, days, amount, status, receipt_hash, txn, note, created_at, decided_at",
    "test_files": "*",  # faqat Telegram file_id — fayllarning o'zi Telegram'da
}
SERIAL_TABLES = ("tests", "attempts", "essays", "students", "classes", "class_students",
                 "payments", "registrations", "teacher_places", "practice_essays", "join_requests", "essay_payments",
                 "wallet_payments", "test_files", "pass_payments", "teacher_earnings", "withdrawals")


def export_tables() -> dict[str, list[dict]]:
    with connect() as conn:
        return {table: [dict(row) for row in conn.execute(f"SELECT {columns} FROM {table}").fetchall()]
                for table, columns in BACKUP_TABLES.items()}


def import_tables(data: dict[str, list[dict]], force: bool = False) -> dict[str, int]:
    """Zaxira nusxadan tiklash — bo'sh bazaga (force=True bo'lsa, mavjud yozuvlar ustiga qo'shmaydi, o'tkazib yuboradi)."""
    _forget_settings()
    counts = {}
    with connect() as conn:
        if not force and conn.execute("SELECT COUNT(*) AS n FROM tests").fetchone()["n"]:
            raise ValueError("Baza bo'sh emas — tiklash faqat yangi (bo'sh) bazaga. Majburan: --force")
        for table in BACKUP_TABLES:
            rows = data.get(table) or []
            for row in rows:
                columns = list(row)
                conn.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
                    "ON CONFLICT DO NOTHING",
                    [row[c] for c in columns],
                )
            counts[table] = len(rows)
        if POSTGRES:  # avtomatik raqamlar tiklangan eng katta id'dan davom etsin
            for table in SERIAL_TABLES:
                conn.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                             f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")
    return counts


# ---------- Sinflar (o'qituvchining doimiy o'quvchilar ro'yxati) ----------

MAX_ID = 9999  # titulda 4 xonali ID


def list_classes(owner_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM class_students s WHERE s.class_id = c.id) AS students "
            "FROM classes c WHERE c.owner_id = ? ORDER BY c.id", (owner_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def create_class(owner_id: int, name: str) -> dict:
    with connect() as conn:
        row = conn.execute("INSERT INTO classes (owner_id, name, created_at) VALUES (?, ?, ?) RETURNING *",
                           (owner_id, name, _now())).fetchone()
    return dict(row)


def get_class(class_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM classes WHERE id = ?", (class_id,)).fetchone())


def rename_class(class_id: int, name: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE classes SET name = ? WHERE id = ?", (name, class_id))


def delete_class(class_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM class_students WHERE class_id = ?", (class_id,))
        conn.execute("DELETE FROM classes WHERE id = ?", (class_id,))


def list_class_students(class_id: int) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM class_students WHERE class_id = ? ORDER BY name, number", (class_id,)).fetchall()]


def owner_class_students(owner_id: int) -> list[dict]:
    """O'qituvchining barcha sinflaridagi o'quvchilar (sinf nomi bilan) — natijalarni doimiy ID orqali topish uchun."""
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT s.*, c.name AS class_name FROM class_students s JOIN classes c ON c.id = s.class_id "
            "WHERE s.owner_id = ?", (owner_id,)).fetchall()]


def add_class_students(class_id: int, owner_id: int, names: list[str]) -> list[dict]:
    """Yangi o'quvchilar — o'qituvchining keyingi ID'lari bilan (o'chirilgan ID hech qachon qayta berilmaydi)."""
    with connect() as conn:
        counter = conn.execute("SELECT last FROM id_counters WHERE owner_id = ?", (owner_id,)).fetchone()
        present = conn.execute("SELECT COALESCE(MAX(number), 0) AS n FROM class_students WHERE owner_id = ?",
                               (owner_id,)).fetchone()["n"]
        last = max(counter["last"] if counter else 0, present)
        if last + len(names) > MAX_ID:
            raise ValueError(f"ID'lar tugadi (ko'pi bilan {MAX_ID})")
        for index, name in enumerate(names, start=1):
            conn.execute("INSERT INTO class_students (class_id, owner_id, number, name) VALUES (?, ?, ?, ?)",
                         (class_id, owner_id, last + index, name))
        conn.execute("INSERT INTO id_counters (owner_id, last) VALUES (?, ?) "
                     "ON CONFLICT(owner_id) DO UPDATE SET last = excluded.last", (owner_id, last + len(names)))
    return list_class_students(class_id)


def update_class_student(class_id: int, student_id: int, name: str | None = None, delete: bool = False) -> None:
    with connect() as conn:
        if delete:
            conn.execute("DELETE FROM class_students WHERE id = ? AND class_id = ?", (student_id, class_id))
        elif name:
            conn.execute("UPDATE class_students SET name = ? WHERE id = ? AND class_id = ?", (name, student_id, class_id))


# ---------- Ro'yxatdan o'tish (oflayn «Ona tili» kursi) ----------

def save_registration(tg_id: int, role: str, first_name: str, last_name: str, phone: str,
                      age: int | None, region: str, district: str) -> dict:
    """Yangi ariza yoki borini yangilash (bitta foydalanuvchi — bitta ariza).

    full_name — ism va familiyadan yig'iladi: ro'yxatlar va adminga xabarlarda shu ishlatiladi.
    O'qituvchida region/district — birinchi joyi (qolganlari teacher_places jadvalida).
    """
    full_name = f"{first_name} {last_name}".strip()
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO registrations (tg_id, role, first_name, last_name, full_name, phone, age, "
            "region, district, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET role = excluded.role, first_name = excluded.first_name, "
            "last_name = excluded.last_name, full_name = excluded.full_name, phone = excluded.phone, "
            "age = excluded.age, region = excluded.region, district = excluded.district, "
            "updated_at = excluded.updated_at RETURNING *",
            (tg_id, role, first_name, last_name, full_name, phone, age, region, district, _now(), _now()),
        ).fetchone()
    return dict(row)


# ---------- O'qituvchining dars beradigan joylari ----------

def set_teacher_places(tg_id: int, places: list[tuple[str, str]]) -> list[dict]:
    """O'qituvchining joylari to'liq almashtiriladi (ro'yxatni qayta yuborganda eskisi qolmaydi)."""
    with connect() as conn:
        conn.execute("DELETE FROM teacher_places WHERE tg_id = ?", (tg_id,))
        for region, district in places:
            conn.execute("INSERT INTO teacher_places (tg_id, region, district, created_at) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(tg_id, region, district) DO NOTHING", (tg_id, region, district, _now()))
    return list_teacher_places(tg_id)


def list_teacher_places(tg_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT region, district FROM teacher_places WHERE tg_id = ? ORDER BY region, district",
                            (tg_id,)).fetchall()
    return [dict(row) for row in rows]


def teachers_in(region: str, district: str) -> list[dict]:
    """Shu tumanda dars beradigan o'qituvchilar — o'quvchining arizasi shularga boradi."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT p.tg_id, r.full_name, r.phone, u.username FROM teacher_places p "
            "LEFT JOIN registrations r ON r.tg_id = p.tg_id LEFT JOIN users u ON u.tg_id = p.tg_id "
            "WHERE p.region = ? AND p.district = ?", (region, district)).fetchall()
    return [dict(row) for row in rows]


def districts_with_teachers() -> list[dict]:
    """Kurs bor joylar (o'quvchi tanlashda belgilab ko'rsatiladi)."""
    with connect() as conn:
        rows = conn.execute("SELECT region, district, COUNT(*) AS teachers FROM teacher_places "
                            "GROUP BY region, district").fetchall()
    return [dict(row) for row in rows]


def get_registration(tg_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM registrations WHERE tg_id = ?", (tg_id,)).fetchone())


def list_registrations(limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT r.*, u.username FROM registrations r LEFT JOIN users u ON u.tg_id = r.tg_id "
            "ORDER BY r.id DESC LIMIT ?", (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def count_registrations() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM registrations").fetchone()["n"]


# ---------- Obuna va AI sarfi ----------

def get_subscription(tg_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM subscriptions WHERE tg_id = ?", (tg_id,)).fetchone())


def set_subscription(tg_id: int, plan: str, expires_at: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO subscriptions (tg_id, plan, expires_at, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET plan = excluded.plan, expires_at = excluded.expires_at, "
            "updated_at = excluded.updated_at, reminded = NULL",
            (tg_id, plan, expires_at, _now()),
        )


def mark_reminded(tg_id: int, key: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE subscriptions SET reminded = ? WHERE tg_id = ?", (key, tg_id))


def list_subscriptions() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT s.*, u.full_name, u.username FROM subscriptions s LEFT JOIN users u ON u.tg_id = s.tg_id "
            "ORDER BY s.expires_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def find_user(ref: str) -> dict | None:
    """Telegram ID yoki @username bo'yicha (bot bilan gaplashgan foydalanuvchi)."""
    ref = ref.strip()
    with connect() as conn:
        if ref.lstrip("-").isdigit():
            row = conn.execute("SELECT * FROM users WHERE tg_id = ?", (int(ref),)).fetchone()
            return dict(row) if row else {"tg_id": int(ref), "full_name": None, "username": None}
        row = conn.execute("SELECT * FROM users WHERE LOWER(username) = ?", (ref.lstrip("@").lower(),)).fetchone()
    return _row(row)


def get_usage(tg_id: int, month: str) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM usage WHERE tg_id = ? AND month = ?", (tg_id, month)).fetchone()
    return dict(row) if row else {"tg_id": tg_id, "month": month, "essays": 0, "written": 0, "notified": 0}


def consume_usage(tg_id: int, month: str, kind: str, amount: int, limit: int | None) -> bool:
    """AI sarfini yozadi — limitdan oshsa yozmaydi va False (bitta tranzaksiyada: ikki so'rov limitni yorib o'tmaydi)."""
    assert kind in ("essays", "written")
    with connect() as conn:
        conn.execute("INSERT INTO usage (tg_id, month) VALUES (?, ?) ON CONFLICT(tg_id, month) DO NOTHING", (tg_id, month))
        if limit is None:
            conn.execute(f"UPDATE usage SET {kind} = {kind} + ? WHERE tg_id = ? AND month = ?", (amount, tg_id, month))
            return True
        updated = conn.execute(
            f"UPDATE usage SET {kind} = {kind} + ? WHERE tg_id = ? AND month = ? AND {kind} + ? <= ?",
            (amount, tg_id, month, amount, limit),
        ).rowcount
    return bool(updated)


def reset_limit_notice(tg_id: int, month: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE usage SET notified = 0 WHERE tg_id = ? AND month = ?", (tg_id, month))


def get_credits(tg_id: int) -> int:
    """Sotib olingan qo'shimcha esselar qoldig'i."""
    with connect() as conn:
        row = conn.execute("SELECT essays FROM credits WHERE tg_id = ?", (tg_id,)).fetchone()
    return row["essays"] if row else 0


def add_credits(tg_id: int, essays: int) -> int:
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO credits (tg_id, essays, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET essays = credits.essays + excluded.essays, updated_at = excluded.updated_at "
            "RETURNING essays", (tg_id, essays, _now())).fetchone()
    return row["essays"]


def use_credits(tg_id: int, amount: int) -> bool:
    """Paketdan olish — yetmasa olmaydi va False (bitta so'rovda: ikki esse bitta qoldiqni ikki marta olmaydi)."""
    with connect() as conn:
        return bool(conn.execute("UPDATE credits SET essays = essays - ?, updated_at = ? WHERE tg_id = ? AND essays >= ?",
                                 (amount, _now(), tg_id, amount)).rowcount)


def take_limit_notice(tg_id: int, month: str) -> bool:
    """Limit tugagani haqida oyiga bir marta xabar — birinchi chaqiriqda True."""
    with connect() as conn:
        return bool(conn.execute("UPDATE usage SET notified = 1 WHERE tg_id = ? AND month = ? AND notified = 0",
                                 (tg_id, month)).rowcount)


# ---------- Ustozning esse kodi ----------
# Qisqa doimiy kod (E + 5 raqam): o'quvchi botga yozsa — ustozga ulanadi, esselari ustoz limitidan tekshiriladi

def essay_code_of(teacher_id: int) -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (f"essay_code:{teacher_id}",)).fetchone()
        if row:
            return row["value"]
        for _ in range(200):
            code = f"E{random.randint(10000, 99999)}"
            if not conn.execute("SELECT 1 FROM settings WHERE key = ?", (f"essay_code_owner:{code}",)).fetchone():
                break
        else:
            raise RuntimeError("Bo'sh esse kodi topilmadi")
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (f"essay_code:{teacher_id}", code))
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (f"essay_code_owner:{code}", str(teacher_id)))
    return code


def teacher_of_essay_code(code: str) -> int | None:
    code = "E" + "".join(ch for ch in str(code) if ch.isdigit())
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (f"essay_code_owner:{code}",)).fetchone()
    return int(row["value"]) if row else None


# ---------- Sozlamalar (admin bot orqali o'zgartiradi) ----------

# Sozlamalar kichik va kam o'zgaradi, lekin bitta sahifa ochilishida 30 martagacha o'qilardi (har biri — masofadagi
# bazaga alohida so'rov). Endi xotirada: yozilganda darhol yangilanadi, baribir SETTINGS_TTL soniyada bir qayta o'qiladi
SETTINGS_TTL = 30
_settings_cache: dict = {"at": 0.0, "data": None, "gen": 0}
_settings_lock = threading.Lock()


def _forget_settings() -> None:
    with _settings_lock:
        _settings_cache["data"] = None
        _settings_cache["gen"] += 1  # shu paytda o'qilayotgan (eski) javob xotiraga yozilmasin


def get_settings() -> dict:
    with _settings_lock:
        data, at, gen = _settings_cache["data"], _settings_cache["at"], _settings_cache["gen"]
    if data is not None and time.monotonic() - at < SETTINGS_TTL:
        return dict(data)  # nusxa — chaqirgan tomon o'zgartirsa ham xotiradagisi buzilmaydi
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    data = {row["key"]: row["value"] for row in rows}
    with _settings_lock:
        # O'qish davomida sozlama yozilgan bo'lsa (gen o'zgargan) — bu javob eskirgan bo'lishi mumkin, saqlanmaydi
        if _settings_cache["gen"] == gen:
            _settings_cache.update(data=data, at=time.monotonic())
    return dict(data)


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
    _forget_settings()


# ---------- Obuna to'lovlari (karta → chek → admin tasdiqlaydi) ----------

def create_payment(tg_id: int, period: str, amount: int, essays: int | None = None) -> dict:
    """Yangi to'lov so'rovi (obuna muddati yoki esse paketi). Eski tugallanmagan so'rovlar bekor qilinadi."""
    with connect() as conn:
        conn.execute("UPDATE payments SET status = 'canceled' WHERE tg_id = ? AND status IN ('pending', 'sent')",
                     (tg_id,))
        row = conn.execute(
            "INSERT INTO payments (tg_id, period, amount, essays, created_at) VALUES (?, ?, ?, ?, ?) RETURNING *",
            (tg_id, period, amount, essays, _now()),
        ).fetchone()
    return dict(row)


def get_payment(payment_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute(
            "SELECT p.*, u.full_name, u.username FROM payments p LEFT JOIN users u ON u.tg_id = p.tg_id "
            "WHERE p.id = ?", (payment_id,)).fetchone())


def open_payment(tg_id: int) -> dict | None:
    """Foydalanuvchining tugallanmagan to'lovi (chek kutilmoqda yoki admin ko'rib chiqmoqda)."""
    with connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM payments WHERE tg_id = ? AND status IN ('pending', 'sent') ORDER BY id DESC LIMIT 1",
            (tg_id,)).fetchone())


def attach_receipt(payment_id: int, file_id: str, receipt_hash: str | None = None, txn: str | None = None,
                   note: str | None = None) -> None:
    with connect() as conn:
        conn.execute("UPDATE payments SET receipt = ?, receipt_hash = ?, txn = ?, note = ?, status = 'sent' "
                     "WHERE id = ? AND status IN ('pending', 'sent')", (file_id, receipt_hash, txn, note, payment_id))


def decide_payment(payment_id: int, status: str, admin_id: int) -> bool:
    """Admin tasdiqladi (paid) yoki rad etdi (rejected) — birinchi bosganda True (ikkinchi admin takrorlamaydi)."""
    with connect() as conn:
        return bool(conn.execute(
            "UPDATE payments SET status = ?, decided_at = ?, admin_id = ? WHERE id = ? AND status IN ('pending', 'sent')",
            (status, _now(), admin_id, payment_id)).rowcount)


def undo_payment(payment_id: int, admin_id: int) -> bool:
    """AI tasdiqlagan to'lovni admin bekor qildi (soxta chek): paid → rejected, bir marta."""
    with connect() as conn:
        return bool(conn.execute(
            "UPDATE payments SET status = 'rejected', decided_at = ?, admin_id = ? WHERE id = ? AND status = 'paid'",
            (_now(), admin_id, payment_id)).rowcount)


def take_credits(tg_id: int, essays: int) -> int:
    """Paket bekor qilindi: kreditlar olinadi (manfiyga tushmaydi). Qaytaradi — qolgan kredit."""
    with connect() as conn:
        # CASE — SQLite (skalyar MAX) va PostgreSQL (GREATEST) ikkalasida bir xil ishlaydi
        row = conn.execute("UPDATE credits SET essays = CASE WHEN essays > ? THEN essays - ? ELSE 0 END, updated_at = ? "
                           "WHERE tg_id = ? RETURNING essays", (essays, essays, _now(), tg_id)).fetchone()
    return row["essays"] if row else 0


def list_payments(status: str | None = None, limit: int = 30) -> list[dict]:
    with connect() as conn:
        sql = ("SELECT p.*, u.full_name, u.username FROM payments p LEFT JOIN users u ON u.tg_id = p.tg_id "
               + ("WHERE p.status = ? " if status else "") + "ORDER BY p.id DESC LIMIT ?")
        rows = conn.execute(sql, ((status, limit) if status else (limit,))).fetchall()
    return [dict(row) for row in rows]


# ---------- Titulli testning ilovada yozilgan essesi ----------

def get_offline_essay(test_id: int, number: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM offline_essays WHERE test_id = ? AND number = ?",
                                 (test_id, number)).fetchone())


def offline_essay_of_user(test_id: int, tg_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM offline_essays WHERE test_id = ? AND tg_id = ?",
                                 (test_id, tg_id)).fetchone())


def save_offline_essay(test_id: int, number: int, tg_id: int, name: str | None, text: str, verified: bool = False) -> None:
    """Shu ID'li esse (yangi yoki almashtirilgan). Esse kodi bilan kelgan bo'lsa — egasi o'zgaradi va «tasdiqlangan»."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO offline_essays (test_id, number, tg_id, name, text, created_at, verified) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(test_id, number) DO UPDATE SET tg_id = excluded.tg_id, name = excluded.name, text = excluded.text, "
            "created_at = excluded.created_at, verified = CASE WHEN excluded.verified = 1 THEN 1 ELSE offline_essays.verified END",
            (test_id, number, tg_id, name, text, _now(), 1 if verified else 0),
        )


def list_offline_essays(test_id: int) -> list[dict]:
    """O'qituvchi uchun: botda ID bilan yozilgan esselar — kim (qaysi Telegram akkaunt) yuborgani,
    ro'yxatdagi ism, natijaga biriktirilganmi. Begona ID bilan yuborilganini shu yerdan ko'radi."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT e.number, e.tg_id, e.name, e.text, e.created_at, e.verified, s.name AS roster_name, s.attempt_id, "
            "a.essay AS score, u.full_name AS tg_name, u.username "
            "FROM offline_essays e "
            "LEFT JOIN students s ON s.test_id = e.test_id AND s.number = e.number "
            "LEFT JOIN attempts a ON a.id = s.attempt_id "
            "LEFT JOIN users u ON u.tg_id = e.tg_id "
            "WHERE e.test_id = ? ORDER BY e.number",
            (test_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_offline_essay(test_id: int, number: int) -> int | None:
    """O'qituvchi esseni bekor qiladi (begona yoki xato ID bilan yuborilgan): ID bo'shaydi, egasi qayta yuboradi.
    Natijaga biriktirilgan bo'lsa — o'sha esse va uning bahosi ham olib tashlanadi (qog'ozdagi esse suratlari
    tegilmaydi). Qaytaradi: yuborgan akkaunt (tg_id) yoki None — bunday esse yo'q."""
    with connect() as conn:
        row = conn.execute("SELECT tg_id FROM offline_essays WHERE test_id = ? AND number = ?",
                           (test_id, number)).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM offline_essays WHERE test_id = ? AND number = ?", (test_id, number))
        student = conn.execute("SELECT attempt_id FROM students WHERE test_id = ? AND number = ? "
                               "AND attempt_id IS NOT NULL", (test_id, number)).fetchone()
        if student:
            removed = conn.execute("DELETE FROM essays WHERE attempt_id = ? AND text IS NOT NULL AND photos = '[]'",
                                   (student["attempt_id"],)).rowcount
            if removed:
                conn.execute("UPDATE attempts SET essay = NULL WHERE id = ?", (student["attempt_id"],))
    return row["tg_id"]


def waiting_offline_essays(test_id: int) -> int:
    """Yuborilgan, lekin titul hali skanerlanmagan (natijaga biriktirilmagan) esselar soni."""
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM offline_essays e WHERE e.test_id = ? AND NOT EXISTS ("
            "SELECT 1 FROM students s WHERE s.test_id = e.test_id AND s.number = e.number AND s.attempt_id IS NOT NULL)",
            (test_id,),
        ).fetchone()["n"]


def set_essay_text(attempt_id: int, text: str, status: str) -> None:
    """Natijaga esse matni (yangi yoki almashtirilgan) — AI navbatiga qayta qo'yiladi."""
    with connect() as conn:
        updated = conn.execute(
            "UPDATE essays SET text = ?, photos = '[]', status = ?, tries = 0, error = NULL, ai_score = NULL, "
            "ai_result = NULL, graded_at = NULL WHERE attempt_id = ?",
            (text, status, attempt_id),
        ).rowcount
        if not updated:
            conn.execute("INSERT INTO essays (attempt_id, text, photos, status, created_at) VALUES (?, ?, '[]', ?, ?)",
                         (attempt_id, text, status, _now()))


def retry_written(attempt_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE written_jobs SET status = 'pending', tries = 0, error = NULL, notified = 0 "
                     "WHERE attempt_id = ? AND image IS NOT NULL", (attempt_id,))


def written_status(test_id: int) -> dict:
    """Testning yozma javob navbati: {"pending": n, "done": n, "failed": n}."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM written_jobs WHERE test_id = ? GROUP BY status", (test_id,)
        ).fetchall()
    counts = {"pending": 0, "done": 0, "failed": 0}
    counts.update({row["status"]: row["n"] for row in rows})
    return counts


def take_written_notice(test_id: int) -> dict | None:
    """Navbat bo'shagan bo'lsa — hali xabar qilinmagan natijalar soni (va ular "xabar qilingan" bo'ladi)."""
    with connect() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM written_jobs WHERE test_id = ? AND status = 'pending'", (test_id,)
        ).fetchone()["n"]
        if pending:
            return None
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM written_jobs WHERE test_id = ? AND notified = 0 GROUP BY status",
            (test_id,),
        ).fetchall()
        conn.execute("UPDATE written_jobs SET notified = 1 WHERE test_id = ? AND notified = 0", (test_id,))
    counts = {row["status"]: row["n"] for row in rows}
    return counts if counts else None


def retry_essay(attempt_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE essays SET status = 'pending', tries = 0, error = NULL WHERE attempt_id = ?", (attempt_id,))


# ---------- Mini App ----------

def set_test_channels(test_id: int, channels: list[dict]) -> None:
    """Testning majburiy kanallari: [{"chat_id", "title", "url"}] — o'quvchi obuna bo'lmaguncha testni ocholmaydi."""
    with connect() as conn:
        conn.execute("UPDATE tests SET channels = ? WHERE id = ?", (json.dumps(channels, ensure_ascii=False), test_id))


def set_sheet_style(test_id: int, style: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE tests SET sheet_style = ? WHERE id = ?", (style, test_id))


def set_check_mode(test_id: int, check_mode: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE tests SET check_mode = ? WHERE id = ?", (check_mode, test_id))


def set_mode(test_id: int, mode: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE tests SET mode = ? WHERE id = ?", (mode, test_id))


def set_essay_photos(attempt_id: int, photos: list[str], status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE essays SET photos = ?, status = ?, tries = 0, error = NULL WHERE attempt_id = ?",
                     (json.dumps(photos), status, attempt_id))


def set_theme(test_id: int, theme: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE tests SET theme = ? WHERE id = ?", (theme, test_id))


def attempts_of_user(tg_id: int, limit: int = 50) -> list[dict]:
    """O'quvchining topshirgan testlari (yangi birinchi)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT a.id, a.variant, a.created_at, t.code, t.title, t.fmt, t.closed, t.theme "
            "FROM attempts a JOIN tests t ON t.id = a.test_id WHERE a.tg_id = ? ORDER BY a.id DESC LIMIT ?",
            (tg_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------- Majburiy obuna ----------

def list_channels() -> list[dict]:
    with connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM channels ORDER BY added_at").fetchall()]


def add_channel(chat_id: int, title: str, url: str, added_by: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO channels (chat_id, title, url, added_by, added_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title, url = excluded.url",
            (chat_id, title, url, added_by, _now()),
        )


def remove_channel(chat_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM channels WHERE chat_id = ?", (chat_id,))


def stats() -> dict:
    with connect() as conn:
        return {
            name: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for name, table in (("users", "users"), ("tests", "tests"), ("attempts", "attempts"))
        }


# ---------- Oflayn test: o'quvchilar ----------

def offline_tg_id(test_id: int, number: int) -> int:
    """Telegram'siz o'quvchi uchun ichki ID (manfiy — xabar yuborilmaydi, attempts jadvalida takrorlanmaydi)."""
    return -(test_id * 100000 + number)


def list_students(test_id: int) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM students WHERE test_id = ? ORDER BY number",
                                              (test_id,)).fetchall()]


def get_student(test_id: int, student_id: int | None = None, number: int | None = None) -> dict | None:
    with connect() as conn:
        if student_id is not None:
            row = conn.execute("SELECT * FROM students WHERE id = ? AND test_id = ?", (student_id, test_id)).fetchone()
        else:
            row = conn.execute("SELECT * FROM students WHERE number = ? AND test_id = ?", (number, test_id)).fetchone()
    return _row(row)


def add_students(test_id: int, names: list[str], variants: list[str], numbers: list[int] | None = None) -> list[dict]:
    """Ro'yxatga qo'shish. numbers — titulda bo'yalgan ID (bo'sh bo'lsa yoki band bo'lsa — keyingi bo'sh raqam)."""
    with connect() as conn:
        taken = {row["number"] for row in conn.execute("SELECT number FROM students WHERE test_id = ?", (test_id,))}
        for index, (name, variant) in enumerate(zip(names, variants)):
            wanted = numbers[index] if numbers and index < len(numbers) else None
            if not wanted or wanted in taken:
                wanted = max(taken, default=0) + 1
            taken.add(wanted)
            conn.execute("INSERT INTO students (test_id, number, name, variant) VALUES (?, ?, ?, ?)",
                         (test_id, wanted, name, variant))
    return list_students(test_id)


def update_student(test_id: int, student_id: int, name: str | None = None, variant: str | None = None) -> None:
    with connect() as conn:
        if name is not None:
            conn.execute("UPDATE students SET name = ? WHERE id = ? AND test_id = ?", (name, student_id, test_id))
        if variant is not None:
            conn.execute("UPDATE students SET variant = ? WHERE id = ? AND test_id = ?", (variant, student_id, test_id))


def delete_student(test_id: int, student_id: int) -> None:
    with connect() as conn:
        row = conn.execute("SELECT attempt_id FROM students WHERE id = ? AND test_id = ?", (student_id, test_id)).fetchone()
        if row and row["attempt_id"]:
            conn.execute("DELETE FROM attempts WHERE id = ?", (row["attempt_id"],))
        conn.execute("DELETE FROM students WHERE id = ? AND test_id = ?", (student_id, test_id))


def save_sheet_marks(attempt_id: int, marks: str) -> None:
    """Titul o'qilganda: doirachalar joyi (surat file_id'si — bot albomni yuborgach)."""
    with connect() as conn:
        conn.execute("INSERT INTO attempt_sheets (attempt_id, marks, created_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(attempt_id) DO UPDATE SET marks = excluded.marks, file_id = NULL", (attempt_id, marks, _now()))


def set_sheet_file(attempt_id: int, file_id: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO attempt_sheets (attempt_id, file_id, created_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(attempt_id) DO UPDATE SET file_id = excluded.file_id", (attempt_id, file_id, _now()))


def get_sheet(attempt_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM attempt_sheets WHERE attempt_id = ?", (attempt_id,)).fetchone())


def move_attempt(attempt_id: int, tg_id: int, name: str) -> None:
    """Natija boshqa o'quvchiga (ID xato o'qilgan): ichki ID va ism; eski ro'yxat bog'lanishi uziladi."""
    with connect() as conn:
        conn.execute("UPDATE students SET attempt_id = NULL WHERE attempt_id = ?", (attempt_id,))
        conn.execute("UPDATE attempts SET tg_id = ?, student_name = ? WHERE id = ?", (tg_id, name, attempt_id))


def rename_attempt(attempt_id: int, name: str) -> None:
    """Natijadagi ism (va ro'yxatdagi o'quvchi ismi ham — bog'langan bo'lsa)."""
    with connect() as conn:
        conn.execute("UPDATE attempts SET student_name = ? WHERE id = ?", (name, attempt_id))
        conn.execute("UPDATE students SET name = ? WHERE attempt_id = ?", (name, attempt_id))


def link_student_attempt(student_id: int, attempt_id: int | None) -> None:
    with connect() as conn:
        conn.execute("UPDATE students SET attempt_id = ? WHERE id = ?", (attempt_id, student_id))


def delete_attempt(attempt_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE students SET attempt_id = NULL WHERE attempt_id = ?", (attempt_id,))
        conn.execute("DELETE FROM attempts WHERE id = ?", (attempt_id,))
