import asyncio
import imaplib
import json
import os
import smtplib
import sqlite3
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email import message_from_bytes
from email.header import decode_header
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    bot_token: str
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    check_interval_seconds: int
    smtp_use_starttls: bool
    db_path: str
    mailcow_api_url: str
    mailcow_api_key: str


@dataclass
class Session:
    user_id: int
    email: str
    password: str
    last_seen_uid: int


@dataclass
class MailItem:
    uid: str
    subject: str
    sender: str
    date: str
    body_preview: str


@dataclass
class PendingAction:
    action: str
    data: dict[str, str] = field(default_factory=dict)


def _env(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def load_config() -> Config:
    return Config(
        bot_token=_env("BOT_TOKEN"),
        imap_host=_env("IMAP_HOST"),
        imap_port=int(_env("IMAP_PORT", "993")),
        smtp_host=_env("SMTP_HOST"),
        smtp_port=int(_env("SMTP_PORT", "587")),
        check_interval_seconds=int(_env("CHECK_INTERVAL_SECONDS", "25")),
        smtp_use_starttls=_env("SMTP_USE_STARTTLS", "true").lower() in {"1", "true", "yes", "y"},
        db_path=_env("DB_PATH", "/opt/mail_tg_bot/mailbot.sqlite3"),
        mailcow_api_url=os.getenv("MAILCOW_API_URL", "").strip(),
        mailcow_api_key=os.getenv("MAILCOW_API_KEY", "").strip(),
    )


def normalize_email(email: str) -> str:
    email = email.strip()
    if "@" not in email:
        return email
    local, domain = email.split("@", 1)
    try:
        domain_ascii = domain.encode("idna").decode("ascii")
    except Exception:
        domain_ascii = domain
    return f"{local}@{domain_ascii}"


def decode_mime(value: Optional[str]) -> str:
    if not value:
        return "(без темы)"
    parts = decode_header(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def extract_text_preview(msg) -> str:
    text_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            if content_type != "text/plain":
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text_parts.append(payload.decode(charset, errors="replace"))
            except Exception:
                text_parts.append(payload.decode("utf-8", errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                text_parts.append(payload.decode(charset, errors="replace"))
            except Exception:
                text_parts.append(payload.decode("utf-8", errors="replace"))

    text = "\n".join(part.strip() for part in text_parts if part.strip())
    text = " ".join(text.split())
    if not text:
        return "(без текста)"
    if len(text) > 220:
        return text[:217] + "..."
    return text


def db_conn(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str) -> None:
    with db_conn(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                user_id INTEGER PRIMARY KEY,
                email TEXT NOT NULL,
                password TEXT NOT NULL,
                last_seen_uid INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()


def get_session(db_path: str, user_id: int) -> Optional[Session]:
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT user_id, email, password, last_seen_uid FROM sessions WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return Session(row["user_id"], row["email"], row["password"], row["last_seen_uid"])


def list_sessions(db_path: str) -> list[Session]:
    with db_conn(db_path) as conn:
        rows = conn.execute("SELECT user_id, email, password, last_seen_uid FROM sessions").fetchall()
    return [Session(r["user_id"], r["email"], r["password"], r["last_seen_uid"]) for r in rows]


def save_session(db_path: str, user_id: int, email: str, password: str) -> None:
    with db_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO sessions (user_id, email, password, last_seen_uid)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET email=excluded.email, password=excluded.password
            """,
            (user_id, email, password),
        )
        conn.commit()


def delete_session(db_path: str, user_id: int) -> None:
    with db_conn(db_path) as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.commit()


def update_last_seen_uid(db_path: str, user_id: int, last_seen_uid: int) -> None:
    with db_conn(db_path) as conn:
        conn.execute("UPDATE sessions SET last_seen_uid = ? WHERE user_id = ?", (last_seen_uid, user_id))
        conn.commit()


def check_login(cfg: Config, email: str, password: str) -> None:
    with imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port) as imap:
        imap.login(normalize_email(email), password)


def fetch_last_messages(cfg: Config, email: str, password: str, limit: int = 5) -> list[MailItem]:
    limit = max(1, min(limit, 20))
    with imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port) as imap:
        imap.login(email, password)
        imap.select("INBOX")
        status, data = imap.uid("search", None, "ALL")
        if status != "OK" or not data or not data[0]:
            return []

        uids = data[0].split()[-limit:]
        items: list[MailItem] = []
        for uid_b in reversed(uids):
            uid = uid_b.decode()
            st, msg_data = imap.uid("fetch", uid, "(RFC822)")
            if st != "OK" or not msg_data:
                continue
            raw = next((p[1] for p in msg_data if isinstance(p, tuple)), None)
            if not raw:
                continue

            msg = message_from_bytes(raw)
            subject = decode_mime(msg.get("Subject"))
            sender = decode_mime(msg.get("From") or "(неизвестно)")
            body_preview = extract_text_preview(msg)
            dt_raw = msg.get("Date")
            try:
                dt = parsedate_to_datetime(dt_raw).strftime("%Y-%m-%d %H:%M:%S") if dt_raw else "(без даты)"
            except Exception:
                dt = dt_raw or "(без даты)"
            items.append(MailItem(uid, subject, sender, dt, body_preview))
        return items


def fetch_new_messages(cfg: Config, session: Session) -> tuple[list[MailItem], int]:
    with imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port) as imap:
        imap.login(session.email, session.password)
        imap.select("INBOX")
        status, data = imap.uid("search", None, "ALL")
        if status != "OK" or not data or not data[0]:
            return [], session.last_seen_uid

        all_uids = [int(x) for x in data[0].split()] if data[0] else []
        if not all_uids:
            return [], session.last_seen_uid

        current_max = all_uids[-1]
        if session.last_seen_uid <= 0:
            return [], current_max

        new_uids = [uid for uid in all_uids if uid > session.last_seen_uid]
        if not new_uids:
            return [], current_max

        items: list[MailItem] = []
        for uid_int in new_uids:
            uid = str(uid_int)
            st, msg_data = imap.uid("fetch", uid, "(RFC822)")
            if st != "OK" or not msg_data:
                continue
            raw = next((p[1] for p in msg_data if isinstance(p, tuple)), None)
            if not raw:
                continue

            msg = message_from_bytes(raw)
            subject = decode_mime(msg.get("Subject"))
            sender = decode_mime(msg.get("From") or "(неизвестно)")
            body_preview = extract_text_preview(msg)
            dt_raw = msg.get("Date")
            try:
                dt = parsedate_to_datetime(dt_raw).strftime("%Y-%m-%d %H:%M:%S") if dt_raw else "(без даты)"
            except Exception:
                dt = dt_raw or "(без даты)"
            items.append(MailItem(uid, subject, sender, dt, body_preview))
        return items, current_max


def send_email(cfg: Config, email: str, password: str, to_addr: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = email
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    if cfg.smtp_use_starttls:
        context = ssl.create_default_context()
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(email, password)
            server.send_message(msg)
    else:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=context, timeout=30) as server:
            server.login(email, password)
            server.send_message(msg)


def parse_mailcow_register_response(body: str, fallback_email: str) -> tuple[bool, str]:
    try:
        payload = json.loads(body)
    except Exception:
        return True, f"Ящик создан: {fallback_email}"

    if not isinstance(payload, list) or not payload:
        return True, f"Ящик создан: {fallback_email}"

    messages: list[str] = []
    success = False

    for item in payload:
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        msg = item.get("msg")

        if isinstance(msg, list) and msg:
            code = msg[0]
            value = msg[1] if len(msg) > 1 else None

            if code == "mailbox_added":
                created_email = value or fallback_email
                messages.append(f"Ящик создан: {created_email}")
                success = True
                continue
            if code == "password_complexity":
                messages.append("Пароль слишком простой. Нужен более сложный пароль.")
                continue
            if code == "mailbox_quota_left_exceeded":
                messages.append("На домене закончилась доступная квота для новых ящиков.")
                continue
            if code == "rl_saved":
                continue

        if isinstance(msg, str):
            messages.append(msg)

    if success and messages:
        return True, "\n".join(dict.fromkeys(messages))
    if messages:
        return False, "\n".join(dict.fromkeys(messages))
    return True, f"Ящик создан: {fallback_email}"


def register_mailbox(cfg: Config, email: str, password: str) -> tuple[bool, str]:
    if not cfg.mailcow_api_url or not cfg.mailcow_api_key:
        return False, "Регистрация выключена: добавь MAILCOW_API_URL и MAILCOW_API_KEY в .env"

    if "@" not in email:
        return False, "Неверный email"

    email = normalize_email(email)
    local_part, domain = email.split("@", 1)

    payload = {
        "active": "1",
        "domain": domain,
        "local_part": local_part,
        "name": local_part,
        "password": password,
        "password2": password,
        "quota": "1024",
    }

    url = cfg.mailcow_api_url.rstrip("/") + "/api/v1/add/mailbox"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-API-Key": cfg.mailcow_api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return parse_mailcow_register_response(body, email)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        ok, text = parse_mailcow_register_response(body, email)
        if ok:
            return True, text
        return False, text
    except Exception as exc:
        return False, f"Ошибка регистрации: {exc}"


def parse_login_args(raw: str) -> tuple[Optional[str], Optional[str]]:
    parts = raw.strip().split(maxsplit=1)
    if len(parts) != 2:
        return None, None
    return parts[0].strip(), parts[1].strip()


def parse_register_args(raw: str) -> tuple[Optional[str], Optional[str]]:
    return parse_login_args(raw)


def parse_send_args(raw: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    parts = [p.strip() for p in raw.split("|", 2)]
    if len(parts) != 3 or not all(parts):
        return None, None, None
    return parts[0], parts[1], parts[2]


def main_menu(is_logged_in: bool) -> InlineKeyboardMarkup:
    if is_logged_in:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="📬 Последние 5", callback_data="menu_last5", style="success"),
                    InlineKeyboardButton(text="✉️ Отправить", callback_data="menu_send", style="primary"),
                ],
                [InlineKeyboardButton(text="👤 Кто я", callback_data="menu_whoami")],
                [InlineKeyboardButton(text="🚪 Выйти", callback_data="menu_logout", style="primary")],
            ]
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔐 Войти", callback_data="menu_login", style="primary"),
                InlineKeyboardButton(text="🆕 Регистрация", callback_data="menu_register", style="success"),
            ],
        ]
    )


def menu_for_user(user_id: int) -> InlineKeyboardMarkup:
    return main_menu(is_logged_in=get_session(cfg.db_path, user_id) is not None)


cfg = load_config()
init_db(cfg.db_path)
bot = Bot(token=cfg.bot_token)
dp = Dispatcher()
pending: dict[int, PendingAction] = {}


@dp.message(Command("start"))
@dp.message(Command("help"))
async def cmd_start(message: Message):
    await message.answer(
        "Почтовый бот готов.\n"
        "Работает с кириллическими доменами.\n\n"
        "Команды:\n"
        "/login email пароль\n"
        "/register email пароль\n"
        "/whoami\n"
        "/last [N]\n"
        "/send (мастер отправки)\n"
        "/logout",
        reply_markup=menu_for_user(message.from_user.id),
    )


@dp.callback_query(lambda c: c.data == "menu_login")
async def cb_login(callback: CallbackQuery):
    pending[callback.from_user.id] = PendingAction(action="login")
    await callback.message.answer("Введи: email пароль")
    await callback.answer()


@dp.callback_query(lambda c: c.data == "menu_register")
async def cb_register(callback: CallbackQuery):
    pending[callback.from_user.id] = PendingAction(action="register")
    await callback.message.answer("Введи: email пароль")
    await callback.answer()


@dp.callback_query(lambda c: c.data == "menu_send")
async def cb_send(callback: CallbackQuery):
    s = get_session(cfg.db_path, callback.from_user.id)
    if not s:
        await callback.message.answer("Сначала войди в почту: /login email пароль")
        await callback.answer()
        return
    pending[callback.from_user.id] = PendingAction(action="send_to")
    await callback.message.answer(
        "Шаг 1/3.\n"
        "Кому отправить письмо?\n"
        "Пример: `user@example.com`",
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "menu_whoami")
async def cb_whoami(callback: CallbackQuery):
    s = get_session(cfg.db_path, callback.from_user.id)
    if not s:
        await callback.message.answer("Ты не вошел. Нажми Войти")
    else:
        await callback.message.answer(f"Текущий ящик: {s.email}")
    await callback.answer()


@dp.callback_query(lambda c: c.data == "menu_logout")
async def cb_logout(callback: CallbackQuery):
    delete_session(cfg.db_path, callback.from_user.id)
    pending.pop(callback.from_user.id, None)
    await callback.message.answer("Выход выполнен", reply_markup=menu_for_user(callback.from_user.id))
    await callback.answer()


@dp.callback_query(lambda c: c.data == "menu_last5")
async def cb_last5(callback: CallbackQuery):
    s = get_session(cfg.db_path, callback.from_user.id)
    if not s:
        await callback.message.answer("Сначала войди: кнопка Войти")
        await callback.answer()
        return

    loop = asyncio.get_running_loop()
    try:
        items = await loop.run_in_executor(None, fetch_last_messages, cfg, s.email, s.password, 5)
    except Exception as exc:
        await callback.message.answer(f"IMAP ошибка: {exc}")
        await callback.answer()
        return

    if not items:
        await callback.message.answer("Входящие пусты")
    else:
        lines = [
            f"UID {i.uid}\nОт: {i.sender}\nТема: {i.subject}\nДата: {i.date}\nТекст: {i.body_preview}"
            for i in items
        ]
        await callback.message.answer("\n\n".join(lines)[:3800])
    await callback.answer()


@dp.message(Command("login"))
async def cmd_login(message: Message):
    email, password = parse_login_args(message.text.replace("/login", "", 1))
    if not email or not password:
        await message.answer("Использование: /login email пароль")
        return

    email = normalize_email(email)
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, check_login, cfg, email, password)
        save_session(cfg.db_path, message.from_user.id, email, password)
        await message.answer(f"Вход успешен: {email}", reply_markup=menu_for_user(message.from_user.id))
    except Exception as exc:
        await message.answer(f"Ошибка входа: {exc}")


@dp.message(Command("register"))
async def cmd_register(message: Message):
    email, password = parse_register_args(message.text.replace("/register", "", 1))
    if not email or not password:
        await message.answer("Использование: /register email пароль")
        return

    ok, text = await asyncio.get_running_loop().run_in_executor(None, register_mailbox, cfg, email, password)
    await message.answer(text if ok else f"Не удалось: {text}")


@dp.message(Command("logout"))
async def cmd_logout(message: Message):
    delete_session(cfg.db_path, message.from_user.id)
    pending.pop(message.from_user.id, None)
    await message.answer("Выход выполнен", reply_markup=menu_for_user(message.from_user.id))


@dp.message(Command("whoami"))
async def cmd_whoami(message: Message):
    s = get_session(cfg.db_path, message.from_user.id)
    if not s:
        await message.answer("Ты не вошел. Используй /login")
    else:
        await message.answer(f"Текущий ящик: {s.email}")


@dp.message(Command("last"))
async def cmd_last(message: Message):
    s = get_session(cfg.db_path, message.from_user.id)
    if not s:
        await message.answer("Ты не вошел. Используй /login")
        return

    args = message.text.replace("/last", "", 1).strip()
    count = 5
    if args:
        try:
            count = int(args)
        except ValueError:
            await message.answer("Использование: /last 5")
            return

    loop = asyncio.get_running_loop()
    try:
        items = await loop.run_in_executor(None, fetch_last_messages, cfg, s.email, s.password, count)
    except Exception as exc:
        await message.answer(f"IMAP ошибка: {exc}")
        return

    if not items:
        await message.answer("Входящие пусты")
        return

    lines = [
        f"UID {i.uid}\nОт: {i.sender}\nТема: {i.subject}\nДата: {i.date}\nТекст: {i.body_preview}"
        for i in items
    ]
    await message.answer("\n\n".join(lines)[:3800])


@dp.message(Command("send"))
async def cmd_send(message: Message):
    s = get_session(cfg.db_path, message.from_user.id)
    if not s:
        await message.answer("Ты не вошел. Используй /login")
        return

    pending[message.from_user.id] = PendingAction(action="send_to")
    await message.answer(
        "Шаг 1/3.\n"
        "Кому отправить письмо?\n"
        "Пример: `user@example.com`",
        parse_mode="Markdown",
    )


@dp.message()
async def handle_pending(message: Message):
    action = pending.get(message.from_user.id)
    if not action:
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустой ввод")
        return

    if action.action == "login":
        pending.pop(message.from_user.id, None)
        email, password = parse_login_args(text)
        if not email or not password:
            await message.answer("Нужно: email пароль")
            return
        email = normalize_email(email)
        try:
            await asyncio.get_running_loop().run_in_executor(None, check_login, cfg, email, password)
            save_session(cfg.db_path, message.from_user.id, email, password)
            await message.answer(f"Вход успешен: {email}", reply_markup=menu_for_user(message.from_user.id))
        except Exception as exc:
            await message.answer(f"Ошибка входа: {exc}")
        return

    if action.action == "register":
        pending.pop(message.from_user.id, None)
        email, password = parse_register_args(text)
        if not email or not password:
            await message.answer("Нужно: email пароль")
            return
        ok, reply = await asyncio.get_running_loop().run_in_executor(None, register_mailbox, cfg, email, password)
        await message.answer(reply if ok else f"Не удалось: {reply}")
        return

    if action.action == "send":
        pending.pop(message.from_user.id, None)
        s = get_session(cfg.db_path, message.from_user.id)
        if not s:
            await message.answer("Сначала войди")
            return
        to_addr, subject, body = parse_send_args(text)
        if not to_addr:
            await message.answer("Нужно: кому@example.com | Тема | Текст")
            return
        to_addr = normalize_email(to_addr)
        try:
            await asyncio.get_running_loop().run_in_executor(None, send_email, cfg, s.email, s.password, to_addr, subject, body)
            await message.answer("Письмо отправлено")
        except Exception as exc:
            await message.answer(f"SMTP ошибка: {exc}")
        return

    if action.action == "send_to":
        to_addr = normalize_email(text)
        if "@" not in to_addr:
            await message.answer("Неверный адрес. Введи email получателя, например user@example.com")
            return
        action.action = "send_subject"
        action.data["to_addr"] = to_addr
        pending[message.from_user.id] = action
        await message.answer("Шаг 2/3.\nВведи тему письма:")
        return

    if action.action == "send_subject":
        action.action = "send_body"
        action.data["subject"] = text
        pending[message.from_user.id] = action
        await message.answer("Шаг 3/3.\nВведи текст письма:")
        return

    if action.action == "send_body":
        pending.pop(message.from_user.id, None)
        s = get_session(cfg.db_path, message.from_user.id)
        if not s:
            await message.answer("Сначала войди")
            return
        to_addr = action.data.get("to_addr", "")
        subject = action.data.get("subject", "(без темы)")
        body = text
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, send_email, cfg, s.email, s.password, to_addr, subject, body
            )
            await message.answer(
                "Письмо отправлено.\n"
                f"Кому: {to_addr}\n"
                f"Тема: {subject}",
                reply_markup=menu_for_user(message.from_user.id),
            )
        except Exception as exc:
            await message.answer(f"SMTP ошибка: {exc}")


async def poll_new_messages():
    await asyncio.sleep(5)
    while True:
        for s in list_sessions(cfg.db_path):
            try:
                loop = asyncio.get_running_loop()
                items, max_uid = await loop.run_in_executor(None, fetch_new_messages, cfg, s)
                if max_uid > s.last_seen_uid:
                    update_last_seen_uid(cfg.db_path, s.user_id, max_uid)
                for it in items:
                    await bot.send_message(
                        s.user_id,
                        "Новое письмо\n"
                        f"Ящик: {s.email}\n"
                        f"От: {it.sender}\n"
                        f"Тема: {it.subject}\n"
                        f"Дата: {it.date}\n"
                        f"Текст: {it.body_preview}",
                    )
            except Exception:
                pass
        await asyncio.sleep(max(10, cfg.check_interval_seconds))


async def main():
    asyncio.create_task(poll_new_messages())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
