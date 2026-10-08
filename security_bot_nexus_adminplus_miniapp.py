import asyncio
import html
import os
import re
import secrets
import sqlite3
import hashlib
import hmac
import io
import ipaddress
import mimetypes
import socket
import ssl
import struct
import tempfile
import urllib.error
import urllib.request
import urllib.parse
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message, PreCheckoutQuery, BufferedInputFile, WebAppInfo
try:
    from aiohttp import web
except Exception:
    web = None
try:
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.telegram import TelegramAPIServer
except Exception:
    AiohttpSession = None
    TelegramAPIServer = None

BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_BOT_TOKEN_HERE")
BOT_API_BASE = os.getenv("BOT_API_BASE", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
MINIAPP_URL = os.getenv("MINIAPP_URL", "").strip()
MINIAPP_HOST = os.getenv("MINIAPP_HOST", "127.0.0.1")
MINIAPP_PORT = int(os.getenv("MINIAPP_PORT", "8080"))
DB_NAME = os.getenv("DB_NAME", "security_bot.db")

FREE_SCANS = 3
MONTH_SCANS = 0  # 0 = безлимитный Premium
MONTH_PRICE_STARS = 15
LIFETIME_PRICE_STARS = 25
MONTH_DAYS = 30
FREE_FILE_SCANS = 4
FREE_FILE_MAX_BYTES = 20 * 1024 * 1024
TELEGRAM_STANDARD_MAX_BYTES = 20 * 1024 * 1024
PREMIUM_FILE_MAX_BYTES = 2 * 1024 * 1024 * 1024
ARCHIVE_ENTRY_LIMIT = 5000
# Optional: androguard (APK), py7zr (7Z), rarfile + unrar/bsdtar (RAR).

# Temporary input states. They live only while the bot process is running.
admin_promo_state = {}
broadcast_state = {}
admin_user_state = {}
admin_manage_state = {}
channel_state = {}

# Aiogram dispatcher
dp = Dispatcher()


def db():
    con = sqlite3.connect(DB_NAME)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db():
    con = db(); cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        scans_used INTEGER NOT NULL DEFAULT 0,
        bonus_scans INTEGER NOT NULL DEFAULT 0,
        premium_type TEXT NOT NULL DEFAULT 'free',
        premium_until TEXT,
        premium_scans_used INTEGER NOT NULL DEFAULT 0,
        notify_enabled INTEGER NOT NULL DEFAULT 1,
        blocked INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT '',
        tg_username TEXT NOT NULL DEFAULT '',
        first_name TEXT NOT NULL DEFAULT '',
        last_name TEXT NOT NULL DEFAULT ''
    )""")
    cols = {r[1] for r in cur.execute("PRAGMA table_info(users)")}
    migrations = {
        "bonus_scans": "ALTER TABLE users ADD COLUMN bonus_scans INTEGER NOT NULL DEFAULT 0",
        "notify_enabled": "ALTER TABLE users ADD COLUMN notify_enabled INTEGER NOT NULL DEFAULT 1",
        "blocked": "ALTER TABLE users ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0",
        "created_at": "ALTER TABLE users ADD COLUMN created_at TEXT NOT NULL DEFAULT ''",
        "tg_username": "ALTER TABLE users ADD COLUMN tg_username TEXT NOT NULL DEFAULT ''",
        "first_name": "ALTER TABLE users ADD COLUMN first_name TEXT NOT NULL DEFAULT ''",
        "last_name": "ALTER TABLE users ADD COLUMN last_name TEXT NOT NULL DEFAULT ''",
        "file_scans_used": "ALTER TABLE users ADD COLUMN file_scans_used INTEGER NOT NULL DEFAULT 0",
    }
    for col, sql in migrations.items():
        if col not in cols:
            cur.execute(sql)
    cur.execute("""CREATE TABLE IF NOT EXISTS scans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        url TEXT NOT NULL,
        score INTEGER NOT NULL,
        verdict TEXT NOT NULL,
        reasons TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )""")
    scan_cols = {r[1] for r in cur.execute("PRAGMA table_info(scans)")}
    if "reasons" not in scan_cols:
        cur.execute("ALTER TABLE scans ADD COLUMN reasons TEXT NOT NULL DEFAULT ''")
    cur.execute("""CREATE TABLE IF NOT EXISTS favorites (
        user_id INTEGER NOT NULL,
        url TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(user_id, url)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS promo_codes (
        code TEXT PRIMARY KEY,
        plan TEXT NOT NULL,
        max_uses INTEGER NOT NULL,
        used_count INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        scans_limit INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )""")
    promo_cols = {r[1] for r in cur.execute("PRAGMA table_info(promo_codes)")}
    if "scans_limit" not in promo_cols:
        cur.execute("ALTER TABLE promo_codes ADD COLUMN scans_limit INTEGER NOT NULL DEFAULT 0")
    if "created_at" not in promo_cols:
        cur.execute("ALTER TABLE promo_codes ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
    cur.execute("""CREATE TABLE IF NOT EXISTS promo_activations (
        code TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        activated_at TEXT NOT NULL,
        PRIMARY KEY(code, user_id)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS file_scans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        file_name TEXT NOT NULL,
        file_size INTEGER NOT NULL DEFAULT 0,
        detected_type TEXT NOT NULL DEFAULT '',
        sha256 TEXT NOT NULL DEFAULT '',
        md5 TEXT NOT NULL DEFAULT '',
        risk_score INTEGER NOT NULL DEFAULT 100,
        verdict TEXT NOT NULL DEFAULT '',
        report TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS file_favorites (
        user_id INTEGER NOT NULL,
        file_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(user_id,file_id)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY,
        role TEXT NOT NULL DEFAULT 'admin',
        created_at TEXT NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS bot_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""")
    cur.execute("INSERT OR IGNORE INTO admins(user_id, role, created_at) VALUES(?,?,?)", (ADMIN_ID, 'owner', datetime.now(timezone.utc).isoformat()))
    cur.execute("INSERT OR IGNORE INTO bot_settings(key,value) VALUES('maintenance','0')")
    cur.execute("INSERT OR IGNORE INTO bot_settings(key,value) VALUES('channel_url','https://t.me/logovolinkvora')")
    con.commit(); con.close()


def ensure_user(user_id: int, tg_username: str = "", first_name: str = "", last_name: str = ""):
    con = db(); cur = con.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cur.execute("INSERT OR IGNORE INTO users(user_id, created_at, tg_username, first_name, last_name) VALUES (?, ?, ?, ?, ?)",
                (user_id, now, tg_username or "", first_name or "", last_name or ""))
    cur.execute("UPDATE users SET tg_username=?, first_name=?, last_name=? WHERE user_id=?",
                (tg_username or "", first_name or "", last_name or "", user_id))
    con.commit(); con.close()


def get_user(user_id: int):
    ensure_user(user_id)
    con = db(); row = con.execute("SELECT user_id, scans_used, bonus_scans, premium_type, premium_until, premium_scans_used, notify_enabled, blocked FROM users WHERE user_id=?", (user_id,)).fetchone(); con.close()
    return row


def is_blocked(user_id: int):
    return bool(get_user(user_id)[7])


def set_plan(user_id: int, plan: str, until=None):
    ensure_user(user_id); con = db()
    con.execute("UPDATE users SET premium_type=?, premium_until=?, premium_scans_used=0 WHERE user_id=?", (plan, until, user_id))
    con.commit(); con.close()


def normalize_expired_plan(user_id: int):
    u = get_user(user_id)
    if u[3] == "month" and (not u[4] or datetime.now(timezone.utc) >= datetime.fromisoformat(u[4])):
        con = db(); con.execute("UPDATE users SET premium_type='free', premium_until=NULL, premium_scans_used=0 WHERE user_id=?", (user_id,)); con.commit(); con.close()


def scans_left(user_id: int):
    normalize_expired_plan(user_id); u = get_user(user_id)
    if u[3] == "lifetime": return None
    if u[3] == "month": return None
    return u[2] + max(0, FREE_SCANS - u[1])


def consume_scan(user_id: int):
    normalize_expired_plan(user_id); u = get_user(user_id)
    if u[3] == "lifetime": return True, "lifetime"
    con = db()
    # Bonus checks are consumed first, so a code like +7 makes Free 3 become 10.
    if u[2] > 0:
        con.execute("UPDATE users SET bonus_scans=bonus_scans-1 WHERE user_id=?", (user_id,)); source = "bonus"
    elif u[3] == "month":
        return True, "premium"
    elif u[1] < FREE_SCANS:
        con.execute("UPDATE users SET scans_used=scans_used+1 WHERE user_id=?", (user_id,)); source = "free"
    else:
        con.close(); return False, None
    con.commit(); con.close(); return True, source


def status_text(user_id: int):
    normalize_expired_plan(user_id); u = get_user(user_id)
    if u[3] == "lifetime": base = "♾ Premium навсегда\n🔎 Проверки: безлимит"
    elif u[3] == "month":
        until = u[4] or "—"
        try: until = datetime.fromisoformat(until).strftime("%d.%m.%Y %H:%M UTC")
        except Exception: pass
        base = f"💎 Premium на месяц\n🔎 Проверки: безлимит\n⏳ До: {until}"
    else: base = f"🆓 Free\n🔎 Бесплатных проверок: {max(0, FREE_SCANS-u[1])}/{FREE_SCANS}"
    bonus = f"\n🎟 Бонусных проверок: {u[2]}" if u[2] else ""
    return base + bonus


SUSPICIOUS_WORDS = {"login","verify","bonus","free","gift","wallet","password","security","account","claim","crypto","signin","update","unlock","airdrop","support"}
SHORTENERS = {"bit.ly","tinyurl.com","t.co","goo.gl","is.gd","cutt.ly","rb.gy"}


def analyze_url(url: str, deep=False):
    original = url.strip()
    if not original: return 0, "❌ Пустая ссылка", ["Ссылка отсутствует"]
    normalized = original if original.startswith(("http://","https://")) else "https://" + original
    try:
        p = urlparse(normalized); host = (p.hostname or "").lower(); text = (p.path + "?" + p.query).lower()
    except Exception:
        return 0, "❌ Некорректная ссылка", ["Не удалось разобрать URL"]
    score = 100; reasons=[]
    if p.scheme != "https": score -= 20; reasons.append("Используется HTTP вместо HTTPS")
    if not host: score -= 40; reasons.append("Не найден домен")
    if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", host): score -= 25; reasons.append("Используется IP-адрес вместо домена")
    if "xn--" in host: score -= 15; reasons.append("Обнаружен punycode-домен")
    if "@" in normalized: score -= 25; reasons.append("В URL есть символ @")
    if host.count(".") >= 3: score -= 10; reasons.append("Много уровней поддоменов")
    if len(normalized) > 180: score -= 10; reasons.append("Очень длинный URL")
    if p.query.count("&") >= 4: score -= 5; reasons.append("Много параметров")
    if p.port not in (None, 80, 443): score -= 10; reasons.append(f"Нестандартный порт: {p.port}")
    if host in SHORTENERS: score -= 12; reasons.append("Сервис сокращения ссылок скрывает конечный адрес")
    if host.startswith("www-") or host.count("-") >= 4: score -= 5; reasons.append("Подозрительная структура домена")
    found = sorted(w for w in SUSPICIOUS_WORDS if w in text or w in host)
    if found: score -= min(25, 4*len(found)); reasons.append("Подозрительные слова: " + ", ".join(found))
    if deep:
        if len(set(host.replace(".",""))) <= 4 and len(host) > 12: score -= 8; reasons.append("Домен выглядит необычно из-за повторяющихся символов")
        digits = sum(c.isdigit() for c in host); letters = sum(c.isalpha() for c in host)
        if digits >= 4 and digits > letters * 0.35: score -= 7; reasons.append("Много цифр в домене")
        if host.count("-") >= 2: score -= 5; reasons.append("Несколько дефисов в домене")
        if len(p.path) > 100: score -= 6; reasons.append("Очень длинный путь")
        if "%" in normalized: score -= 3; reasons.append("URL содержит кодированные символы")
    score=max(0,min(100,score))
    verdict = "🟢 Низкий риск" if score>=80 else "🟡 Средний риск" if score>=50 else "🔴 Высокий риск"
    if not reasons: reasons=["Явных подозрительных признаков по локальным правилам не найдено"]
    return score, verdict, reasons


def save_scan(user_id,url,score,verdict,reasons):
    con=db(); cur=con.cursor(); cur.execute("INSERT INTO scans(user_id,url,score,verdict,reasons,created_at) VALUES(?,?,?,?,?,?)",(user_id,url,score,verdict,"\n".join(reasons),datetime.now(timezone.utc).isoformat())); scan_id=cur.lastrowid; con.commit(); con.close(); return scan_id


def main_kb():
    
    con=db(); row=con.execute("SELECT value FROM bot_settings WHERE key='channel_url'").fetchone(); con.close()
    channel_url=row[0] if row and row[0] else "https://t.me/logovolinkvora"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔎 Проверить ссылку",callback_data="check")],[InlineKeyboardButton(text="📁 Проверить файл",callback_data="file_check")],[InlineKeyboardButton(text="💎 Подписка",callback_data="subscription")],[InlineKeyboardButton(text="📜 История",callback_data="history"),InlineKeyboardButton(text="📊 Моя статистика",callback_data="mystats")],[InlineKeyboardButton(text="⭐ Избранное",callback_data="favorites")],[InlineKeyboardButton(text="👤 Мой аккаунт",callback_data="account")],[InlineKeyboardButton(text="📢 Наш канал",url=channel_url)],[InlineKeyboardButton(text="⚙️ Настройки",callback_data="settings")]])


def subscription_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"💎 Месяц — {MONTH_PRICE_STARS} ⭐ / безлимит",callback_data="buy_month")],[InlineKeyboardButton(text=f"♾ Навсегда — {LIFETIME_PRICE_STARS} ⭐",callback_data="buy_lifetime")],[InlineKeyboardButton(text="🎟 Ввести промокод",callback_data="promo")],[InlineKeyboardButton(text="⬅️ Назад",callback_data="back")]])


def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎟 Создать промокод",callback_data="admin_create_promo"), InlineKeyboardButton(text="📋 Промокоды",callback_data="admin_promos")],
        [InlineKeyboardButton(text="📢 Рассылка",callback_data="admin_broadcast"), InlineKeyboardButton(text="📊 Статистика",callback_data="admin_stats")],
        [InlineKeyboardButton(text="👥 Пользователи",callback_data="admin_users"), InlineKeyboardButton(text="👑 Администраторы",callback_data="admin_admins")],
        [InlineKeyboardButton(text="🚨 Аварийка",callback_data="admin_maintenance"), InlineKeyboardButton(text="⚙️ Настройки",callback_data="admin_settings")],
        [InlineKeyboardButton(text="📢 Управление каналом",callback_data="admin_channel")],
        [InlineKeyboardButton(text="🖥 Открыть Mini App",web_app=WebAppInfo(url=MINIAPP_URL))] if MINIAPP_URL else [],
        [InlineKeyboardButton(text="⬅️ Главное меню",callback_data="back")]])


def promo_plan_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📅 Месяц — 15 проверок",callback_data="admin_plan_month")],[InlineKeyboardButton(text="♾ Навсегда — безлимит",callback_data="admin_plan_lifetime")],[InlineKeyboardButton(text="🎟 Бонус проверок (+N)",callback_data="admin_plan_scans")],[InlineKeyboardButton(text="❌ Отмена",callback_data="admin_cancel")]])


def broadcast_confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Отправить всем",callback_data="broadcast_confirm")],[InlineKeyboardButton(text="❌ Отмена",callback_data="broadcast_cancel")]])


def result_kb(scan_id, user_id):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔍 Глубокий анализ",callback_data=f"deep:{scan_id}"),InlineKeyboardButton(text="🔄 Проверить снова",callback_data=f"recheck:{scan_id}")],[InlineKeyboardButton(text="⭐ В избранное",callback_data=f"fav:{scan_id}")],[InlineKeyboardButton(text="⬅️ Меню",callback_data="back")]])


def is_admin(uid):
    if uid == ADMIN_ID:
        return True
    con=db(); row=con.execute("SELECT 1 FROM admins WHERE user_id=?",(uid,)).fetchone(); con.close()
    return bool(row)

def is_owner(uid): return uid == ADMIN_ID

def maintenance_enabled():
    con=db(); row=con.execute("SELECT value FROM bot_settings WHERE key='maintenance'").fetchone(); con.close()
    return bool(row and row[0]=='1')


@dp.message(CommandStart())
async def start(message:Message):
    ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    if is_blocked(message.from_user.id): return
    if maintenance_enabled() and not is_admin(message.from_user.id):
        await message.answer("🛠 Сейчас проводятся технические работы. Попробуй позже.")
        return
    await message.answer("🛡 SecurityBot Nexus\n\nПроверяй ссылки на подозрительные признаки.",reply_markup=main_kb())


@dp.message(Command("admin"))
async def admin(message:Message):
    if not is_admin(message.from_user.id): await message.answer("⛔ Нет доступа."); return
    await message.answer("⚙️ Админ-панель",reply_markup=admin_kb())


@dp.callback_query(F.data=="account")
async def account(c:CallbackQuery):
    ensure_user(c.from_user.id, c.from_user.username, c.from_user.first_name, c.from_user.last_name)
    normalize_expired_plan(c.from_user.id)
    u=get_user(c.from_user.id)
    con=db()
    created=con.execute("SELECT created_at FROM users WHERE user_id=?",(c.from_user.id,)).fetchone()[0]
    total=con.execute("SELECT COUNT(*) FROM scans WHERE user_id=?",(c.from_user.id,)).fetchone()[0]
    file_total=con.execute("SELECT COUNT(*) FROM file_scans WHERE user_id=?",(c.from_user.id,)).fetchone()[0]
    promo_total=con.execute("SELECT COUNT(*) FROM promo_activations WHERE user_id=?",(c.from_user.id,)).fetchone()[0]
    con.close()
    try:
        reg=datetime.fromisoformat(created).astimezone(timezone.utc).strftime("%d.%m.%Y")
    except Exception:
        reg="неизвестно"
    name=" ".join(x for x in [u and None] if x)
    display_name=" ".join(x for x in [c.from_user.first_name, c.from_user.last_name] if x) or "не указано"
    username=f"@{c.from_user.username}" if c.from_user.username else "не указан"
    plan="💎 Premium" if u[3] in ("month","lifetime") else "🆓 Free"
    if u[3]=="lifetime": premium_until="♾ Навсегда"; scan_limit="♾ Безлимит"
    elif u[3]=="month": premium_until=datetime.fromisoformat(u[4]).astimezone(timezone.utc).strftime("%d.%m.%Y") if u[4] else "—"; scan_limit="♾ Безлимит"
    else: premium_until="—"; scan_limit=f"{max(0, scans_left(c.from_user.id))} осталось"
    bonus=u[2]
    text=(f"👤 <b>Мой аккаунт</b>\n\n"
          f"🆔 Telegram ID: <code>{c.from_user.id}</code>\n"
          f"👤 Имя: {html.escape(display_name)}\n"
          f"🔹 Username: {html.escape(username)}\n"
          f"📅 В SecurityBot Nexus с: {reg}\n\n"
          f"📌 Тариф: {plan}\n"
          f"⏳ Premium до: {premium_until}\n"
          f"🔎 Проверок: {scan_limit}\n"
          f"🎁 Бонусных проверок: {bonus}\n"
          f"📊 Всего проверок ссылок: {total}\n"
          f"📁 Проверено файлов: {file_total}\n"
          f"🎟 Активировано промокодов: {promo_total}\n\n"
          f"⚠️ Настоящую дату создания Telegram-аккаунта бот получить не может. Здесь указана дата регистрации именно в SecurityBot Nexus.")
    await c.message.edit_text(text,parse_mode="HTML",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Подписка",callback_data="subscription")],[InlineKeyboardButton(text="⬅️ Назад",callback_data="back")]])); await c.answer()


@dp.callback_query(F.data=="subscription")
async def subscription(c:CallbackQuery):
    if is_blocked(c.from_user.id): return
    await c.message.edit_text("💎 Подписка\n\n"+status_text(c.from_user.id)+"\n\n💎 Месяц: 15 ⭐, 30 дней, безлимитные проверки\n♾ Навсегда: 25 ⭐, безлимит",reply_markup=subscription_kb()); await c.answer()


@dp.callback_query(F.data=="buy_month")
async def buy_month(c:CallbackQuery,bot:Bot):
    await bot.send_invoice(chat_id=c.from_user.id,title="Premium на месяц",description="30 дней и безлимитные проверки.",payload="premium_month",currency="XTR",prices=[LabeledPrice(label="Premium на месяц",amount=MONTH_PRICE_STARS)]); await c.answer()


@dp.callback_query(F.data=="buy_lifetime")
async def buy_lifetime(c:CallbackQuery,bot:Bot):
    await bot.send_invoice(chat_id=c.from_user.id,title="Premium навсегда",description="Безлимитные проверки.",payload="premium_lifetime",currency="XTR",prices=[LabeledPrice(label="Premium навсегда",amount=LIFETIME_PRICE_STARS)]); await c.answer()


@dp.pre_checkout_query()
async def pre_checkout(q:PreCheckoutQuery,bot:Bot): await bot.answer_pre_checkout_query(q.id,ok=True)


@dp.message(F.successful_payment)
async def payment(message:Message):
    if message.successful_payment.invoice_payload=="premium_month":
        until=(datetime.now(timezone.utc)+timedelta(days=MONTH_DAYS)).isoformat(); set_plan(message.from_user.id,"month",until)
        await message.answer("✅ Premium на месяц активирован!\n\n🔎 Проверки: безлимит\n⏳ 30 дней",reply_markup=main_kb())
    elif message.successful_payment.invoice_payload=="premium_lifetime":
        set_plan(message.from_user.id,"lifetime"); await message.answer("♾ Premium навсегда активирован!\n\n🔎 Проверки: безлимит",reply_markup=main_kb())


@dp.callback_query(F.data=="check")
async def check(c:CallbackQuery): await c.message.answer("🔎 Отправь ссылку сообщением."); await c.answer()


@dp.callback_query(F.data=="history")
async def history(c:CallbackQuery):
    con=db(); rows=con.execute("SELECT id,url,score,verdict,created_at FROM scans WHERE user_id=? ORDER BY id DESC" if is_premium(c.from_user.id) else "SELECT id,url,score,verdict,created_at FROM scans WHERE user_id=? ORDER BY id DESC LIMIT 10",(c.from_user.id,)).fetchall(); con.close()
    if not rows: await c.message.edit_text("📜 История пуста.",reply_markup=main_kb()); await c.answer(); return
    lines=["📜 Последние проверки:\n"]
    for sid,url,score,verdict,dt in rows:
        lines.append(f"#{sid} • <code>{html.escape(url)}</code>\n  {score}/100 — {verdict} — {datetime.fromisoformat(dt).strftime('%d.%m %H:%M')}")
    await c.message.edit_text("\n".join(lines),reply_markup=main_kb()); await c.answer()


@dp.callback_query(F.data=="mystats")
async def mystats(c:CallbackQuery):
    con=db(); total=con.execute("SELECT COUNT(*) FROM scans WHERE user_id=?",(c.from_user.id,)).fetchone()[0]; risky=con.execute("SELECT COUNT(*) FROM scans WHERE user_id=? AND score<50",(c.from_user.id,)).fetchone()[0]; fav=con.execute("SELECT COUNT(*) FROM favorites WHERE user_id=?",(c.from_user.id,)).fetchone()[0]; con.close()
    await c.message.edit_text(f"📊 Моя статистика\n\n🔎 Проверено ссылок: {total}\n🔴 Высокого риска: {risky}\n⭐ В избранном: {fav}\n\n{status_text(c.from_user.id)}",reply_markup=main_kb()); await c.answer()


@dp.callback_query(F.data=="favorites")
async def favorites(c:CallbackQuery):
    con=db(); rows=con.execute("SELECT url FROM favorites WHERE user_id=? ORDER BY created_at DESC LIMIT 20",(c.from_user.id,)).fetchall(); con.close()
    if not rows: await c.message.edit_text("⭐ Избранное пусто.",reply_markup=main_kb()); await c.answer(); return
    await c.message.edit_text("⭐ Избранные ссылки:\n\n"+"\n".join(f"• <code>{html.escape(r[0])}</code>" for r in rows),reply_markup=main_kb()); await c.answer()


@dp.callback_query(F.data=="settings")
async def settings(c:CallbackQuery):
    u=get_user(c.from_user.id); text="🔔 Уведомления: ВКЛ" if u[6] else "🔕 Уведомления: ВЫКЛ"
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔕 Выключить" if u[6] else "🔔 Включить",callback_data="toggle_notify")],[InlineKeyboardButton(text="⬅️ Назад",callback_data="back")]])
    await c.message.edit_text("⚙️ Настройки\n\n"+text,reply_markup=kb); await c.answer()


@dp.callback_query(F.data=="toggle_notify")
async def toggle_notify(c:CallbackQuery):
    u=get_user(c.from_user.id); con=db(); con.execute("UPDATE users SET notify_enabled=? WHERE user_id=?",(0 if u[6] else 1,c.from_user.id)); con.commit(); con.close(); await settings(c)


@dp.callback_query(F.data.startswith("fav:"))
async def add_favorite(c:CallbackQuery):
    sid=int(c.data.split(":",1)[1]); con=db(); row=con.execute("SELECT url FROM scans WHERE id=? AND user_id=?",(sid,c.from_user.id)).fetchone()
    if not row: con.close(); await c.answer("Проверка не найдена",show_alert=True); return
    con.execute("INSERT OR IGNORE INTO favorites(user_id,url,created_at) VALUES(?,?,?)",(c.from_user.id,row[0],datetime.now(timezone.utc).isoformat())); con.commit(); con.close(); await c.answer("⭐ Добавлено в избранное")


async def run_scan_message(message:Message,url:str,deep=False):
    if maintenance_enabled() and not is_admin(message.from_user.id):
        await message.answer("🛠 Сейчас проводятся технические работы. Попробуй позже."); return
    left=scans_left(message.from_user.id)
    if left==0: await message.answer("❌ Лимит проверок закончился.\n\nОткрой 💎 Подписка или используй промокод.",reply_markup=subscription_kb()); return
    ok,source=consume_scan(message.from_user.id)
    if not ok: await message.answer("❌ Лимит проверок закончился.",reply_markup=subscription_kb()); return
    score,verdict,reasons=analyze_url(url,deep=deep); sid=save_scan(message.from_user.id,url,score,verdict,reasons)
    remaining=scans_left(message.from_user.id); remain="♾ безлимит" if remaining is None else str(remaining)
    reason_text="\n".join("• "+html.escape(r) for r in reasons)
    mode="Глубокий анализ" if deep else "Быстрый анализ"
    await message.answer(f"🛡 <b>{mode}</b>\n\n🔗 <code>{html.escape(url)}</code>\n📊 Оценка: {score}/100\n📌 {verdict}\n\nПризнаки:\n{reason_text}\n\n🔎 Осталось проверок: {remain}\n\n⚠️ Локальная эвристическая проверка не гарантирует безопасность сайта.",reply_markup=result_kb(sid,message.from_user.id),parse_mode="HTML")


@dp.callback_query(F.data.startswith("recheck:"))
async def recheck(c:CallbackQuery):
    sid=int(c.data.split(":",1)[1]); con=db(); row=con.execute("SELECT url FROM scans WHERE id=? AND user_id=?",(sid,c.from_user.id)).fetchone(); con.close()
    if not row: await c.answer("Проверка не найдена",show_alert=True); return
    await run_scan_message(c.message,row[0]); await c.answer()


# ---------------- ADMIN PROMOS ----------------
@dp.callback_query(F.data=="admin_create_promo")
async def admin_create(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    admin_promo_state[c.from_user.id]={}
    await c.message.edit_text("🎟 Создание промокода\n\nВыбери тип:",reply_markup=promo_plan_kb()); await c.answer()


@dp.callback_query(F.data.in_({"admin_plan_month","admin_plan_lifetime","admin_plan_scans"}))
async def admin_plan(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    plan={"admin_plan_month":"month","admin_plan_lifetime":"lifetime","admin_plan_scans":"scans"}[c.data]
    admin_promo_state[c.from_user.id]={"plan":plan}
    if plan=="scans": prompt="Сколько БОНУСНЫХ проверок получит каждый?\nНапример: 7\n(Free 3 + 7 = 10 проверок)"
    else: prompt="Сколько активаций разрешить?\nНапример: 10\n0 = безлимит"
    await c.message.edit_text(prompt); await c.answer()


async def finish_promo(message:Message,state):
    plan=state["plan"]; con=db(); code=secrets.token_hex(5).upper(); max_uses=-1 if state["max_uses"]==0 else state["max_uses"]
    con.execute("INSERT INTO promo_codes(code,plan,max_uses,used_count,active,scans_limit,created_at) VALUES(?,?,?,?,?,?,?)",(code,"promo" if plan=="scans" else plan,max_uses,0,1,state.get("bonus",0),datetime.now(timezone.utc).isoformat())); con.commit(); con.close(); admin_promo_state.pop(message.from_user.id,None)
    uses="безлимит" if max_uses==-1 else str(max_uses)
    if plan=="scans": desc=f"🎟 Код: <code>{code}</code>\n➕ Бонус: +{state['bonus']} проверок каждому\n👥 Активаций: {uses}"
    elif plan=="month": desc=f"🎟 Код: <code>{code}</code>\n💎 Premium: месяц\n👥 Активаций: {uses}"
    else: desc=f"🎟 Код: <code>{code}</code>\n♾ Premium: навсегда\n👥 Активаций: {uses}"
    await message.answer("✅ Промокод создан!\n\n"+desc,parse_mode="HTML",reply_markup=admin_kb())


# ---------------- FILE + DEEP URL SECURITY ----------------

def get_setting(key, default=""):
    con=db(); row=con.execute("SELECT value FROM bot_settings WHERE key=?",(key,)).fetchone(); con.close()
    return row[0] if row else default


def is_premium(uid):
    normalize_expired_plan(uid); return get_user(uid)[3] in ("month","lifetime")


def file_limit(uid):
    return None if is_premium(uid) else max(0, FREE_FILE_SCANS - get_user(uid)[0] * 0 + get_user_file_scans(uid))


def get_user_file_scans(uid):
    con=db(); cols={r[1] for r in con.execute("PRAGMA table_info(users)")};
    if "file_scans_used" not in cols: con.close(); return 0
    row=con.execute("SELECT COALESCE(file_scans_used,0) FROM users WHERE user_id=?",(uid,)).fetchone(); con.close(); return row[0] if row else 0


def consume_file_scan(uid):
    if is_premium(uid): return True
    used=get_user_file_scans(uid)
    if used>=FREE_FILE_SCANS: return False
    con=db(); con.execute("UPDATE users SET file_scans_used=COALESCE(file_scans_used,0)+1 WHERE user_id=?",(uid,)); con.commit(); con.close(); return True


def sha256_md5(data):
    return hashlib.sha256(data).hexdigest(), hashlib.md5(data).hexdigest()


def detect_file_type(data, filename=""):
    name=(filename or "").lower()
    if data.startswith(b"MZ"): return "PE/Windows executable (MZ)"
    if data.startswith(b"PK\x03\x04") or data.startswith(b"PK\x05\x06") or data.startswith(b"PK\x07\x08"):
        if name.endswith(".apk"): return "Android APK (ZIP container)"
        if name.endswith((".docx",".xlsx",".pptx")): return "Office Open XML (ZIP container)"
        return "ZIP archive"
    if data.startswith(b"Rar!\x1a\x07"): return "RAR archive"
    if data.startswith(b"7z\xbc\xaf\x27\x1c"): return "7-Zip archive"
    if data.startswith(b"\x25PDF-"): return "PDF document"
    if data.startswith(b"\x89PNG\r\n\x1a\n"): return "PNG image"
    if data.startswith(b"\xff\xd8\xff"): return "JPEG image"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"): return "GIF image"
    if data.startswith(b"OggS"): return "OGG media"
    if data.startswith(b"ID3") or (len(data)>2 and data[:2]==b"\xff\xfb"): return "MP3 audio"
    if data.startswith(b"\x1f\x8b"): return "GZIP archive"
    if data.startswith(b"\x7fELF"): return "ELF executable"
    return mimetypes.guess_type(filename or "")[0] or "Неизвестный бинарный/текстовый тип"


def double_extension(filename):
    parts=(filename or "").lower().split(".")
    if len(parts)<3: return False
    dangerous={"exe","scr","bat","cmd","com","msi","dll","js","vbs","ps1","jar","apk","sh"}
    return parts[-1] in dangerous and parts[-2] not in dangerous


def archive_names(data, kind):
    names=[]; note=""
    try:
        if kind.startswith("ZIP") or kind.startswith("Android APK") or kind.startswith("Office"):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                infos=z.infolist()[:ARCHIVE_ENTRY_LIMIT]
                names=[i.filename for i in infos]
                if len(z.infolist())>ARCHIVE_ENTRY_LIMIT: note=f"Архив слишком большой: показаны первые {ARCHIVE_ENTRY_LIMIT} записей"
        elif kind.startswith("RAR"):
            try:
                import rarfile
                with rarfile.RarFile(io.BytesIO(data)) as r: names=r.namelist()[:ARCHIVE_ENTRY_LIMIT]
            except Exception: note="RAR распознан, но модуль/распаковщик RAR недоступен."
        elif kind.startswith("7-Zip"):
            try:
                import py7zr
                with tempfile.NamedTemporaryFile(suffix=".7z") as f:
                    f.write(data); f.flush()
                    with py7zr.SevenZipFile(f.name,mode="r") as z: names=z.getnames()[:ARCHIVE_ENTRY_LIMIT]
            except Exception: note="7Z распознан, но модуль py7zr недоступен."
    except Exception as e: note="Не удалось безопасно прочитать содержимое архива: "+str(e)[:120]
    return names,note


def apk_report(data, names):
    permissions=[]; components=[]; package="не определён"; notes=[]
    if "AndroidManifest.xml" not in names:
        notes.append("AndroidManifest.xml не найден — это может быть невалидный APK.")
        return package,permissions,components,notes
    try:
        from androguard.core.apk import APK
        with tempfile.NamedTemporaryFile(suffix=".apk") as f:
            f.write(data); f.flush(); a=APK(f.name)
            package=a.get_package() or package
            permissions=sorted(set(a.get_permissions() or []))
            components=[]
            for label,getter in (("Activity",a.get_activities),("Service",a.get_services),("Receiver",a.get_receivers),("Provider",a.get_providers)):
                for x in getter() or []: components.append(f"{label}: {x}")
    except Exception:
        notes.append("Для package name/permissions/components нужен optional пакет androguard. APK всё равно проверен по структуре, имени и хешам.")
    return package,permissions,components,notes


def analyze_file(data, filename):
    sha,md5=sha256_md5(data); kind=detect_file_type(data,filename); names,note=archive_names(data,kind)
    reasons=[]; score=100; dangerous_perms=[]; package="—"; components=[]
    low=(filename or "").lower()
    if double_extension(filename): score-=30; reasons.append("Обнаружено двойное расширение, похожее на маскировку исполняемого файла")
    ext=low.rsplit(".",1)[-1] if "." in low else ""
    executable={"exe","scr","bat","cmd","com","msi","dll","js","vbs","ps1","jar","sh"}
    if ext in executable: score-=18; reasons.append("Файл относится к потенциально исполняемым типам")
    if ext=="apk" or kind.startswith("Android APK"):
        package,perms,components,notes=apk_report(data,names); dangerous_perms=[p for p in perms if any(x in p for x in ("READ_SMS","SEND_SMS","RECORD_AUDIO","CAMERA","ACCESS_FINE_LOCATION","ACCESS_COARSE_LOCATION","READ_CONTACTS","WRITE_CONTACTS","REQUEST_INSTALL_PACKAGES","SYSTEM_ALERT_WINDOW","BIND_ACCESSIBILITY_SERVICE","RECEIVE_BOOT_COMPLETED"))]
        if dangerous_perms: score-=min(30,5+4*len(dangerous_perms)); reasons.append("Есть потенциально чувствительные/опасные Android-разрешения")
        suspicious_components=[x for x in components if any(k in x.lower() for k in ("accessibility","deviceadmin","overlay","proxy","vpnservice"))]
        if suspicious_components: score-=15; reasons.append("Найдены компоненты, требующие дополнительной проверки")
        notes += [f"Подозрительный компонент: {x}" for x in suspicious_components]
    suspicious_names=[n for n in names if any(k in n.lower() for k in ("autorun","startup","keylog","stealer","password","credential","token","wallet","miner","cryptominer"))]
    if suspicious_names: score-=min(30,5+len(suspicious_names)*4); reasons.append("Внутри архива найдены подозрительно названные файлы")
    if note: reasons.append(note)
    if kind.startswith("PE/") and not low.endswith((".exe",".dll",".scr",".sys",".msi")): score-=20; reasons.append("Реальный тип файла не совпадает с обычным расширением")
    score=max(0,min(100,score)); verdict="🟢 Низкий риск" if score>=80 else "🟡 Средний риск" if score>=50 else "🔴 Высокий риск"
    if not reasons: reasons=["Явных подозрительных признаков по локальной эвристике не найдено"]
    return {"sha256":sha,"md5":md5,"kind":kind,"score":score,"verdict":verdict,"reasons":reasons,"names":names,"package":package,"permissions":dangerous_perms or [],"all_permissions":locals().get("perms",[]),"components":components,"notes":notes if 'notes' in locals() else []}


def save_file_scan(uid,filename,size,report):
    con=db(); cur=con.cursor(); cur.execute("INSERT INTO file_scans(user_id,file_name,file_size,detected_type,sha256,md5,risk_score,verdict,report,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(uid,filename,size,report["kind"],report["sha256"],report["md5"],report["score"],report["verdict"],str(report),datetime.now(timezone.utc).isoformat())); sid=cur.lastrowid; con.commit(); con.close(); return sid


def file_result_kb(sid):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📤 Экспорт отчёта",callback_data=f"file_report:{sid}"),InlineKeyboardButton(text="⭐ В избранное",callback_data=f"file_fav:{sid}")],[InlineKeyboardButton(text="⬅️ Меню",callback_data="back")]])


def format_file_report(r, filename, size):
    lines=[f"📦 {filename}",f"📏 Размер: {size/1024/1024:.2f} MB",f"🧩 Реальный тип: {r['kind']}",f"🔑 SHA-256: {r['sha256']}",f"🔑 MD5: {r['md5']}",f"📊 Риск: {r['score']}/100",f"📌 {r['verdict']}"]
    if r.get('package') and r['package']!="—": lines.append(f"🏢 Package name: {r['package']}")
    if r.get('all_permissions'): lines.append("🔐 Разрешения:\n"+"\n".join("• "+x for x in r['all_permissions'][:100]))
    if r.get('permissions'): lines.append("⚠️ Опасные разрешения:\n"+"\n".join("• "+x for x in r['permissions']))
    if r.get('components'): lines.append("🧩 Компоненты:\n"+"\n".join("• "+x for x in r['components'][:100]))
    if r.get('names'): lines.append("🔎 Подозрительные/архивные записи:\n"+"\n".join("• "+x for x in r['names'][:30]))
    if r.get('reasons'): lines.append("⚠️ Признаки:\n"+"\n".join("• "+x for x in r['reasons']))
    if r.get('notes'): lines.append("ℹ️ Примечания:\n"+"\n".join("• "+x for x in r['notes']))
    lines.append("⚠️ Это эвристический анализ, а не гарантия отсутствия вредоносного кода.")
    return "\n\n".join(lines)


def resolve_public_host(host):
    try:
        infos=socket.getaddrinfo(host,None,type=socket.SOCK_STREAM)
        ips=sorted({x[4][0] for x in infos})
        public=[]
        for ip in ips:
            obj=ipaddress.ip_address(ip)
            if obj.is_private or obj.is_loopback or obj.is_link_local or obj.is_reserved or obj.is_multicast: return False,ips,"Домен указывает на непубличный/служебный IP"
            public.append(ip)
        return True,public,""
    except Exception as e: return False,[],"Не удалось разрешить домен: "+str(e)[:120]


def inspect_url_network(url):
    normalized=url if url.startswith(("http://","https://")) else "https://"+url
    chain=[normalized]; current=normalized; final=None; status=None; headers={}; notes=[]
    for _ in range(5):
        p=urllib.parse.urlparse(current)
        if not p.hostname: break
        ok,ips,msg=resolve_public_host(p.hostname)
        if not ok: notes.append(msg); break
        req=urllib.request.Request(current,method="HEAD",headers={"User-Agent":"SecurityBot-Nexus/1.0"})
        try:
            with urllib.request.urlopen(req,timeout=8) as resp:
                status=resp.status; final=resp.geturl(); headers=dict(resp.headers); chain.append(final) if final!=current else None
                if final==current: break
                current=final
        except urllib.error.HTTPError as e:
            status=e.code; final=e.geturl() or current; headers=dict(e.headers or {}); loc=headers.get("Location")
            if loc and len(chain)<6:
                nxt=urllib.parse.urljoin(current,loc); chain.append(nxt); current=nxt; continue
            break
        except Exception as e:
            notes.append("Сетевой запрос не завершён: "+str(e)[:120]); break
    return chain,final,status,headers,notes


def ssl_info(url):
    p=urllib.parse.urlparse(url if url.startswith("http") else "https://"+url); host=p.hostname; port=p.port or 443
    if p.scheme!="https" or not host: return "HTTPS не используется или домен не определён."
    try:
        ctx=ssl.create_default_context()
        with socket.create_connection((host,port),timeout=6) as sock:
            with ctx.wrap_socket(sock,server_hostname=host) as ss:
                cert=ss.getpeercert(); subject=dict(x[0] for x in cert.get("subject",[])); issuer=dict(x[0] for x in cert.get("issuer",[]));
                return f"TLS: {ss.version()}\nСертификат: {subject.get('commonName','—')}\nИздатель: {issuer.get('commonName','—')}\nДействует: {cert.get('notBefore','—')} — {cert.get('notAfter','—')}"
    except Exception as e: return "SSL/TLS: не удалось получить сертификат: "+str(e)[:120]


def deep_url_report(url):
    score,verdict,reasons=analyze_url(url,deep=True); p=urllib.parse.urlparse(url if url.startswith("http") else "https://"+url); host=p.hostname or ""; chain,final,status,headers,notes=inspect_url_network(url); ssltext=ssl_info(url)
    if final and final!=url: reasons.append("Обнаружен редирект на конечный URL")
    if len(chain)>=4: score-=8; reasons.append("Цепочка редиректов длиннее обычной")
    if final and any(x in final.lower() for x in ("login","verify","password","account","signin","wallet","claim")): score-=8; reasons.append("Конечный адрес содержит признаки страницы входа/получения данных")
    score=max(0,min(100,score)); verdict="🟢 Низкий риск" if score>=80 else "🟡 Средний риск" if score>=50 else "🔴 Высокий риск"
    return score,verdict,reasons,chain,final,status,ssltext,notes


@dp.callback_query(F.data=="file_check")
async def file_check(c:CallbackQuery):
    u=get_user(c.from_user.id); used=get_user_file_scans(c.from_user.id); limit="безлимит" if is_premium(c.from_user.id) else f"{max(0,FREE_FILE_SCANS-used)} осталось"
    await c.message.answer(f"📁 Отправь файл документом.\n\n🆓 Free: до 20 MB, {FREE_FILE_SCANS} проверки.\n💎 Premium: безлимитные проверки.\n📦 До 2 ГБ: настрой Local Bot API Server и укажи BOT_API_BASE; обычный Telegram Bot API ограничивает скачивание примерно 20 MB.\n\nОсталось: {limit}"); await c.answer()


@dp.message(F.document)
async def document_scan(message:Message,bot:Bot):
    uid=message.from_user.id; ensure_user(uid,message.from_user.username,message.from_user.first_name,message.from_user.last_name)
    if is_blocked(uid) and not is_admin(uid): return
    if maintenance_enabled() and not is_admin(uid): await message.answer("🛠 Сейчас проводятся технические работы. Попробуй позже."); return
    size=message.document.file_size or 0; premium=is_premium(uid); max_bytes=PREMIUM_FILE_MAX_BYTES if premium else FREE_FILE_MAX_BYTES
    if size>max_bytes: await message.answer(f"❌ Файл слишком большой. Лимит для твоего тарифа: {max_bytes/1024/1024:.0f} MB."); return
    if not consume_file_scan(uid): await message.answer("❌ Лимит файловых проверок Free закончился. Открой 💎 Подписка.",reply_markup=subscription_kb()); return
    try:
        tgfile=await bot.get_file(message.document.file_id)
        bio=io.BytesIO(); await bot.download_file(tgfile.file_path,bio); data=bio.getvalue()
    except Exception as e:
        await message.answer("❌ Не удалось скачать файл через Telegram Bot API. Для файлов больше стандартного лимита нужен Local Bot API Server.\n\nОшибка: "+str(e)[:200]); return
    report=analyze_file(data,message.document.file_name or "file")
    sid=save_file_scan(uid,message.document.file_name or "file",len(data),report)
    text=format_file_report(report,message.document.file_name or "file",len(data))
    await message.answer(text,reply_markup=file_result_kb(sid),parse_mode=None)


@dp.callback_query(F.data.startswith("file_report:"))
async def file_report(c:CallbackQuery):
    if not is_premium(c.from_user.id): await c.answer("Экспорт отчётов доступен в Premium",show_alert=True); return
    sid=int(c.data.split(":",1)[1]); con=db(); row=con.execute("SELECT file_name,file_size,report FROM file_scans WHERE id=? AND user_id=?",(sid,c.from_user.id)).fetchone(); con.close()
    if not row: await c.answer("Отчёт не найден",show_alert=True); return
    content=f"SecurityBot Nexus — отчёт\n\nФайл: {row[0]}\nРазмер: {row[1]} bytes\n\n{row[2]}".encode("utf-8")
    await c.message.answer_document(BufferedInputFile(content,filename=f"security_report_{sid}.txt")); await c.answer()


@dp.callback_query(F.data.startswith("file_fav:"))
async def file_fav(c:CallbackQuery):
    sid=int(c.data.split(":",1)[1]); con=db(); row=con.execute("SELECT id FROM file_scans WHERE id=? AND user_id=?",(sid,c.from_user.id)).fetchone()
    if not row: con.close(); await c.answer("Файл не найден",show_alert=True); return
    con.execute("INSERT OR IGNORE INTO file_favorites(user_id,file_id,created_at) VALUES(?,?,?)",(c.from_user.id,sid,datetime.now(timezone.utc).isoformat())); con.commit(); con.close(); await c.answer("⭐ Файл добавлен в избранное")


@dp.callback_query(F.data.startswith("deep:") )
async def deep_network(c:CallbackQuery):
    sid=int(c.data.split(":",1)[1]); con=db(); row=con.execute("SELECT url FROM scans WHERE id=? AND user_id=?",(sid,c.from_user.id)).fetchone(); con.close()
    if not row: await c.answer("Проверка не найдена",show_alert=True); return
    if scans_left(c.from_user.id)==0: await c.answer("Нет доступных проверок",show_alert=True); return
    ok,_=consume_scan(c.from_user.id)
    if not ok: await c.answer("Нет доступных проверок",show_alert=True); return
    await c.message.answer("⏳ Глубокий анализ: редиректы, домен, TLS...")
    score,verdict,reasons,chain,final,status,ssltext,notes=deep_url_report(row[0])
    sid2=save_scan(c.from_user.id,row[0],score,verdict,reasons)
    chain_text="\n".join(f"{i+1}. {x}" for i,x in enumerate(chain[:6]))
    await c.message.answer(f"🧠 <b>Глубокий анализ</b>\n\n🔗 <code>{html.escape(row[0])}</code>\n📊 Риск: {score}/100\n📌 {verdict}\n\n🔄 Редиректы:\n{html.escape(chain_text)}\n\n🌍 Домен: {html.escape(urlparse(row[0]).hostname or '—')}\n📡 HTTP: {status or '—'}\n🔐 SSL/TLS:\n<code>{html.escape(ssltext)}</code>\n\n⚠️ Признаки:\n"+"\n".join("• "+html.escape(x) for x in reasons+notes)+"\n\n⚠️ Сетевой анализ не является гарантией безопасности.",parse_mode="HTML",reply_markup=result_kb(sid2,c.from_user.id)); await c.answer()


@dp.message()
async def messages(message:Message,bot:Bot):
    uid=message.from_user.id; text=(message.text or "").strip(); ensure_user(uid)
    if is_blocked(uid) and not is_admin(uid): return
    if is_admin(uid) and uid in channel_state:
        if text.lower() in ("отмена","cancel"):
            channel_state.pop(uid,None); await message.answer("❌ Отменено",reply_markup=admin_kb()); return
        if not re.match(r"^https?://t\.me/",text):
            await message.answer("❌ Нужна ссылка вида https://t.me/..."); return
        con=db(); con.execute("INSERT OR REPLACE INTO bot_settings(key,value) VALUES('channel_url',?)",(text,)); con.commit(); con.close(); channel_state.pop(uid,None); await message.answer("✅ Ссылка канала обновлена.",reply_markup=admin_kb()); return
    if is_admin(uid) and isinstance(admin_user_state.get(uid),tuple) and admin_user_state[uid][0]=="add_checks":
        try: value=int(text)
        except: await message.answer("❌ Введи число."); return
        if value<=0: await message.answer("❌ Число должно быть больше 0."); return
        target=admin_user_state[uid][1]; con=db(); con.execute("UPDATE users SET bonus_scans=bonus_scans+? WHERE user_id=?",(value,target)); con.commit(); con.close(); admin_user_state.pop(uid,None); await message.answer(f"✅ Пользователю {target} добавлено +{value} проверок.",reply_markup=admin_kb()); return
    # Admin promo wizard.
    if is_admin(uid) and uid in admin_promo_state:
        st=admin_promo_state[uid]
        try: value=int(text)
        except: await message.answer("❌ Введи целое число."); return
        if value<0: await message.answer("❌ Число не может быть отрицательным."); return
        if st["plan"]=="scans" and "bonus" not in st:
            if value<=0: await message.answer("❌ Бонус должен быть больше 0."); return
            st["bonus"]=value; st["stage"]="uses"; await message.answer("👥 Теперь сколько АКТИВАЦИЙ у промокода?\nНапример: 20\n0 = безлимит"); return
        st["max_uses"]=value; await finish_promo(message,st); return
    # Broadcast input.
    if is_admin(uid) and uid in broadcast_state:
        if text.lower()=="отмена": broadcast_state.pop(uid,None); await message.answer("❌ Отменено",reply_markup=admin_kb()); return
        broadcast_state[uid]=text; con=db(); count=con.execute("SELECT COUNT(*) FROM users WHERE blocked=0").fetchone()[0]; con.close()
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Отправить всем",callback_data="broadcast_confirm")],[InlineKeyboardButton(text="❌ Отмена",callback_data="broadcast_cancel")]])
        await message.answer(f"📢 Предпросмотр:\n\n{text}\n\n👥 Получателей: {count}",reply_markup=kb); return
    # Admin user search/block wizard.
    if is_admin(uid) and uid in admin_user_state:
        try: target=int(text)
        except: await message.answer("❌ Нужен Telegram ID."); return
        u=get_user(target); con=db(); total=con.execute("SELECT COUNT(*) FROM scans WHERE user_id=?",(target,)).fetchone()[0]; con.close()
        await message.answer(f"👤 ID: {target}\n📊 Проверок ссылок: {total}\n📁 Файлов: {get_user_file_scans(target)}\n{status_text(target)}\n🚫 Заблокирован: {'да' if u[7] else 'нет'}",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚫 Заблокировать" if not u[7] else "✅ Разблокировать",callback_data=f"toggleblock:{target}")],[InlineKeyboardButton(text="💎 Выдать месяц",callback_data=f"grant_month:{target}"),InlineKeyboardButton(text="♾ Выдать навсегда",callback_data=f"grant_life:{target}")],[InlineKeyboardButton(text="➕ Добавить проверки",callback_data=f"add_checks:{target}" )],[InlineKeyboardButton(text="⬅️ Админка",callback_data="admin_home")]])); admin_user_state.pop(uid,None); return
    if is_owner(uid) and uid in admin_manage_state:
        try: target=int(text)
        except: await message.answer("❌ Нужен Telegram ID."); return
        action=admin_manage_state.pop(uid)
        if action=="add":
            con=db(); con.execute("INSERT OR REPLACE INTO admins(user_id,role,created_at) VALUES(?,?,?)",(target,"admin",datetime.now(timezone.utc).isoformat())); con.commit(); con.close(); await message.answer(f"✅ Пользователь {target} получил права администратора.",reply_markup=admin_kb())
        else:
            if target==ADMIN_ID: await message.answer("❌ Главного админа удалить нельзя.",reply_markup=admin_kb()); return
            con=db(); con.execute("DELETE FROM admins WHERE user_id=?",(target,)); con.commit(); con.close(); await message.answer(f"✅ Права администратора у {target} сняты.",reply_markup=admin_kb())
        return
    # Promo redemption.
    con=db(); promo=con.execute("SELECT code,plan,max_uses,used_count,active,scans_limit FROM promo_codes WHERE code=?",(text.upper(),)).fetchone()
    if promo:
        code,plan,max_uses,used,active,bonus=promo
        if not active: await message.answer("❌ Промокод отключён."); return
        if max_uses!=-1 and used>=max_uses: await message.answer("❌ Лимит активаций исчерпан."); return
        already=con.execute("SELECT 1 FROM promo_activations WHERE code=? AND user_id=?",(code,uid)).fetchone()
        if already: con.close(); await message.answer("❌ Ты уже активировал этот промокод."); return
        con.execute("INSERT INTO promo_activations(code,user_id,activated_at) VALUES(?,?,?)",(code,uid,datetime.now(timezone.utc).isoformat()))
        con.execute("UPDATE promo_codes SET used_count=used_count+1 WHERE code=?",(code,)); con.commit(); con.close()
        if plan=="promo":
            con=db(); con.execute("UPDATE users SET bonus_scans=bonus_scans+? WHERE user_id=?",(bonus,uid)); con.commit(); con.close(); await message.answer(f"🎉 Промокод активирован!\n\n➕ +{bonus} проверок\n🔎 Теперь доступно: {scans_left(uid)} проверок",reply_markup=main_kb()); return
        if plan=="month": set_plan(uid,"month",(datetime.now(timezone.utc)+timedelta(days=30)).isoformat()); await message.answer("🎉 Premium на месяц активирован!",reply_markup=main_kb()); return
        set_plan(uid,"lifetime"); await message.answer("🎉 Premium навсегда активирован!",reply_markup=main_kb()); return
    # URL.
    if text.startswith(("http://","https://","www.")) or "." in text:
        await run_scan_message(message,text); return
    if text.startswith("/admin"):
        return
    await message.answer("Отправь ссылку, например https://example.com",reply_markup=main_kb())



# ---------------- ADMIN PLUS ----------------
@dp.callback_query(F.data=="admin_maintenance")
async def admin_maintenance(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    state="ВКЛЮЧЕНА" if maintenance_enabled() else "ВЫКЛЮЧЕНА"
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔴 Выключить аварийный режим" if maintenance_enabled() else "🟢 Включить аварийный режим",callback_data="toggle_maintenance")],[InlineKeyboardButton(text="⬅️ Админка",callback_data="admin_home")]])
    await c.message.edit_text(f"🚨 Аварийный режим\n\nСейчас: {state}\n\nКогда включён, обычные пользователи не смогут пользоваться ботом.",reply_markup=kb); await c.answer()

@dp.callback_query(F.data=="toggle_maintenance")
async def toggle_maintenance(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    enabled=maintenance_enabled()
    con=db(); con.execute("UPDATE bot_settings SET value=? WHERE key='maintenance'",('0' if enabled else '1',)); con.commit(); con.close()
    await admin_maintenance(c)

@dp.callback_query(F.data=="admin_settings")
async def admin_settings(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    text=f"⚙️ Настройки бота\n\n🚨 Аварийный режим: {'ВКЛ' if maintenance_enabled() else 'ВЫКЛ'}\n🆔 Главный админ: {ADMIN_ID}"
    await c.message.edit_text(text,reply_markup=admin_kb()); await c.answer()

@dp.callback_query(F.data=="admin_admins")
async def admin_admins(c:CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer("Только главный админ",show_alert=True); return
    con=db(); rows=con.execute("SELECT user_id,role FROM admins ORDER BY user_id").fetchall(); con.close()
    lines=["👑 Администраторы\n"]
    for uid,role in rows: lines.append(f"• {uid} — {'главный админ' if role=='owner' else 'админ'}")
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Выдать админа",callback_data="admin_add")],[InlineKeyboardButton(text="❌ Забрать админку",callback_data="admin_remove")],[InlineKeyboardButton(text="⬅️ Админка",callback_data="admin_home")]])
    await c.message.edit_text("\n".join(lines),reply_markup=kb); await c.answer()

@dp.callback_query(F.data.in_({"admin_add","admin_remove"}))
async def admin_manage_start(c:CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer("Только главный админ",show_alert=True); return
    admin_manage_state[c.from_user.id]="add" if c.data=="admin_add" else "remove"
    await c.message.edit_text(("➕ Отправь Telegram ID пользователя, которому выдать админку." if c.data=="admin_add" else "❌ Отправь Telegram ID пользователя, у которого забрать админку.")); await c.answer()


@dp.callback_query(F.data=="admin_channel")
async def admin_channel(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    url=get_setting("channel_url","https://t.me/logovolinkvora")
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✏️ Изменить ссылку",callback_data="channel_edit")],[InlineKeyboardButton(text="⬅️ Админка",callback_data="admin_home")]])
    await c.message.edit_text(f"📢 Управление каналом\n\nТекущая ссылка:\n{html.escape(url)}\n\nЭта ссылка используется кнопкой «Наш канал».",reply_markup=kb); await c.answer()

@dp.callback_query(F.data=="channel_edit")
async def channel_edit(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    channel_state[c.from_user.id]=True; await c.message.edit_text("📢 Отправь новую ссылку канала, например https://t.me/logovolinkvora"); await c.answer()

@dp.callback_query(F.data.startswith("grant_month:"))
async def grant_month(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    target=int(c.data.split(":",1)[1]); set_plan(target,"month",(datetime.now(timezone.utc)+timedelta(days=30)).isoformat()); await c.answer("Premium на месяц выдан"); await c.message.answer(f"💎 Пользователю {target} выдан Premium на 30 дней.")

@dp.callback_query(F.data.startswith("grant_life:"))
async def grant_life(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    target=int(c.data.split(":",1)[1]); set_plan(target,"lifetime"); await c.answer("Premium навсегда выдан"); await c.message.answer(f"♾ Пользователю {target} выдан Premium навсегда.")

@dp.callback_query(F.data.startswith("add_checks:"))
async def add_checks(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    target=int(c.data.split(":",1)[1]); admin_user_state[c.from_user.id]=("add_checks",target); await c.message.answer("➕ Сколько бонусных проверок добавить? Отправь число."); await c.answer()

# ---------------- ADMIN CALLBACKS ----------------
@dp.callback_query(F.data=="admin_home")
async def admin_home(c:CallbackQuery):
    if is_admin(c.from_user.id): await c.message.edit_text("⚙️ Админ-панель",reply_markup=admin_kb())
    await c.answer()


@dp.callback_query(F.data=="admin_cancel")
async def admin_cancel(c:CallbackQuery):
    admin_promo_state.pop(c.from_user.id,None); await c.message.edit_text("❌ Отменено",reply_markup=admin_kb()); await c.answer()


@dp.callback_query(F.data=="admin_promos")
async def admin_promos(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    con=db(); rows=con.execute("SELECT code,plan,max_uses,used_count,active,scans_limit FROM promo_codes ORDER BY created_at DESC LIMIT 20").fetchall(); con.close()
    if not rows: text="📋 Промокодов пока нет."
    else:
        lines=["📋 Последние промокоды\n"]
        for code,plan,mx,used,active,bonus in rows:
            lim="∞" if mx==-1 else str(mx); kind=f"+{bonus} проверок" if plan=="promo" else "месяц" if plan=="month" else "навсегда"; lines.append(f"• <code>{code}</code> — {kind} — {used}/{lim} — {'активен' if active else 'выкл.'}")
        text="\n".join(lines)
    await c.message.edit_text(text,parse_mode="HTML",reply_markup=admin_kb()); await c.answer()


@dp.callback_query(F.data=="admin_broadcast")
async def admin_broadcast(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    broadcast_state[c.from_user.id]=None; await c.message.edit_text("📢 Напиши текст объявления.\n\nДля отмены напиши: отмена"); await c.answer()


@dp.callback_query(F.data=="broadcast_cancel")
async def broadcast_cancel(c:CallbackQuery): broadcast_state.pop(c.from_user.id,None); await c.message.edit_text("❌ Рассылка отменена",reply_markup=admin_kb()); await c.answer()


@dp.callback_query(F.data=="broadcast_confirm")
async def broadcast_confirm(c:CallbackQuery,bot:Bot):
    if not is_admin(c.from_user.id) or c.from_user.id not in broadcast_state: return
    text=broadcast_state.pop(c.from_user.id,None)
    if not text: await c.answer("Нет текста",show_alert=True); return
    con=db(); ids=[r[0] for r in con.execute("SELECT user_id FROM users WHERE blocked=0")]; con.close(); sent=failed=0
    await c.message.edit_text("📢 Рассылка запущена...")
    for uid in ids:
        try: await bot.send_message(uid,text); sent+=1; await asyncio.sleep(0.05)
        except Exception: failed+=1
    await c.message.edit_text(f"✅ Рассылка завершена\n\n📨 Отправлено: {sent}\n❌ Ошибок: {failed}",reply_markup=admin_kb()); await c.answer()


@dp.callback_query(F.data=="admin_stats")
async def admin_stats(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    con=db(); users=con.execute("SELECT COUNT(*) FROM users").fetchone()[0]; blocked=con.execute("SELECT COUNT(*) FROM users WHERE blocked=1").fetchone()[0]; scans=con.execute("SELECT COUNT(*) FROM scans").fetchone()[0]; prem=con.execute("SELECT COUNT(*) FROM users WHERE premium_type IN ('month','lifetime')").fetchone()[0]; bonuses=con.execute("SELECT COALESCE(SUM(bonus_scans),0) FROM users").fetchone()[0]
    files=con.execute("SELECT COUNT(*) FROM file_scans").fetchone()[0]
    day=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat(); week=(datetime.now(timezone.utc)-timedelta(days=7)).isoformat(); month=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
    new_day=con.execute("SELECT COUNT(*) FROM users WHERE created_at>=?",(day,)).fetchone()[0]; new_week=con.execute("SELECT COUNT(*) FROM users WHERE created_at>=?",(week,)).fetchone()[0]; new_month=con.execute("SELECT COUNT(*) FROM users WHERE created_at>=?",(month,)).fetchone()[0]
    con.close()
    await c.message.edit_text(f"📊 Статистика\n\n👥 Пользователей: {users}\n🚫 Заблокировано: {blocked}\n🔎 Проверок ссылок: {scans}\n📁 Проверок файлов: {files}\n💎 Premium: {prem}\n🎟 Бонусных проверок: {bonuses}\n\n📈 Новые пользователи:\n• За день: {new_day}\n• За неделю: {new_week}\n• За месяц: {new_month}",reply_markup=admin_kb()); await c.answer()


@dp.callback_query(F.data=="admin_users")
async def admin_users(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    admin_user_state[c.from_user.id]=True; await c.message.edit_text("👥 Пользователи\n\nОтправь Telegram ID пользователя, чтобы посмотреть его данные и заблокировать/разблокировать."); await c.answer()


@dp.callback_query(F.data.startswith("toggleblock:"))
async def toggleblock(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    target=int(c.data.split(":",1)[1]); u=get_user(target); con=db(); con.execute("UPDATE users SET blocked=? WHERE user_id=?",(0 if u[7] else 1,target)); con.commit(); con.close(); await c.message.edit_text(f"{'🚫 Пользователь заблокирован.' if not u[7] else '✅ Пользователь разблокирован.'}",reply_markup=admin_kb()); await c.answer()


@dp.callback_query(F.data=="back")
async def back(c:CallbackQuery):
    await c.message.edit_text("🛡 SecurityBot Nexus",reply_markup=main_kb()); await c.answer()


# ---------- Admin Mini App API ----------
def _validate_webapp_init_data(init_data: str):
    if not init_data or not BOT_TOKEN:
        return None
    try:
        from urllib.parse import parse_qsl
        data = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = data.pop("hash", "")
        if not received_hash:
            return None
        check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not secrets.compare_digest(calculated, received_hash):
            return None
        auth_date = int(data.get("auth_date", "0"))
        if auth_date and abs(int(datetime.now(timezone.utc).timestamp()) - auth_date) > 86400:
            return None
        return data
    except Exception:
        return None


def _webapp_admin(request):
    data = _validate_webapp_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if not data:
        return None
    try:
        uid = int(__import__("json").loads(data.get("user", "{}"))["id"])
    except Exception:
        return None
    return uid if is_admin(uid) else None


async def miniapp_index(request):
    if not MINIAPP_URL:
        return web.Response(text="Mini App URL is not configured. Set MINIAPP_URL.", status=503)
    try:
        with open("admin_miniapp.html", "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html")
    except FileNotFoundError:
        return web.Response(text="admin_miniapp.html not found", status=500)


async def miniapp_stats(request):
    if _webapp_admin(request) is None: return web.json_response({"error":"forbidden"}, status=403)
    con=db()
    users=con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    blocked=con.execute("SELECT COUNT(*) FROM users WHERE blocked=1").fetchone()[0]
    scans=con.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
    files=con.execute("SELECT COUNT(*) FROM file_scans").fetchone()[0]
    premium=con.execute("SELECT COUNT(*) FROM users WHERE premium_type IN ('month','lifetime')").fetchone()[0]
    day=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat(); week=(datetime.now(timezone.utc)-timedelta(days=7)).isoformat(); month=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
    new_day=con.execute("SELECT COUNT(*) FROM users WHERE created_at>=?",(day,)).fetchone()[0]
    new_week=con.execute("SELECT COUNT(*) FROM users WHERE created_at>=?",(week,)).fetchone()[0]
    new_month=con.execute("SELECT COUNT(*) FROM users WHERE created_at>=?",(month,)).fetchone()[0]
    con.close()
    return web.json_response({"users":users,"blocked":blocked,"scans":scans,"files":files,"premium":premium,"new_day":new_day,"new_week":new_week,"new_month":new_month,"maintenance":maintenance_enabled()})


async def miniapp_users(request):
    if _webapp_admin(request) is None: return web.json_response({"error":"forbidden"}, status=403)
    con=db(); rows=con.execute("SELECT user_id,tg_username,first_name,premium_type,blocked,created_at FROM users ORDER BY created_at DESC LIMIT 100").fetchall(); con.close()
    return web.json_response({"users":[{"id":r[0],"username":r[1],"name":r[2],"premium":r[3],"blocked":bool(r[4]),"created_at":r[5]} for r in rows]})


async def miniapp_toggle_maintenance(request):
    if _webapp_admin(request) is None: return web.json_response({"error":"forbidden"}, status=403)
    data=await request.json(); value="1" if bool(data.get("enabled")) else "0"
    con=db(); con.execute("INSERT OR REPLACE INTO bot_settings(key,value) VALUES('maintenance',?)",(value,)); con.commit(); con.close()
    return web.json_response({"ok":True,"maintenance":value=="1"})


async def miniapp_toggle_block(request):
    if _webapp_admin(request) is None: return web.json_response({"error":"forbidden"}, status=403)
    data=await request.json(); target=int(data.get("user_id"))
    if target == ADMIN_ID: return web.json_response({"error":"cannot block owner"}, status=400)
    con=db(); row=con.execute("SELECT blocked FROM users WHERE user_id=?",(target,)).fetchone()
    if not row: con.close(); return web.json_response({"error":"user not found"}, status=404)
    new=0 if row[0] else 1; con.execute("UPDATE users SET blocked=? WHERE user_id=?",(new,target)); con.commit(); con.close()
    return web.json_response({"ok":True,"blocked":bool(new)})


def start_miniapp_server():
    if not web or not MINIAPP_URL:
        return None
    app=web.Application()
    app.router.add_get("/", miniapp_index)
    app.router.add_get("/api/stats", miniapp_stats)
    app.router.add_get("/api/users", miniapp_users)
    app.router.add_post("/api/maintenance", miniapp_toggle_maintenance)
    app.router.add_post("/api/block", miniapp_toggle_block)
    return app


async def main():
    init_db()
    if BOT_TOKEN=="PASTE_BOT_TOKEN_HERE": raise RuntimeError("BOT_TOKEN is not configured.")
    if ADMIN_ID==123456789: raise RuntimeError("ADMIN_ID is not configured.")
    
    if BOT_API_BASE and AiohttpSession and TelegramAPIServer:
        session=AiohttpSession(api=TelegramAPIServer.from_base(BOT_API_BASE))
        bot=Bot(BOT_TOKEN,session=session)
    else:
        bot=Bot(BOT_TOKEN)
    if MINIAPP_URL and web:
        app=start_miniapp_server()
        runner=web.AppRunner(app)
        await runner.setup()
        site=web.TCPSite(runner, MINIAPP_HOST, MINIAPP_PORT)
        await site.start()
        print(f"Mini App server: http://{MINIAPP_HOST}:{MINIAPP_PORT}")
    elif MINIAPP_URL and not web:
        raise RuntimeError("aiohttp is required for Mini App server.")
    await dp.start_polling(bot)


if __name__=="__main__": asyncio.run(main())
