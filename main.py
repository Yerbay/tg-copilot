"""Копилот переписки — многопользовательский (до MAX_USERS человек).

Один процесс, для каждого пользователя бота:
- reader — его аккаунт Telegram (вход по QR через бота). ТОЛЬКО читает чаты из его белого списка.
- WhatsApp — его сессия в общем мосте wa/bridge.mjs (привязка по коду). Тоже только читает;
  сохраняются лишь сообщения из его белого списка, остальные отбрасываются сразу.
- bot — общий бот от @BotFather. Каждому пишет только про его данные.

Кто сейчас «я», хранит contextvar `cur` (см. acting_as): его ставит каждый обработчик
(сообщение боту, событие Telegram/WhatsApp пользователя, вечерний разбор), а задачи наследуют.

Написать собеседникам скрипт не может: у reader и моста WhatsApp нет ни одного вызова отправки,
а бот пишет только пользователям бота (см. notify и notify_photo).
Новых пользователей пускает администратор (ADMIN_ID) кнопкой в боте.
"""
import asyncio
import base64
import contextlib
import functools
import hashlib
import io
import random
import json
import logging
import re
import shlex
import shutil
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import qrcode
from qrcode.image.pure import PyPNGImage
from telethon import Button, TelegramClient, events
from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError
from telethon.tl import types
from telethon.tl.types import User as TgUser

import config
import db
import humanize
import llm
import prompts
import stt
import wa

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # не писать в журнал каждый запрос к API
log = logging.getLogger("copilot")

bot: TelegramClient  # общий бот-консультант
TZ = ZoneInfo(config.TIMEZONE)
_pending: dict[int, asyncio.Task] = {}  # contact_id -> отложенная генерация подсказки
_series: dict[int, float] = {}          # contact_id -> когда началась серия её сообщений (пока ждём тишины)
DIALOG_MAX = 20                         # сколько последних реплик разговора с агентом помнить
_access_requests: dict[int, str] = {}   # кто просил доступ: id -> имя


# ================================================================ пользователи

@dataclass(eq=False)
class User:
    """Пользователь бота и всё, что о нём держим в памяти."""
    id: int
    name: str
    role: str = "user"                     # admin | user
    status: str = "active"                 # active | blocked
    partner: str = "f"                     # f — переписывается с девушками, m — с парнями
    about: str = ""                        # карточка самого пользователя: возраст, город, работа…
    literacy: str = "95"                   # грамотность предлагаемых сообщений, см. humanize.LEVELS
    mode: str = "dating"                   # dating — знакомства, relationship — постоянные отношения
    reader: TelegramClient | None = None   # его Telegram, только чтение
    tg_ready: bool = False
    tg_name: str = ""
    tg_login: asyncio.Task | None = None   # идёт вход по QR
    wa_status: str = "не подключён"
    wa_pairing: bool = False               # запрошен код привязки — сообщить, когда подключится
    focus: tuple | None = None             # о ком разговор: ("contact", id) | ("all", None)
    dialog: list = field(default_factory=list)
    turn_log: list | None = None           # что инструменты показали за текущий ход агента
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_screenshot: dict | None = None
    state: tuple | None = None             # ждём ввода: ("wa_phone",) | ("tg_2fa", client)
    limit_notice: date | None = None       # когда последний раз сообщали про дневной лимит

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


USERS: dict[int, User] = {}
cur: ContextVar[User] = ContextVar("user")


def me() -> User:
    return cur.get()


@contextlib.contextmanager
def acting_as(u: User):
    """Всё внутри (и запущенные отсюда задачи) выполняется от имени пользователя u."""
    token = cur.set(u)
    try:
        yield u
    finally:
        cur.reset(token)


def _user_from_row(row) -> User:
    return User(id=row["id"], name=row["name"], role=row["role"], status=row["status"], partner=row["partner"],
                about=row["about"], literacy=row["literacy"], mode=row["mode"])


FORMS = {
    "f": {"she": "Она", "wrote": "написала", "partners": "девушками", "people": "Собеседницы", "her": "её"},
    "m": {"she": "Он", "wrote": "написал", "partners": "парнями", "people": "Собеседники", "her": "его"},
}


def F(key: str) -> str:
    """Слово с учётом того, с кем переписывается текущий пользователь."""
    u = cur.get(None)
    return FORMS[u.partner if u else "f"][key]


MODES = {"dating": "🔍 Знакомства", "relationship": "💞 Отношения"}


_contact_mode: ContextVar[str | None] = ContextVar("contact_mode", default=None)  # режим человека, о ком сейчас речь


def mode_of(contact) -> str:
    """Режим для человека: свой, если задан в его карточке, иначе — пользователя по умолчанию."""
    if contact is not None and contact["mode"]:
        return contact["mode"]
    u = cur.get(None)
    return u.mode if u else "dating"


@contextlib.contextmanager
def contact_mode(contact):
    """Всё внутри — в режиме этого человека (промпты, кнопки, подсказки только на важное)."""
    token = _contact_mode.set(mode_of(contact) if contact is not None else None)
    try:
        yield
    finally:
        _contact_mode.reset(token)


def per_contact(fn):
    """Для функций, где первый аргумент — контакт: выполнять в его режиме."""
    @functools.wraps(fn)
    async def wrapper(contact, *args, **kwargs):
        with contact_mode(contact):
            return await fn(contact, *args, **kwargs)
    return wrapper


def is_rel() -> bool:
    return (_contact_mode.get() or mode_of(None)) == "relationship"


def P(name: str) -> str:
    """Промпт для режима текущего пользователя: в отношениях — вариант с суффиксом _REL, если он есть."""
    if is_rel() and hasattr(prompts, name + "_REL"):
        return getattr(prompts, name + "_REL")
    return getattr(prompts, name)


def system_prompt() -> str:
    u = me()
    return P("SYSTEM").format(
        partners=F("partners"), partner_note=prompts.PARTNER_NOTE_M if u.partner == "m" else "",
    ) + prompts.USER_STYLE.format(
        about=u.about or prompts.ABOUT_EMPTY, literacy=prompts.LITERACY.get(u.literacy, prompts.LITERACY["95"]),
    )


HELP = """Пишите мне обычным языком — я сам пойму, что сделать. Например:
• «добавь Анну, её вотсап 8 701 123 45 67» / «её телеграм @anna»
• «что ответить Анне?» / «дай варианты посмешнее»
• «она стала отвечать коротко, что не так?»
• «вот мой черновик для Анны: …»
• «запомни, что Анна любит горы»
• «запомни про меня: мне 34, работаю в IT» — варианты будут звучать как вы
• «Аня — моя девушка» / «с Машей просто знакомлюсь» — режим для конкретного человека
• «кому я давно не отвечал?»
• «убери Марию из списка»
• «подключи вотсап, мой номер …» / «подключи телеграм»

Можно голосовым — я расшифрую и отвечу. Можно прислать скриншоты переписки или анкеты из любого приложения \
(Tinder, Instagram…), один или альбомом. Имя видно на скриншоте — сохраню сам (если такое имя уже есть — \
спрошу, та же это или другая); подпись «это Дина» — сохраню под этим именем.
Голосовые в переписках из белого списка я тоже расшифровываю.

Сам я тоже пишу: когда вам пишут — варианты ответа (кнопки под ними: ещё, короче, с юмором, теплее), \
каждый вечер — разбор дня и напоминания. Читаю только чаты, которые вы добавили в список, и никому не пишу.

Команды — то же самое коротко:
/menu — главное меню
/connect — подключить WhatsApp / Telegram, что подключено
/settings — настройки: режим (знакомства / отношения), грамотность вариантов, о себе
/me — что я знаю о вас (/me 34 года, Алматы… — записать)
/add @username Имя — добавить чат Telegram
/add wa +77011234567 Имя — добавить чат WhatsApp
/list — контакты
/suggest Имя, /review Имя, /draft Имя текст
/daily — вечерний разбор сейчас
/delete_me — удалить все мои данные и отключить мессенджеры"""

HELP_ADMIN = "\n\nАдминистратору: /users — пользователи, расход, блокировка."


# ================================================================ отправка

async def notify(text: str, buttons=None, to: int | None = None):
    """Единственная функция отправки текста. Адресат — пользователь бота (по умолчанию текущий)."""
    target = to or me().id
    text = text.strip() or "(пусто)"
    if to is None and me().turn_log is not None:  # идёт ход агента — запомним, что он показал
        me().turn_log.append(text)
    chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]  # лимит Telegram — 4096
    sent = None
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        sent = await bot.send_message(target, chunk, buttons=buttons if last else None, parse_mode=None)
    return sent


async def notify_photo(png: bytes, caption: str, edit=None, to: int | None = None):
    """Картинка (QR) — тоже только пользователю бота. edit — прежнее сообщение, которое заменить."""
    target = to or me().id
    file = io.BytesIO(png)
    file.name = "qr.png"
    if edit is not None:
        try:
            return await bot.edit_message(target, edit, caption, file=file)
        except Exception:  # noqa: BLE001 — сообщение удалили или оно слишком старое: пришлём новое
            file.seek(0)
    return await bot.send_file(target, file, caption=caption)


def typing():
    return bot.action(me().id, "typing")


# ================================================================ переписка

@dataclass
class Msg:
    """Сообщение из любого мессенджера."""
    date: datetime  # с часовым поясом
    out: bool       # True — написал пользователь
    text: str
    source: str     # TG | WA | SS


def _fmt_msg(m: Msg) -> str:
    who = "Я" if m.out else F("she")
    when = m.date.astimezone(TZ).strftime("%d.%m %H:%M")
    return f"[{when} {m.source}] {who}: {m.text}"


_stt_slots = asyncio.Semaphore(3)  # сколько голосовых распознаём одновременно


async def _tg_voice_text(u: User, m) -> str:
    """Голосовое из Telegram -> «[голосовое] текст». Расшифровка кэшируется, повторно не платим."""
    key = f"tg:{u.id}:{m.chat_id}:{m.id}"
    text = db.get_transcript(key)
    if text is None and stt.enabled():
        async with _stt_slots:
            audio = await u.reader.download_media(m, file=bytes)
            text = await stt.transcribe(audio, m.file.mime_type or "audio/ogg", key)
    return f"[голосовое] {text}" if text else "[голосовое]"


async def history(contact, limit: int | None = None, since: datetime | None = None) -> list[Msg]:
    """Переписка с человеком: Telegram, WhatsApp и скриншоты вместе, старые сверху.
    limit — сколько последних сообщений всего, since — только сообщения новее этой даты."""
    limit = limit or config.HISTORY_LIMIT
    owner = USERS.get(contact["user_id"])
    msgs: list[Msg] = []
    if contact["chat_id"] and owner and owner.tg_ready:
        tg = await owner.reader.get_messages(contact["chat_id"], limit=limit)
        voices = iter(await asyncio.gather(*(_tg_voice_text(owner, m) for m in tg if m.voice)))
        for m in tg:
            text = next(voices) if m.voice else m.message or ("[медиа]" if m.media else "")
            msgs.append(Msg(m.date, m.out, text, "TG"))
    for r in db.messages(contact["id"], limit):  # WhatsApp и скриншоты
        msgs.append(Msg(datetime.fromisoformat(r["ts"]), bool(r["out"]), r["text"], r["source"]))
    msgs.sort(key=lambda m: m.date)
    msgs = msgs[-limit:]
    if since:
        msgs = [m for m in msgs if m.date >= since]
    return msgs


def as_text(msgs: list) -> str:
    return "\n".join(_fmt_msg(m) for m in msgs) or "(сообщений нет)"


def facts_of(contact) -> str:
    return contact["facts"] or "(пока пусто)"


def own(contact_id: int):
    """Контакт, только если он принадлежит текущему пользователю (id из кнопок можно подделать)."""
    contact = db.get_by_id(contact_id)
    return contact if contact and contact["user_id"] == me().id else None


# ================================================================ клавиатуры

def kb_main():
    return [
        [Button.inline(f"👥 {F('people')}", b"L"), Button.inline("🌐 Вопрос по всем", b"A")],
        [Button.inline("📊 Вечерний разбор", b"Y"), Button.inline("🔌 Подключения", b"S")],
        [Button.inline("❓ Помощь", b"H")],
    ]


def kb_contacts():
    rows = [[Button.inline(("💞 " if mode_of(r) == "relationship" else "") + r["name"], f"c:{r['id']}".encode())]
            for r in db.list_contacts(me().id)]
    rows.append([Button.inline("⬅️ Меню", b"M")])
    return rows


def kb_contact(cid: int):
    contact = own(cid)
    mode = MODES[mode_of(contact)] if contact else ""
    return [
        [Button.inline("💡 Варианты ответа", f"s:{cid}".encode()), Button.inline("🔍 Разбор", f"v:{cid}".encode())],
        [Button.inline("📇 Карточка", f"k:{cid}".encode()), Button.inline(f"{mode} · сменить", f"cm:{cid}".encode())],
        [Button.inline("🗑 Убрать", f"d:{cid}".encode()), Button.inline("⬅️ К списку", b"L")],
    ]


COPY_MAX = 256  # столько символов Telegram разрешает в кнопке «копировать»
_VARIANT_LINE = re.compile(r"^\s*[*_]*\s*(\d)\s*(?:️?⃣|[.)])[*_\s]*(.+?)\s*$")  # «1️⃣ …», «1. …», «2) …»
_QUOTES = "«»\"“”„'‘’`"


def _clean_variant(text: str) -> str:
    """Текст варианта для вставки в мессенджер: без кавычек и разметки вокруг."""
    text = text.strip().strip("*_").strip()
    if (q := re.fullmatch(r"[«\"“„](.+?)[»\"”“]\s*[.!?)]*", text)) and q.group(1):
        text = q.group(1)  # «привет)» -> привет)
    return text.strip(_QUOTES + " ").strip()


def extract_variants(text: str, need_quotes: bool = False) -> list[str]:
    """Варианты ответа из нумерованного списка. need_quotes — брать только пункты с текстом в кавычках
    (в свободных ответах агента нумерованным бывает и обычный совет)."""
    variants = []
    for line in text.splitlines():
        m = _VARIANT_LINE.match(line)
        if not m:
            continue
        body = m.group(2)
        quoted = re.search(r"«([^»]{2,})»", body) or re.search(r"[\"“]([^\"”]{2,})[\"”]", body)
        if need_quotes and not quoted:
            continue
        variant = _clean_variant(quoted.group(1) if quoted else body)  # пояснение идёт отдельной строкой
        if variant:
            variants.append(variant)
    return variants[:5]


def humanize_answer(text: str, need_quotes: bool = False) -> str:
    """Ошибки по уровню грамотности пользователя — в текстах вариантов, а не в пояснениях к ним.
    Меняем прямо в ответе, чтобы на экране и в кнопке «копировать» был один и тот же текст."""
    level = me().literacy
    if level not in ("95", "85", "70"):
        return text
    rnd = random.Random()
    lines = []
    for line in text.splitlines():
        m = _VARIANT_LINE.match(line)
        if m:
            body = m.group(2)
            if re.search(r"«[^»]{2,}»", body):  # текст сообщения в кавычках, после них — пояснение
                body = re.sub(r"«([^»]{2,})»", lambda q: f"«{humanize.humanize(q.group(1), level, rnd)}»", body)
            elif not need_quotes:
                body = humanize.humanize(body, level, rnd)
            line = line[: m.start(2)] + body + line[m.end(2) :]
        lines.append(line)
    return "\n".join(lines)


def kb_copy(variants: list[str]) -> list:
    """Ряд кнопок «📋 1 / 📋 2 / 📋 3»: нажатие копирует вариант — остаётся вставить его в мессенджер."""
    row = [types.KeyboardInlineButton(text=f"📋 {i}", type=types.InlineButtonTypeCopy(copy_text=v))
           for i, v in enumerate(variants, 1) if len(v) <= COPY_MAX]
    return [row] if row else []


def kb_suggestion(sid: int, cid: int, variants: list[str] | None = None):
    rows = kb_copy(variants or []) + [
        [Button.inline("🔄 Ещё", f"r:{sid}:more".encode()), Button.inline("✂️ Короче", f"r:{sid}:short".encode())],
        [Button.inline("😄 С юмором", f"r:{sid}:fun".encode()), Button.inline("🤍 Теплее", f"r:{sid}:warm".encode())],
    ]
    if is_rel():
        rows += [
            [Button.inline("🫂 Поддержать", f"r:{sid}:support".encode()),
             Button.inline("🙏 Извиниться", f"r:{sid}:sorry".encode())],
            [Button.inline("🕊 Сгладить", f"r:{sid}:soften".encode()), Button.inline("💬 Обсудить", f"c:{cid}".encode())],
        ]
    else:
        rows.append([Button.inline("💬 Обсудить", f"c:{cid}".encode())])
    return rows


def kb_connect():
    return [
        [Button.inline("📱 WhatsApp", b"cw"), Button.inline("✈️ Telegram", b"ct")],
        [Button.inline("📸 Только скриншоты", b"cs")],
    ]


def kb_partner():
    return [[Button.inline("👩 С девушками", b"p:f"), Button.inline("👨 С парнями", b"p:m")]]


def kb_literacy():
    level = me().literacy
    return [[Button.inline(("✅ " if lv == level else "") + humanize.LABELS[lv], f"g:{lv}".encode())]
            for lv in humanize.LEVELS]


def kb_settings():
    return [
        [Button.inline("👤 Обо мне", b"ab"), Button.inline("✍️ Грамотность", b"g")],
        [Button.inline("💞 Режим", b"md"), Button.inline("👩/👨 С кем переписываюсь", b"pp")],
    ]


def kb_mode():
    return [[Button.inline(("✅ " if key == me().mode else "") + label, f"md:{key}".encode())]
            for key, label in MODES.items()]


# ================================================================ подсказки

def her_last(msgs: list[Msg]) -> str:
    """Сообщения собеседника после последнего ответа пользователя; если он ответил последним — последнее."""
    tail = []
    for m in reversed(msgs):
        if m.out:
            break
        tail.append(m.text)
    if not tail:
        tail = [next((m.text for m in reversed(msgs) if not m.out), "")]
    return "\n".join(f"«{t}»" for t in reversed(tail) if t)[-1000:]


def _quote(name: str, incoming: str) -> str:
    """Шапка подсказки: на что именно отвечаем."""
    return f"💬 {name} {F('wrote')}:\n{incoming}\n\n" if incoming else f"💬 {name}\n\n"


@per_contact
async def suggest(contact, wish: str = "", msgs: list[Msg] | None = None) -> None:
    msgs = msgs or await history(contact)
    if not msgs:
        await notify(f"{contact['name']}: в чате пока нет сообщений.")
        return
    task = P("SUGGEST").format(
        name=contact["name"], facts=facts_of(contact), history=as_text(msgs), n=config.NUM_VARIANTS,
    )
    if wish:
        task += f"\n\nПожелание пользователя к вариантам: {wish}"
    async with typing():
        answer = humanize_answer(await llm.complete(system_prompt(), task))
    incoming = her_last(msgs)
    sid = db.save_suggestion(contact["id"], incoming, answer)
    text = f"{_quote(contact['name'], incoming)}💡 Варианты ответа:\n\n{answer}"
    await notify(text, kb_suggestion(sid, contact["id"], extract_variants(answer)))
    u = me()
    if u.turn_log is None:  # прислали сами, не по просьбе — запомним, чтобы понимать «сделай второй короче»
        set_focus(("contact", contact["id"]))
        u.dialog.extend([{"role": "user", "content": f"(бот сам прислал варианты: {contact['name']} {F('wrote')})"},
                         {"role": "assistant", "content": text}])
        del u.dialog[:-DIALOG_MAX]


async def refine(sid: int, mode: str) -> None:
    sugg = db.get_suggestion(sid)
    contact = own(sugg["contact_id"]) if sugg else None
    if not contact or mode not in prompts.REFINE_MODES:
        await notify("Эта подсказка устарела.")
        return
    msgs = await history(contact)
    instruction = prompts.REFINE_MODES[mode].format(n=config.NUM_VARIANTS)
    await _refine(contact, sugg, msgs, instruction)


@per_contact
async def _refine(contact, sugg, msgs: list[Msg], instruction: str) -> None:
    async with typing():
        answer = await llm.complete(
            system_prompt(),
            prompts.REFINE.format(
                name=contact["name"], facts=facts_of(contact), history=as_text(msgs),
                variants=sugg["variants"], instruction=instruction,
            ),
        )
        answer = humanize_answer(answer)
    new_sid = db.save_suggestion(contact["id"], sugg["incoming"], answer)
    incoming = sugg["incoming"] if sugg["incoming"].startswith("«") else ""  # у старых подсказок другой формат
    await notify(f"{_quote(contact['name'], incoming)}💡 Варианты ответа:\n\n{answer}",
                 kb_suggestion(new_sid, contact["id"], extract_variants(answer)))


async def _delayed_suggest(contact_id: int, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        _series.pop(contact_id, None)  # тишина дождались — дальше «печатает» генерацию не прерывает
        contact = own(contact_id)
        if contact:
            await auto_suggest(contact)
    except asyncio.CancelledError:
        pass
    except llm.LimitReached as e:
        u = me()
        if u.limit_notice != date.today():  # о лимите — раз в день, а не на каждое сообщение
            u.limit_notice = date.today()
            await notify(f"⏸ {e} Подсказки на входящие тоже приостановлены до завтра.")
    except Exception as e:  # noqa: BLE001
        log.exception("Ошибка генерации подсказки")
        await notify(f"⚠️ Ошибка при генерации подсказки: {e}")
    finally:
        # убираем только себя: отменённая задача завершается уже после того, как её место заняла новая
        if _pending.get(contact_id) is asyncio.current_task():
            _pending.pop(contact_id, None)
            _series.pop(contact_id, None)


@per_contact
async def auto_suggest(contact) -> None:
    """Подсказка на входящее сама, без просьбы."""
    msgs = await history(contact)
    if is_rel() and not await is_important(msgs):
        return  # в отношениях подсказываем только на важное, остальное — по запросу
    await suggest(contact, msgs=msgs)


async def is_important(msgs: list[Msg]) -> bool:
    """Нужна ли помощь с ответом: быстрая модель отделяет важное от бытового («купи хлеб»)."""
    if not msgs or msgs[-1].out:
        return False
    answer = await llm.complete(system_prompt(), prompts.IMPORTANCE.format(history=as_text(msgs[-12:])),
                                max_tokens=10)
    return answer.strip().upper().startswith("ДА")


def schedule_suggest(contact_id: int) -> None:
    """Новое сообщение собеседника: ждём DEBOUNCE_SECONDS тишины (каждое сообщение продлевает ожидание),
    но не дольше DEBOUNCE_MAX_SECONDS от начала серии — потом готовим варианты."""
    now = asyncio.get_running_loop().time()
    start = _series.get(contact_id, now)
    cancel_suggest(contact_id)
    _series[contact_id] = start
    delay = max(0.0, min(config.DEBOUNCE_SECONDS, start + config.DEBOUNCE_MAX_SECONDS - now))
    _pending[contact_id] = asyncio.create_task(_delayed_suggest(contact_id, delay))


def extend_suggest(contact_id: int) -> None:
    """Собеседница печатает: если подсказка ещё ждёт тишины — подождём ещё. Новую не заводим."""
    if contact_id in _series:
        schedule_suggest(contact_id)


def cancel_suggest(contact_id: int) -> None:
    """Пользователь ответил сам — отложенная подсказка больше не нужна."""
    _series.pop(contact_id, None)
    task = _pending.pop(contact_id, None)
    if task:
        task.cancel()


# ================================================================ Telegram пользователей

TG_QR_ATTEMPTS = 6  # по 30 с — около 3 минут на сканирование
TG_QR_HINT = ("✈️ Вход в Telegram, который я буду читать.\n"
              "Telegram на телефоне → Настройки → Устройства → Подключить устройство → наведите камеру на этот код.\n"
              "Код надо показать на другом экране: откройте этот чат с ботом на компьютере "
              "(Telegram Desktop или web.telegram.org). Код обновляется сам, ~3 минуты.")


def qr_png(data: str) -> bytes:
    buf = io.BytesIO()
    qrcode.make(data, image_factory=PyPNGImage, box_size=8, border=2).save(buf)
    return buf.getvalue()


def attach_reader(u: User, client: TelegramClient) -> None:
    async def incoming(event):
        if u.status == "active" and (contact := db.get_by_chat(u.id, event.chat_id)):  # только белый список
            with acting_as(u):
                schedule_suggest(contact["id"])

    async def outgoing(event):
        if contact := db.get_by_chat(u.id, event.chat_id):
            cancel_suggest(contact["id"])

    async def typing_(event):  # «печатает…» — она ещё не закончила мысль
        if event.typing and (contact := db.get_by_chat(u.id, event.chat_id)):
            with acting_as(u):
                extend_suggest(contact["id"])

    client.add_event_handler(incoming, events.NewMessage(incoming=True, func=lambda e: e.is_private))
    client.add_event_handler(outgoing, events.NewMessage(outgoing=True, func=lambda e: e.is_private))
    client.add_event_handler(typing_, events.UserUpdate(func=lambda e: e.is_private))


async def start_reader(u: User, client: TelegramClient) -> None:
    attach_reader(u, client)
    await client.get_dialogs()  # прогреваем кэш, чтобы находить чаты по id
    who = await client.get_me()
    u.reader, u.tg_ready, u.tg_name = client, True, who.first_name or str(who.id)
    asyncio.create_task(_watch_reader(u, client))
    log.info("Пользователь %s: читаю Telegram «%s»", u.id, u.tg_name)


async def _watch_reader(u: User, client: TelegramClient) -> None:
    try:
        await client.run_until_disconnected()
    except Exception:  # noqa: BLE001
        log.exception("Telegram пользователя %s упал", u.id)
    if u.reader is client:  # отключились не по нашей воле (сеанс завершили)
        u.reader, u.tg_ready = None, False
        log.warning("Пользователь %s: Telegram отключился", u.id)
        with contextlib.suppress(Exception):
            await notify("⚠️ Telegram отключился — похоже, сеанс завершили. Подключить заново: «подключи телеграм».",
                         to=u.id)


async def load_reader(u: User) -> None:
    """При запуске: подключить сохранённый вход в Telegram, если он есть."""
    path = config.tg_session_path(u.id)
    if not Path(path + ".session").exists():
        return
    client = TelegramClient(path, config.API_ID, config.API_HASH)
    await client.connect()
    if await client.is_user_authorized():
        await start_reader(u, client)
    else:
        await client.disconnect()
        log.warning("Пользователь %s: вход в Telegram больше не действует", u.id)


def tg_connect() -> str:
    """Начать вход в Telegram по QR (QR придёт отдельным сообщением)."""
    u = me()
    if u.tg_ready:
        return f"Telegram уже подключён: {u.tg_name}."
    if u.tg_login and not u.tg_login.done():
        return "Уже жду сканирования QR — он выше."
    u.tg_login = asyncio.create_task(_tg_login(u))
    return "Сейчас пришлю QR для входа в Telegram."


async def _tg_login(u: User) -> None:
    with acting_as(u):
        path = config.tg_session_path(u.id)
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        client = TelegramClient(path, config.API_ID, config.API_HASH)
        qr_message = None
        try:
            await client.connect()
            if not await client.is_user_authorized():
                login = await client.qr_login()
                for _ in range(TG_QR_ATTEMPTS):
                    qr_message = await notify_photo(qr_png(login.url), TG_QR_HINT, edit=qr_message)
                    try:
                        await login.wait(timeout=30)
                        break
                    except TimeoutError:
                        await login.recreate()
                    except SessionPasswordNeededError:
                        u.state = ("tg_2fa", client)  # вход закончится в tg_password
                        await notify("🔐 На аккаунте включён облачный пароль. Пришлите его одним сообщением — "
                                     "я сразу удалю его из чата. /cancel — отмена.")
                        return
                else:
                    await client.disconnect()
                    await notify("QR истёк. Попробовать снова — «подключи телеграм».")
                    return
            await _tg_logged_in(u, client)
        except Exception as e:  # noqa: BLE001
            log.exception("Вход в Telegram пользователя %s", u.id)
            await client.disconnect()
            await notify(f"⚠️ Не удалось подключить Telegram: {e}")
        finally:
            if qr_message is not None:
                with contextlib.suppress(Exception):
                    await bot.delete_messages(u.id, qr_message)


async def _tg_logged_in(u: User, client: TelegramClient) -> None:
    await start_reader(u, client)
    await notify(f"✅ Telegram подключён: {u.tg_name}. Добавляйте чаты: «добавь Анну, её телеграм @anna».")


async def tg_password(u: User, password: str, event) -> None:
    """Облачный пароль для входа в Telegram: не логируем, в ИИ не отправляем, сообщение удаляем."""
    with contextlib.suppress(Exception):
        await event.delete()
    client = u.state[1]
    try:
        await client.sign_in(password=password)
    except PasswordHashInvalidError:
        await notify("Пароль не подошёл. Пришлите ещё раз или /cancel.")
        return
    except Exception as e:  # noqa: BLE001
        u.state = None
        await client.disconnect()
        await notify(f"⚠️ Не удалось войти: {e}")
        return
    u.state = None
    await _tg_logged_in(u, client)


# ================================================================ WhatsApp пользователей

WA_FRESH = timedelta(minutes=10)  # подсказки только на свежие сообщения, не на догруженные после переподключения


def wa_on_start() -> None:
    """Мост (пере)запущен: подключить сохранённые сессии."""
    for u in USERS.values():
        if u.status == "active" and wa.has_session(u.id):
            u.wa_status = "подключается"
            wa.send({"type": "start", "uid": u.id})


def request_wa_code(phone: str) -> str:
    """Привязка WhatsApp по коду. Код придёт отдельным сообщением (см. on_wa_event)."""
    u = me()
    jid = wa.normalize_phone(phone)
    if not jid:
        return "Нужен номер, на котором ваш WhatsApp, например +77011234567."
    if u.wa_status == "подключён":
        return "WhatsApp уже подключён."
    if not wa.send({"type": "pair_code", "uid": u.id, "phone": jid.split("@")[0]}):
        return "WhatsApp-модуль ещё запускается, попробуйте через минуту."
    u.wa_pairing, u.wa_status = True, "ждёт ввода кода"
    return "Запросил код привязки WhatsApp — придёт отдельным сообщением через несколько секунд."


async def on_wa_message(u: User, event: dict) -> None:
    contact = db.get_by_wa(u.id, event["jid"])
    if not contact:
        return  # не из белого списка: не сохраняем, в ИИ не отправляем
    when = datetime.fromtimestamp(event["ts"], timezone.utc)
    db.save_message(contact["id"], "WA", event["id"], when.isoformat(timespec="seconds"), event["out"], event["text"])
    if event.get("audio") and stt.enabled():
        wa.send({"type": "download", "uid": u.id, "id": event["id"]})  # аудио — только для белого списка
    if datetime.now(timezone.utc) - when > WA_FRESH:
        return
    if event["out"]:
        cancel_suggest(contact["id"])
    else:
        schedule_suggest(contact["id"])


async def on_wa_event(event: dict) -> None:
    u = USERS.get(event.get("uid"))
    if not u or u.status != "active":
        return
    with acting_as(u):
        await _wa_event(u, event)


async def _wa_event(u: User, event: dict) -> None:
    kind = event.get("type")
    if kind == "msg":
        await on_wa_message(u, event)
    elif kind == "media":  # аудио голосового, которое мы запросили
        text = await stt.transcribe(base64.b64decode(event["data"]), event.get("mime") or "audio/ogg",
                                    f"wa:{u.id}:{event['id']}")
        if text:
            db.set_message_text(event["id"], f"[голосовое] {text}")
    elif kind == "pair_code":
        if event.get("error"):
            u.wa_pairing = False
            await notify(f"⚠️ Не удалось получить код: {event['error']}")
        else:
            code = event["code"]
            await notify(
                f"🔑 Код привязки WhatsApp: {code[:4]}-{code[4:]}\n\n"
                "На телефоне: WhatsApp → Настройки → Связанные устройства → Привязка устройства → "
                "внизу «Связать по номеру телефона» → введите код. Код действует около двух минут."
            )
    elif kind == "status":
        status = event.get("status")
        if status == "open":
            if u.wa_pairing:
                u.wa_pairing = False
                await notify("✅ WhatsApp подключён. Добавляйте чаты: «добавь Анну, её вотсап 8701…».")
            if u.wa_status != "подключён":
                log.info("Пользователь %s: WhatsApp подключён", u.id)
            u.wa_status = "подключён"
        elif status == "close":
            if u.wa_status == "подключён":
                log.warning("Пользователь %s: WhatsApp прервался (код %s), переподключаюсь", u.id, event.get("code"))
                u.wa_status = "переподключается"
        elif status == "logged_out":
            log.warning("Пользователь %s: WhatsApp отвязан", u.id)
            u.wa_status = "не подключён"
            await notify("⚠️ WhatsApp отвязан от бота. Подключить снова: «подключи вотсап, мой номер …».")
        elif status == "pair_timeout":
            u.wa_status = "не подключён"
            if u.wa_pairing:
                u.wa_pairing = False
                await notify("Код привязки WhatsApp так и не ввели. Попросите новый: «подключи вотсап, мой номер …».")


def connections_text() -> str:
    u = me()
    tg = f"подключён ({u.tg_name})" if u.tg_ready else "не подключён"
    return f"✈️ Telegram: {tg}\n📱 WhatsApp: {u.wa_status}\n📸 Скриншоты: всегда можно присылать"


# ================================================================ консультации

def set_focus(new_focus) -> None:
    me().focus = new_focus


async def build_all_summary() -> str:
    parts = []
    for contact in db.list_contacts(me().id):
        msgs = await history(contact, limit=10)
        last = _fmt_msg(msgs[-1]) if msgs else "сообщений нет"
        parts.append(prompts.CONTACT_SUMMARY.format(
            name=contact["name"] + (" — постоянная девушка, режим «отношения»"
                                    if mode_of(contact) == "relationship" else " — знакомство"),
            facts=facts_of(contact), last=last, history=as_text(msgs),
        ))
    return "\n\n".join(parts) or "(список контактов пуст)"


async def deep_analysis(question: str, contact=None) -> str:
    """Вдумчивый ответ умной модели: по одному человеку (contact) или по всем перепискам. Отправляет и возвращает."""
    if contact:
        msgs = await history(contact, limit=80)
        context = prompts.CONSULT_CONTACT.format(name=contact["name"], facts=facts_of(contact), history=as_text(msgs))
    else:
        context = prompts.CONSULT_ALL.format(summary=await build_all_summary())
    # предыдущие реплики разговора — чтобы уточняющие вопросы понимались в контексте
    convo = [*me().dialog[-6:], {"role": "user", "content": question}]
    with contact_mode(contact):
        system = system_prompt()
    async with typing():
        answer = await llm.chat(system + "\n\n" + context, convo, max_tokens=2000, smart=True)
    await notify(answer)
    return answer


async def open_contact(cid: int) -> None:
    contact = own(cid)
    if not contact:
        await notify("Не нашёл такой контакт.", kb_contacts())
        return
    set_focus(("contact", cid))
    await notify(f"💬 {contact['name']}. Спрашивайте что угодно обычным текстом.", kb_contact(cid))


async def open_all() -> None:
    set_focus(("all", None))
    await notify("🌐 Вопрос по всем перепискам. Пишите обычным текстом.")


# ================================================================ действия

def channels_of(contact) -> str:
    parts = []
    if contact["chat_id"]:
        parts.append(f"Telegram @{contact['username']}" if contact["username"] else "Telegram")
    if contact["wa_jid"]:
        parts.append(f"WhatsApp {wa.jid_to_phone(contact['wa_jid'])}")
    if db.has_messages(contact["id"], "SS"):
        parts.append("скриншоты")
    return ", ".join(parts) or "пока без переписки"


async def show_card(contact) -> None:
    last = await history(contact, limit=1)
    last_line = _fmt_msg(last[0]) if last else "сообщений нет"
    await notify(
        f"📇 {contact['name']} ({channels_of(contact)}) · {MODES[mode_of(contact)]}\n\n{facts_of(contact)}\n\n"
        f"Последнее: {last_line}",
        kb_contact(contact["id"]),
    )


@per_contact
async def review(contact) -> str:
    msgs = await history(contact, limit=150)
    async with typing():
        answer = await llm.complete(
            system_prompt(),
            P("CHAT_REVIEW").format(name=contact["name"], facts=facts_of(contact), history=as_text(msgs)),
            max_tokens=2000, smart=True,
        )
    set_focus(("contact", contact["id"]))
    await notify(f"🔍 Разбор переписки с {contact['name']}:\n\n{answer}\n\nМожно задать уточняющий вопрос текстом.")
    return answer


@per_contact
async def draft_review(contact, draft: str) -> None:
    msgs = await history(contact)
    async with typing():
        answer = await llm.complete(
            system_prompt(),
            P("DRAFT_REVIEW").format(
                name=contact["name"], facts=facts_of(contact), history=as_text(msgs), draft=draft,
            ),
        )
    await notify(f"✍️ Разбор черновика:\n\n{answer}")


def ask_remove(contact):
    return (f"Убрать «{contact['name']}» и удалить {F('her')} карточку и сохранённую переписку?",
            [[Button.inline("Да, убрать", f"D:{contact['id']}".encode()),
              Button.inline("Отмена", f"c:{contact['id']}".encode())]])


async def remove(cid: int) -> None:
    contact = own(cid)
    if contact and db.remove_contact(cid):
        cancel_suggest(cid)
        if me().focus == ("contact", cid):
            set_focus(None)
        await notify(f"Убрал «{contact['name']}» вместе с карточкой.", kb_main())
    else:
        await notify("Не нашёл.", kb_main())


# ---------------------------------------------------------------- добавление контактов

ADD_FORMAT = "Формат:\n/add @username Имя — Telegram\n/add wa +77011234567 Имя — WhatsApp"


def _added(name: str, channel: str, attached: bool) -> str:
    contact = db.get_by_name(me().id, name)
    set_focus(("contact", contact["id"]))
    if attached:
        return f"Привязал {channel} к «{name}». Теперь это одна переписка: {channels_of(contact)}."
    return (f"Добавил: {name} ({channel}).\n"
            "Карточка заполнится при вечернем разборе; чтобы сразу — «Вечерний разбор» в меню.")


async def add_telegram_contact(ref: str, name: str) -> str:
    """Добавить чат Telegram в белый список (или привязать к человеку с тем же именем). Возвращает итог."""
    u = me()
    name = " ".join(name.split())
    if not u.tg_ready:
        return "Telegram ещё не подключён — сначала «подключи телеграм»."
    try:
        entity = await u.reader.get_entity(ref)
    except Exception:  # noqa: BLE001
        return f"Не нашёл {ref}. Подключённый аккаунт Telegram должен видеть этого пользователя."
    if not isinstance(entity, TgUser) or entity.bot:
        return "Можно добавить только личный чат с человеком (не группу и не бота)."
    if db.get_by_chat(u.id, entity.id):
        return "Этот чат уже в списке."
    channel = f"Telegram @{entity.username or entity.id}"
    existing = db.get_by_name(u.id, name)
    if existing and existing["chat_id"]:
        return f"У «{name}» уже есть Telegram. Выберите другое имя."
    if existing:
        db.set_telegram(existing["id"], entity.id, entity.username)
    else:
        db.add_contact(u.id, name, chat_id=entity.id, username=entity.username)
    return _added(name, channel, attached=bool(existing))


async def add_whatsapp_contact(phone: str, name: str) -> str:
    """Добавить номер WhatsApp в белый список (или привязать к человеку с тем же именем). Возвращает итог."""
    u = me()
    jid = wa.normalize_phone(phone)
    name = " ".join(name.split())
    if not jid:
        return f"«{phone}» не похоже на номер телефона."
    if not name:
        return "Нужно имя, под которым записать человека."
    if db.get_by_wa(u.id, jid):
        return "Этот номер уже в списке."
    channel = f"WhatsApp {wa.jid_to_phone(jid)}"
    existing = db.get_by_name(u.id, name)
    if existing and existing["wa_jid"]:
        return f"У «{name}» уже есть WhatsApp. Выберите другое имя."
    if existing:
        db.set_whatsapp(existing["id"], jid)
    else:
        db.add_contact(u.id, name, wa_jid=jid)
    text = _added(name, channel, attached=bool(existing))
    if u.wa_status != "подключён":
        return text + f"\nWhatsApp сейчас: {u.wa_status}. Сообщения начнут сохраняться после подключения."
    return text + "\nWhatsApp не отдаёт старую переписку — история начнётся с новых сообщений."


async def _notify_added(text: str) -> None:
    f = me().focus
    await notify(text, kb_contact(f[1]) if f and f[0] == "contact" else None)


# ================================================================ текстовые команды

async def _need_contact(name: str):
    contact = db.get_by_name(me().id, name)
    if not contact:
        await notify(f"«{name}» нет в списке.", kb_contacts())
    return contact


async def cmd_add(args: list[str]) -> None:
    if args and args[0].lower() in ("wa", "whatsapp"):
        args = args[1:]
    elif not (args and re.fullmatch(r"\+?[\d\-() ]{10,}", args[0])):  # не номер — значит Telegram
        if len(args) < 2:
            await notify(ADD_FORMAT)
            return
        await _notify_added(await add_telegram_contact(args[0], " ".join(args[1:])))
        return
    phone_parts = []  # номер можно писать с пробелами: /add wa +7 701 123 45 67 Имя
    while args and re.fullmatch(r"\+?[\d\-()]+", args[0]):
        phone_parts.append(args.pop(0))
    if not phone_parts or not args:
        await notify(ADD_FORMAT)
        return
    await _notify_added(await add_whatsapp_contact("".join(phone_parts), " ".join(args)))


async def cmd_menu(_):
    await notify("Главное меню", kb_main())


async def cmd_help(_):
    await notify(HELP + (HELP_ADMIN if me().is_admin else ""), kb_main())


async def cmd_list(_):
    if not db.list_contacts(me().id):
        await notify("Список пуст. Напишите, например: «добавь Анну, её вотсап 8 701 123 45 67».")
        return
    await notify(f"👥 {F('people')}:", kb_contacts())


async def cmd_card(args):
    if contact := await _need_contact(" ".join(args)):
        await show_card(contact)


async def cmd_suggest(args):
    if contact := await _need_contact(" ".join(args)):
        await suggest(contact)


async def cmd_review(args):
    if contact := await _need_contact(" ".join(args)):
        await review(contact)


async def cmd_draft(args):
    if len(args) < 2:
        await notify("Формат: /draft Имя текст черновика")
        return
    if contact := await _need_contact(args[0]):
        await draft_review(contact, " ".join(args[1:]))


async def cmd_remove(args):
    if contact := await _need_contact(" ".join(args)):
        await notify(*ask_remove(contact))


async def cmd_all(_):
    await open_all()


async def cmd_done(_):
    set_focus(None)
    await notify("Хорошо, закончили.", kb_main())


async def cmd_daily(_):
    await notify("Запускаю вечерний разбор…")
    await daily_run()


async def cmd_connect(_):
    await notify(connections_text() + "\n\nЧто подключить?", kb_connect())


async def cmd_wa(_):
    await cmd_connect(_)


async def cmd_wa_code(args):
    await notify(request_wa_code("".join(args)) if args else "Формат: /wa_code +77011234567 — номер вашего WhatsApp")


async def cmd_settings(_):
    u = me()
    await notify(f"⚙️ Настройки\n\nРежим по умолчанию: {MODES.get(u.mode, u.mode)} "
                 "(для конкретного человека — в его карточке).\n"
                 f"Переписываетесь с {FORMS[u.partner]['partners']}.\n"
                 f"Грамотность вариантов: {humanize.LABELS.get(u.literacy, u.literacy)}.\n"
                 f"О вас: {u.about or 'пока ничего'}", kb_settings())


ABOUT_ASK = ("👤 Расскажите о себе одним сообщением (можно голосовым) — варианты ответа будут звучать как вы, "
             "а не как ИИ: 20-летний и 45-летний пишут по-разному.\n\n"
             "Например: «34 года, Алматы, айтишник, люблю горы и бокс, пишу коротко, со скобками вместо смайлов».\n"
             "/skip — пропустить, потом можно через /me.")


async def ask_about(onboarding: bool = False) -> None:
    me().state = ("about", onboarding)
    await notify(ABOUT_ASK)


async def save_about(text: str) -> None:
    u = me()
    onboarding = bool(u.state and u.state[0] == "about" and u.state[1])
    u.state = None
    if text:
        u.about = " ".join(text.split())[:1500]
        db.set_user(u.id, about=u.about)
        await notify(f"✅ Запомнил о вас: {u.about}\n\nДополнять можно обычной фразой: «запомни про меня: …».")
    if onboarding:
        await notify("Теперь подключите, откуда читать переписку (можно и то, и другое):", kb_connect())


async def cmd_me(args):
    if args:  # «/me 34 года, Алматы…» — сразу записать
        await save_about(" ".join(args))
        return
    await notify(f"👤 О вас: {me().about or 'пока ничего'}", [[Button.inline("✏️ Заполнить заново", b"ab")]])


async def cmd_delete_me(_):
    if me().is_admin:
        await notify("Администратора так удалить нельзя.")
        return
    await notify("Удалить все ваши данные (контакты, карточки, сохранённую переписку) и отключить "
                 "Telegram и WhatsApp? Это необратимо.",
                 [[Button.inline("Да, удалить всё", b"ZZ"), Button.inline("Отмена", b"M")]])


async def cmd_users(_):
    if not me().is_admin:
        await notify("Команда только для администратора.")
        return
    rows = db.list_users()
    await notify(f"👥 Пользователей: {len(rows)} из {config.MAX_USERS}. Лимит: {config.USER_DAILY_LIMIT} запросов в день.")
    for row in rows:
        u = USERS.get(row["id"])
        tg = f"✅ {u.tg_name}" if u and u.tg_ready else "—"
        wa_ = u.wa_status if u else "—"
        text = (f"{'👑' if row['role'] == 'admin' else '👤'} {row['name']} (ID {row['id']})"
                f"{' — ЗАБЛОКИРОВАН' if row['status'] == 'blocked' else ''}\n"
                f"Telegram: {tg} · WhatsApp: {wa_}\n"
                f"Контактов: {len(db.list_contacts(row['id']))} · запросов сегодня: {db.usage_today(row['id'])}")
        buttons = None
        if row["role"] != "admin":
            block = (Button.inline("▶️ Разблокировать", f"uu:{row['id']}".encode()) if row["status"] == "blocked"
                     else Button.inline("⏸ Заблокировать", f"ub:{row['id']}".encode()))
            buttons = [[block, Button.inline("🗑 Удалить", f"ud:{row['id']}".encode())]]
        await notify(text, buttons)


COMMANDS = {
    "/start": cmd_help, "/help": cmd_help, "/menu": cmd_menu, "/add": cmd_add, "/list": cmd_list,
    "/card": cmd_card, "/suggest": cmd_suggest, "/review": cmd_review, "/draft": cmd_draft,
    "/remove": cmd_remove, "/all": cmd_all, "/done": cmd_done, "/daily": cmd_daily, "/wa": cmd_wa,
    "/wa_code": cmd_wa_code, "/connect": cmd_connect, "/settings": cmd_settings,
    "/delete_me": cmd_delete_me, "/users": cmd_users, "/me": cmd_me,
}


# ================================================================ доступ и администрирование

async def on_stranger(event) -> None:
    """Пишет человек, которого нет среди пользователей: запрос доступа администратору."""
    uid = event.sender_id
    if uid in _access_requests:
        await bot.send_message(uid, "Запрос уже у владельца — напишу, как только он ответит.")
        return
    if len(USERS) >= config.MAX_USERS:
        await bot.send_message(uid, "Сейчас все места в тесте заняты. Попробуйте позже.")
        return
    sender = await event.get_sender()
    name = " ".join(filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)])) or "Без имени"
    username = f"@{sender.username}" if getattr(sender, "username", None) else "без username"
    _access_requests[uid] = name
    await bot.send_message(uid, "👋 Это закрытый тест копилота переписки. Отправил владельцу запрос на доступ — "
                                "напишу, как только он ответит.")
    await notify(f"🙋 Запрос доступа: {name} ({username}, ID {uid})",
                 [[Button.inline("✅ Пустить", f"ua:{uid}".encode()), Button.inline("❌ Отказать", f"ur:{uid}".encode())]],
                 to=config.ADMIN_ID)


async def approve(uid: int) -> None:
    if uid in USERS:
        await notify("Уже пользователь.")
        return
    if len(USERS) >= config.MAX_USERS:
        await notify(f"Мест нет: уже {len(USERS)} из {config.MAX_USERS}.")
        return
    name = _access_requests.pop(uid, "Пользователь")
    db.add_user(uid, name)
    u = USERS[uid] = _user_from_row(db.get_user(uid))
    await notify(f"✅ Пустил: {name}.")
    with acting_as(u):
        await notify(f"🎉 Доступ открыт, {name}!\n\nЯ копилот переписки: читаю только те чаты, которые вы сами "
                     "добавите в список, подсказываю, что ответить, разбираю переписку и напоминаю, "
                     "если разговор затих. Сам никому не пишу.\n\nЧтобы советы были точнее — с кем вы переписываетесь?",
                     kb_partner())


async def reject(uid: int) -> None:
    name = _access_requests.pop(uid, None)
    await notify(f"Отказал: {name or uid}.")
    with contextlib.suppress(Exception):
        await bot.send_message(uid, "Владелец пока не открыл вам доступ.")


async def set_blocked(uid: int, blocked: bool) -> None:
    u = USERS.get(uid)
    if not u or u.is_admin:
        return
    u.status = "blocked" if blocked else "active"
    db.set_user(uid, status=u.status)
    await notify(f"{'⏸ Заблокирован' if blocked else '▶️ Разблокирован'}: {u.name}.")


async def delete_user_data(uid: int) -> None:
    """Всё о пользователе: вход в Telegram (сеанс завершается), привязка WhatsApp, данные в базе, файлы."""
    u = USERS.pop(uid, None)
    if u and u.reader:
        client, u.reader, u.tg_ready = u.reader, None, False
        with contextlib.suppress(Exception):
            await client.log_out()  # сеанс исчезнет из «Устройств» в Telegram
    wa.send({"type": "logout", "uid": uid})  # устройство исчезнет из «Связанных устройств» WhatsApp
    for contact in db.list_contacts(uid):
        cancel_suggest(contact["id"])
    db.delete_user(uid)
    shutil.rmtree(config.SESSIONS_DIR / str(uid), ignore_errors=True)
    log.info("Пользователь %s удалён со всеми данными", uid)


# ================================================================ агент: свободный диалог с инструментами

def _tool(tool_name: str, description: str, /, **params: str) -> dict:
    """Описание инструмента для модели. params: имя -> описание; «?» в начале — необязательный."""
    props, required = {}, []
    for key, desc in params.items():
        optional = desc.startswith("?")
        props[key] = {"type": "integer" if key == "limit" else "string", "description": desc.lstrip("?")}
        if not optional:
            required.append(key)
    return {"name": tool_name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": required}}


TOOLS = [
    _tool("list_contacts", "Белый список: имена, откуда переписка, когда и кто писал последним."),
    _tool("get_chat", "Карточка человека и последние сообщения переписки (Telegram, WhatsApp и скриншоты вместе).",
          name="имя из белого списка", limit="?сколько последних сообщений, по умолчанию 40, максимум 150"),
    _tool("add_whatsapp_contact", "Добавить человека в белый список WhatsApp. Если человек с таким именем уже есть "
          "(например, из Telegram) — WhatsApp привяжется к нему.", phone="номер телефона", name="имя"),
    _tool("add_telegram_contact", "Добавить чат Telegram в белый список по @username. Если человек с таким именем "
          "уже есть — Telegram привяжется к нему.", username="@username", name="имя"),
    _tool("remove_contact", "Убрать человека из белого списка вместе с карточкой и сохранённой перепиской. "
          "Пользователю придёт кнопка подтверждения.", name="имя"),
    _tool("remember", "Записать факт в карточку человека («запомни, что…»).", name="имя", fact="что записать"),
    _tool("set_contact_mode", "Сменить режим для человека: relationship — постоянная девушка (пара), "
          "dating — знакомство. «Аня — моя девушка», «с Машей просто знакомлюсь».",
          name="имя", mode="relationship или dating"),
    _tool("remember_about_me", "Записать факт о самом пользователе в его карточку (возраст, город, работа, "
          "увлечения, манера переписки). От неё зависят варианты сообщений.", fact="что записать"),
    _tool("suggest_replies", "Подготовить варианты ответа человеку — придут пользователю с кнопками.",
          name="имя", wish="?пожелание к вариантам: тон, тема, длина, «позвать в кино» и т.п."),
    _tool("review_draft", "Разобрать черновик сообщения пользователя человеку.", name="имя", draft="текст черновика"),
    _tool("deep_analysis", "Вдумчивый разбор умной моделью: сложный вопрос о человеке или по всем перепискам сразу. "
          "Ответ придёт пользователю.", question="вопрос пользователя с нужным контекстом",
          name="?имя; без имени — по всем перепискам"),
    _tool("review_chat", "Полный разбор переписки с человеком: интерес, сильные стороны, что изменить, "
          "уместна ли встреча. Придёт пользователю.", name="имя"),
    _tool("daily_report", "Вечерний разбор прямо сейчас: обновить карточки, разобрать день, напомнить о паузах."),
    _tool("save_screenshot", "Сохранить последний присланный скриншот переписки в историю человека "
          "(если такого нет в списке — создать). Повторы отсеиваются.", name="имя человека"),
    _tool("connections_status", "Что подключено: Telegram, WhatsApp."),
    _tool("whatsapp_link_code", "Подключить WhatsApp: запросить код привязки (придёт пользователю отдельно).",
          phone="номер WhatsApp пользователя"),
    _tool("telegram_connect", "Подключить Telegram пользователя: пришлёт QR для входа (сканировать с другого экрана)."),
]


def _find_contact(name: str):
    """Контакт текущего пользователя по имени: точно, а если нет — по единственному частичному совпадению."""
    uid = me().id
    if contact := db.get_by_name(uid, name or ""):
        return contact
    key = " ".join((name or "").split()).casefold()
    matches = [c for c in db.list_contacts(uid) if key and (key in c["name_key"] or c["name_key"] in key)]
    return matches[0] if len(matches) == 1 else None


def _no_contact(name: str) -> str:
    names = ", ".join(c["name"] for c in db.list_contacts(me().id)) or "список пуст"
    return f"«{name}» нет в белом списке. Есть: {names}."


async def call_tool(name: str, args: dict) -> str:
    """Выполнить инструмент по просьбе модели. Возвращает текст результата для неё."""
    log.info("Пользователь %s: агент вызывает %s", me().id, name)
    try:
        if name == "list_contacts":
            lines = []
            for c in db.list_contacts(me().id):
                last = await history(c, limit=1)
                when = f"последнее: {_fmt_msg(last[0])[:120]}" if last else "сообщений нет"
                lines.append(f"- {c['name']} ({channels_of(c)}); {when}")
            return "\n".join(lines) or "Белый список пуст."
        if name == "connections_status":
            return connections_text()
        if name == "whatsapp_link_code":
            return request_wa_code(args.get("phone", ""))
        if name == "telegram_connect":
            return tg_connect()
        if name == "daily_report":
            await daily_run()
            return "Отчёт отправлен пользователю."
        if name == "add_whatsapp_contact":
            return await add_whatsapp_contact(args.get("phone", ""), args.get("name", ""))
        if name == "add_telegram_contact":
            return await add_telegram_contact(args.get("username", ""), args.get("name", ""))
        if name == "save_screenshot":
            return save_screenshot(args.get("name", ""))
        if name == "remember_about_me":
            u = me()
            fact = " ".join(args.get("fact", "").split())
            u.about = f"{u.about}; {fact}".strip("; ")[:1500]
            db.set_user(u.id, about=u.about)
            return f"Записал о пользователе: {fact}"
        if name == "deep_analysis" and not args.get("name"):
            set_focus(("all", None))
            await deep_analysis(args.get("question", ""))
            return "Ответ отправлен пользователю."

        contact = _find_contact(args.get("name", ""))
        if not contact:
            return _no_contact(args.get("name", ""))
        set_focus(("contact", contact["id"]))
        if name == "get_chat":
            limit = max(1, min(int(args.get("limit") or 40), 150))
            msgs = await history(contact, limit=limit)
            return (f"{contact['name']} ({channels_of(contact)})\nКарточка:\n{facts_of(contact)}\n\n"
                    f"Переписка (старые сверху):\n{as_text(msgs)}")
        if name == "remove_contact":
            await notify(*ask_remove(contact))
            return "Пользователю отправлена кнопка подтверждения; контакт удалится, только если он её нажмёт."
        if name == "set_contact_mode":
            mode = args.get("mode", "")
            if mode not in MODES:
                return "mode должен быть relationship или dating."
            db.set_contact_mode(contact["id"], mode)
            return f"Режим для {contact['name']}: {MODES[mode]}."
        if name == "remember":
            fact = " ".join(args.get("fact", "").split())
            db.update_facts(contact["id"], (contact["facts"].rstrip() + f"\n- {fact}").strip())
            return f"Записал в карточку {contact['name']}: {fact}"
        if name == "suggest_replies":
            await suggest(contact, args.get("wish", ""))
            return "Варианты отправлены пользователю."
        if name == "review_draft":
            await draft_review(contact, args.get("draft", ""))
            return "Разбор черновика отправлен пользователю."
        if name == "deep_analysis":
            await deep_analysis(args.get("question", ""), contact)
            return "Ответ отправлен пользователю."
        if name == "review_chat":
            await review(contact)
            return "Разбор отправлен пользователю."
        return f"Нет инструмента {name}."
    except llm.LimitReached:
        raise  # лимит — не ошибка инструмента: заканчиваем ход целиком
    except Exception as e:  # noqa: BLE001 — ошибку отдаём модели, пусть объяснит пользователю
        log.exception("Ошибка инструмента %s", name)
        return f"Ошибка: {e}"


def _focus_text() -> str:
    f = me().focus
    if f and f[0] == "contact" and (c := own(f[1])):
        return c["name"]
    return "все переписки" if f else "не выбран"


async def agent_turn(text: str) -> None:
    """Сообщение пользователя обычным языком: модель сама решает, какие инструменты вызвать."""
    u = me()
    async with u.lock:
        contacts = ", ".join(f"{c['name']} ({channels_of(c)}; режим: {MODES[mode_of(c)][2:].lower()})"
                             for c in db.list_contacts(u.id)) or "пока нет"
        focus = own(u.focus[1]) if u.focus and u.focus[0] == "contact" else None
        with contact_mode(focus):  # разговор о конкретном человеке — в его режиме
            system = system_prompt()
        system += "\n\n" + prompts.AGENT.format(
            now=datetime.now(TZ).strftime("%d.%m.%Y %H:%M, %A"), focus=_focus_text(), contacts=contacts,
        )
        u.turn_log = []
        try:
            async with typing():
                answer = await llm.run_tools(system, [*u.dialog, {"role": "user", "content": text}], TOOLS, call_tool)
            if answer:
                answer = humanize_answer(answer, need_quotes=True)
                variants = extract_variants(answer, need_quotes=True)  # варианты в свободном ответе (скриншоты)
                await notify(answer, kb_copy(variants) if len(variants) >= 2 else None)
        finally:
            shown, u.turn_log = u.turn_log, None
        # в историю — всё, что пользователь увидел за ход (варианты, разборы, ответ), чтобы понимать «второй вариант»
        u.dialog.append({"role": "user", "content": text})
        u.dialog.append({"role": "assistant", "content": "\n\n".join(shown)[-6000:] or "(готово)"})
        del u.dialog[:-DIALOG_MAX]


# ================================================================ скриншоты и голосовые боту

MAX_SCREENSHOTS = 10


def _is_image(message) -> bool:
    """Фото — сжатое или отправленное файлом (PNG/JPEG)."""
    return bool(message.photo) or bool(message.file and (message.file.mime_type or "") in ("image/png", "image/jpeg"))


def _parse_json(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    try:
        return json.loads(text[start : end + 1]) if 0 <= start < end else None
    except ValueError:
        return None


def _why_not(raw: str | None) -> str:
    """Для журнала — почему скриншот не прочитан. Длинный ответ может содержать переписку: только длина."""
    if raw is None:
        return "нет ответа"
    raw = " ".join(raw.split())
    what = "пусто: ни переписки, ни анкеты" if _useful(_parse_json(raw)) is False else "не разобрать"
    return f"{what}: «{raw}»" if len(raw) <= 200 else f"{what}, {len(raw)} символов"


def _useful(parsed: dict | None) -> bool | None:
    """Есть ли в распознанном переписка или анкета. None — ответ вообще не JSON."""
    if parsed is None:
        return None
    return bool(parsed.get("messages") or parsed.get("profile"))


async def _read_screenshots(prompt: str, images: list[bytes]) -> dict | None:
    """Скриншоты -> переписка. Сначала основная модель, при неудаче (отказ, не JSON) — Gemini."""
    raw = None
    try:
        raw = await llm.vision(prompt, images)
    except llm.LimitReached:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("Скриншот: основная модель — ошибка %s", e)
    parsed = _parse_json(raw or "")
    if _useful(parsed):
        return parsed
    log.warning("Скриншот: основная модель не прочитала (%d шт., %s) — пробую Gemini", len(images), _why_not(raw))
    raw = await stt.read_images(prompt, images)
    parsed = _parse_json(raw or "")
    if _useful(parsed):
        return parsed
    log.warning("Скриншот: Gemini тоже не прочитал (%s)", _why_not(raw))
    return None


async def handle_screenshots(messages: list, caption: str) -> None:
    """Скриншоты переписки: распознаём текст (картинки нигде не храним) и передаём агенту."""
    u = me()
    images = [await bot.download_media(m, file=bytes) for m in messages[:MAX_SCREENSHOTS]]
    today = datetime.now(TZ).date()
    prompt = (prompts.SCREENSHOT.replace("<TODAY>", today.strftime("%d.%m.%Y"))
              .replace("<YESTERDAY>", (today - timedelta(days=1)).strftime("%d.%m.%Y")))
    async with typing():
        parsed = await _read_screenshots(prompt, images)
    if not _useful(parsed):
        await notify("Не нашёл на скриншотах ни переписки, ни анкеты. Пришлите скриншот чата или профиля "
                     "крупнее, без обрезки.")
        return
    parsed["messages"] = parsed.get("messages") or []
    u.last_screenshot = parsed
    msgs, profile = parsed["messages"], parsed.get("profile")
    app, name = parsed.get("app") or "приложение не определено", parsed.get("name") or "имя не видно"
    last_theirs = next((m["text"] for m in reversed(msgs) if not m.get("out")), None)
    found = ([f"{len(msgs)} сообщ."] if msgs else []) + (["анкета"] if profile else [])
    summary = ", ".join(found)

    # Сохранение: без подписи и с именем на скриншоте — сразу в базу; имя занято — спрашиваем.
    # С подписью («это Дина») решает агент по подписи; без имени — отвечаем разово.
    header_name = _screenshot_name(parsed.get("name"))
    saved = ""
    if caption:
        save_note = "по подписи пользователя (если в ней есть имя — сохрани через save_screenshot)"
    elif not header_name:
        save_note = "не сохранён: имени на скриншоте нет — в конце предложи сохранить"
    elif db.get_by_name(u.id, header_name):
        save_note = "не сохраняй: пользователя спрашивают кнопками, та ли это девушка из списка"
    else:
        saved = save_screenshot(header_name, exact=True)
        save_note = f"уже сохранён ({saved}) — save_screenshot не вызывай"
    await notify(f"📸 {app}, {name}: {summary}{'' if summary.endswith('.') else '.'}"
                 + (f" Последнее от собеседника: «{last_theirs[:150]}»" if last_theirs else "")
                 + (f"\n💾 {saved}" if saved else ""))
    if not caption and header_name and not saved:
        await ask_screenshot_owner(header_name)

    transcript = "\n".join(f"{'Я' if m.get('out') else F('she')}: {m.get('text', '')}" for m in msgs)
    what = "скриншот переписки" if msgs else "скриншот анкеты (переписки ещё нет)"
    hint = ("вероятно, нужны варианты ответа на последнее сообщение собеседника" if msgs
            else "вероятно, нужно первое сообщение")
    await agent_turn(
        f"[Прислал {what}. Приложение: {app}. Имя: {name}. Сохранение: {save_note}]\n"
        + (f"Анкета: {profile}\n" if profile else "")
        + (f"Переписка:\n{transcript[-6000:]}\n" if msgs else "")
        + f"\nПодпись: {caption or f'(без подписи — {hint})'}"
    )


def _screenshot_times(msgs: list[dict]) -> list[datetime]:
    """Время для каждого сообщения скриншота: из даты/времени на картинке, иначе по порядку."""
    now = datetime.now(TZ)
    parsed: list[datetime | None] = []
    for m in msgs:
        dt = None
        try:
            if m.get("time"):
                hh, mm = map(int, m["time"].split(":")[:2])
                day = datetime.strptime(m["date"], "%d.%m.%Y").date() if m.get("date") else now.date()
                dt = datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)
                if not m.get("date") and dt > now:
                    dt -= timedelta(days=1)  # время без даты «позже, чем сейчас» — значит, вчера
        except (ValueError, TypeError):
            dt = None
        parsed.append(dt)
    # пропуски заполняем по порядку: +1 секунда к предыдущему (или назад от первого известного/сейчас)
    known = next((d for d in parsed if d), now - timedelta(seconds=len(msgs)))
    result, prev = [], None
    for i, dt in enumerate(parsed):
        dt = dt or (prev + timedelta(seconds=1) if prev else known - timedelta(seconds=len(msgs) - i))
        result.append(dt)
        prev = dt
    return result


def _screenshot_name(raw) -> str:
    """Имя из шапки или анкеты без возраста: «Yunona, 32» -> «Yunona»."""
    name = re.sub(r"[,\s]*\d+\s*$", "", str(raw or "")).strip(" ,")
    return " ".join(name.split())


async def ask_screenshot_owner(name: str) -> None:
    """Имя со скриншота уже занято — спросить, та же это девушка или другая."""
    await notify(f"В списке уже есть «{name}». Это {F('she').lower()} же?",
                 [[Button.inline(f"Да, дописать к «{name}»"[:60], b"ss:same")],
                  [Button.inline("Нет, записать под другим именем", b"ss:new")]])
    me().state = ("ss_owner", name)


async def screenshot_owner_answer(same: bool) -> None:
    u = me()
    if not (u.state and u.state[0] == "ss_owner"):
        await notify("Этот вопрос уже неактуален.")
        return
    name = u.state[1]
    if same:
        u.state = None
        await notify(f"💾 {save_screenshot(name, exact=True)}")
    else:
        u.state = ("ss_name",)
        await notify(f"Как записать? Пришлите имя, например «{name} Tinder». /cancel — не сохранять.")


async def screenshot_new_name(text: str) -> None:
    """Имя для скриншота, если прежнее было занято."""
    name = _screenshot_name(text)
    if not name:
        await notify("Пришлите имя текстом или /cancel.")
    elif db.get_by_name(me().id, name):
        await ask_screenshot_owner(name)
    else:
        me().state = None
        await notify(f"💾 {save_screenshot(name, exact=True)}")


def save_screenshot(name: str, exact: bool = False) -> str:
    """Сохранить последний скриншот человеку name. exact — только точное совпадение имени (иначе создать)."""
    u = me()
    if not u.last_screenshot:
        return "Нет распознанного скриншота."
    name = " ".join(name.split())
    contact = db.get_by_name(u.id, name) if exact else _find_contact(name)
    created = contact is None
    if created:
        db.add_contact(u.id, name)
        contact = db.get_by_name(u.id, name)
    set_focus(("contact", contact["id"]))
    msgs = u.last_screenshot["messages"]
    new = 0
    for m, dt in zip(msgs, _screenshot_times(msgs)):
        fingerprint = f"{contact['id']}|{bool(m.get('out'))}|{m.get('date')}|{m.get('time')}|{m.get('text')}"
        ext_id = "ss:" + hashlib.sha1(fingerprint.encode()).hexdigest()[:20]
        new += db.save_message(contact["id"], "SS", ext_id, dt.astimezone(timezone.utc).isoformat(timespec="seconds"),
                               bool(m.get("out")), m.get("text", ""))
    result = [f"{'Создал контакт «' + contact['name'] + '»' if created else contact['name']}:"]
    if msgs:
        result.append(f"сохранил {new} сообщ. ({len(msgs) - new} уже были)")
    if profile := u.last_screenshot.get("profile"):
        app = u.last_screenshot.get("app") or "приложение"
        note = f"Анкета ({app}): {' '.join(str(profile).split())}"
        if note not in contact["facts"]:
            db.update_facts(contact["id"], (contact["facts"].rstrip() + f"\n- {note}").strip())
        result.append("анкету записал в карточку")
    return " ".join(result[:1]) + " " + ", ".join(result[1:]) + "."


async def handle_voice(message) -> None:
    """Голосовое боту: расшифровываем и отвечаем, как на текст."""
    if not stt.enabled():
        await notify("Распознавание речи не настроено (STT_API_KEY в .env).")
        return
    async with bot.action(me().id, "record-audio"):
        audio = await bot.download_media(message, file=bytes)
        text = await stt.transcribe(audio, message.file.mime_type or "audio/ogg")
    if not text:
        await notify("Не расслышал — попробуйте ещё раз.")
        return
    await notify(f"🎤 «{text}»")
    if me().state and me().state[0] == "about":
        await save_about(text)
        return
    await agent_turn(text)


# ================================================================ обработчики бота

def _active_user(sender_id: int) -> User | None:
    u = USERS.get(sender_id)
    return u if u and u.status == "active" else None


async def on_bot_message(event) -> None:
    u = USERS.get(event.sender_id)
    if u is None:
        await on_stranger(event)
        return
    if u.status != "active":
        await bot.send_message(event.sender_id, "Доступ приостановлен владельцем.")
        return
    if event.grouped_id:
        return  # часть альбома — его целиком обработает on_bot_album
    with acting_as(u):
        text = (event.raw_text or "").strip()
        try:
            if text.lower() == "/cancel":
                if u.state and u.state[0] == "tg_2fa":
                    await u.state[1].disconnect()
                u.state = None
                await notify("Отменил.", kb_main())
            elif u.state and u.state[0] == "tg_2fa" and text:
                await tg_password(u, text, event)  # облачный пароль — мимо логов и ИИ
            elif u.state and u.state[0] == "ss_name" and text and not text.startswith("/"):
                await screenshot_new_name(text)
            elif u.state and u.state[0] == "about" and text.lower() == "/skip":
                await save_about("")
            elif u.state and u.state[0] == "about" and text and not text.startswith("/"):
                await save_about(text)
            elif u.state and u.state[0] == "wa_phone" and text and not text.startswith("/"):
                u.state = None
                await notify(request_wa_code(text))
            elif _is_image(event.message):
                await handle_screenshots([event.message], text)
            elif event.voice or event.audio:
                await handle_voice(event.message)
            elif not text:
                return
            elif text.startswith("/"):
                try:
                    parts = shlex.split(text)
                except ValueError:
                    parts = text.split()
                command = parts[0].lower().split("@")[0]  # /menu@my_bot -> /menu
                if u.state and u.state[0] == "about":
                    u.state = None  # ушёл в другую команду — рассказ о себе больше не ждём
                handler = COMMANDS.get(command)
                if handler:
                    await handler(parts[1:])
                else:
                    await notify("Не знаю такой команды. /help — справка.")
            else:
                await agent_turn(text)
        except llm.LimitReached as e:
            await notify(f"⏸ {e}")
        except Exception as e:  # noqa: BLE001
            log.exception("Ошибка обработки сообщения")
            await notify(f"⚠️ Ошибка: {e}")


async def on_bot_album(event) -> None:
    """Несколько скриншотов одним альбомом."""
    u = _active_user(event.sender_id)
    if not u:
        return
    with acting_as(u):
        try:
            photos = [m for m in event.messages if _is_image(m)]
            if photos:
                await handle_screenshots(photos, event.text or "")
        except llm.LimitReached as e:
            await notify(f"⏸ {e}")
        except Exception as e:  # noqa: BLE001
            log.exception("Ошибка обработки альбома")
            await notify(f"⚠️ Ошибка: {e}")


async def on_bot_button(event) -> None:
    u = _active_user(event.sender_id)
    await event.answer()  # убираем «часики» на кнопке
    if not u:
        return
    parts = event.data.decode().split(":")
    kind, args = parts[0], parts[1:]
    with acting_as(u):
        try:
            await _button(u, kind, args)
        except llm.LimitReached as e:
            await notify(f"⏸ {e}")
        except Exception as e:  # noqa: BLE001
            log.exception("Ошибка кнопки")
            await notify(f"⚠️ Ошибка: {e}")


async def _button(u: User, kind: str, args: list[str]) -> None:
    if kind in ("ua", "ur", "ub", "uu", "ud", "uD"):  # кнопки администратора
        if not u.is_admin:
            return
        uid = int(args[0])
        if kind == "ua":
            await approve(uid)
        elif kind == "ur":
            await reject(uid)
        elif kind in ("ub", "uu"):
            await set_blocked(uid, kind == "ub")
        elif kind == "ud":
            target = USERS.get(uid)
            await notify(f"Удалить пользователя {target.name if target else uid} со всеми данными?",
                         [[Button.inline("Да, удалить", f"uD:{uid}".encode()), Button.inline("Отмена", b"M")]])
        elif kind == "uD" and uid != config.ADMIN_ID:
            await delete_user_data(uid)
            await notify("Удалён со всеми данными.")
        return

    if kind == "M":
        await cmd_menu([])
    elif kind == "H":
        await cmd_help([])
    elif kind == "L":
        await cmd_list([])
    elif kind == "A":
        await open_all()
    elif kind == "Y":
        await cmd_daily([])
    elif kind == "S":
        await cmd_connect([])
    elif kind == "p":
        u.partner = "m" if args[0] == "m" else "f"
        db.set_user(u.id, partner=u.partner)
        if not u.about:  # знакомство: сначала о самом пользователе, потом подключения
            await notify(f"Запомнил: вы переписываетесь с {FORMS[u.partner]['partners']}.")
            await ask_about(onboarding=True)
        else:
            await notify(f"Запомнил: вы переписываетесь с {FORMS[u.partner]['partners']}.\n\n"
                         "Теперь подключите, откуда читать переписку (можно и то, и другое):", kb_connect())
    elif kind == "pp":
        await notify(f"Сейчас: вы переписываетесь с {FORMS[u.partner]['partners']}. Изменить:", kb_partner())
    elif kind == "ab":
        await ask_about()
    elif kind == "md" and not args:
        await notify("💞 Режим помощника:\n\n🔍 Знакомства — переписка с новыми людьми: как заинтересовать, "
                     "когда позвать на встречу, варианты на каждое сообщение.\n\n"
                     "💞 Отношения — постоянная пара: внимание к её настроению и делам, поддержка, "
                     "сгладить спор, извиниться. Варианты приходят только на важные сообщения, "
                     "на остальные — по запросу. В карточке — важные даты, что она любит, что у неё сейчас.\n\n"
                     "Здесь — режим по умолчанию. Для конкретного человека его можно сменить в карточке: "
                     "👥 → имя → кнопка режима, или словами: «Аня — моя девушка».",
                     kb_mode())
    elif kind == "md" and args[0] in MODES:
        u.mode = args[0]
        db.set_user(u.id, mode=u.mode)
        text = f"✅ Режим по умолчанию: {MODES[u.mode]}."
        others = [c["name"] for c in db.list_contacts(u.id) if not c["mode"]]
        own_mode = [f"{c['name']} — {MODES[c['mode']]}" for c in db.list_contacts(u.id) if c["mode"]]
        if own_mode:
            text += "\nСвой режим остаётся у: " + ", ".join(own_mode) + "."
        if u.mode == "relationship":
            text += ("\n\nВарианты ответа буду присылать, только когда сообщение важное (она расстроена, делится "
                     "личным, напряжение, серьёзный вопрос). На остальные — по запросу: «что ответить?» или "
                     "кнопка в карточке. Под вариантами появятся «Поддержать», «Извиниться», «Сгладить». "
                     "Карточку перестрою в вечернем разборе.")
            if len(others) > 1:
                text += (f"\n\nВ этом режиме сейчас: {', '.join(others)}. Если с кем-то это знакомство — "
                         "смените режим в его карточке (👥 → имя).")
        await notify(text)
    elif kind == "g" and not args:
        await notify("✍️ Насколько грамотно писать варианты ответа? Идеальный текст в переписке выглядит "
                     "подозрительно — на 85 и 70 появятся пропущенные запятые и редкие опечатки. "
                     "«Как пишу я» — повторять вашу манеру из переписки.", kb_literacy())
    elif kind == "g" and args[0] in humanize.LEVELS:
        u.literacy = args[0]
        db.set_user(u.id, literacy=u.literacy)
        await notify(f"✅ Грамотность вариантов: {humanize.LABELS[u.literacy]}.")
    elif kind == "cw":
        u.state = ("wa_phone",)
        await notify("Пришлите номер телефона, на котором ваш WhatsApp (например, +7 701 123 45 67).\n"
                     "Я пришлю код — его нужно ввести в WhatsApp: Настройки → Связанные устройства → "
                     "Привязка устройства → «Связать по номеру телефона».")
    elif kind == "ct":
        await notify(tg_connect())
    elif kind == "ss":
        await screenshot_owner_answer(args[0] == "same")
    elif kind == "cs":
        await notify("📸 Просто присылайте скриншоты переписки или анкеты из любого приложения — один или альбомом. "
                     "Если на скриншоте видно имя, сохраню сам; если такое имя уже есть — спрошу, та же это "
                     "или другая. По анкете предложу первое сообщение.")
    elif kind == "ZZ" and not u.is_admin:
        await notify("Удаляю все данные и отключаю мессенджеры…")
        uid = u.id
        await delete_user_data(uid)
        await bot.send_message(uid, "Готово: всё удалено. Чтобы вернуться — напишите боту снова.")
    elif kind == "c":
        await open_contact(int(args[0]))
    elif kind == "cm":
        contact = own(int(args[0]))
        if not contact:
            await notify("Не нашёл такой контакт.", kb_contacts())
        elif len(args) == 1:
            await notify(f"Режим для {contact['name']}:", [
                [Button.inline(("✅ " if mode_of(contact) == key else "") + label, f"cm:{contact['id']}:{key}".encode())]
                for key, label in MODES.items()])
        elif args[1] in MODES:
            db.set_contact_mode(contact["id"], args[1])
            note = (" Варианты — только на важные сообщения, под ними «Поддержать», «Извиниться», «Сгладить»; "
                    "карточку перестрою в вечернем разборе." if args[1] == "relationship" else "")
            await notify(f"✅ {contact['name']}: {MODES[args[1]]}.{note}", kb_contact(contact["id"]))
    elif kind in ("s", "v", "k", "d"):
        contact = own(int(args[0]))
        if not contact:
            await notify("Не нашёл такой контакт.", kb_contacts())
        elif kind == "s":
            await suggest(contact)
        elif kind == "v":
            await review(contact)
        elif kind == "k":
            await show_card(contact)
        else:
            await notify(*ask_remove(contact))
    elif kind == "D":
        await remove(int(args[0]))
    elif kind == "r":
        await refine(int(args[0]), args[1])


# ================================================================ вечерний разбор

@per_contact
async def daily_for(contact, since: datetime, now: datetime) -> list[str]:
    """Обновляет карточку, делает разбор дня и проверяет паузы. Возвращает блоки отчёта."""
    blocks: list[str] = []
    name = contact["name"]
    day_msgs = await history(contact, limit=200, since=since)

    # 1. карточка: новая строится по всей недавней истории, существующая — по сообщениям за день
    facts_msgs = day_msgs if contact["facts"] else await history(contact, limit=150)
    if facts_msgs:
        new_facts = await llm.complete(
            system_prompt(),
            P("FACTS_UPDATE").format(name=name, facts=facts_of(contact), history=as_text(facts_msgs)),
            smart=True,
        )
        db.update_facts(contact["id"], new_facts)

    # 2. разбор ответов пользователя за день
    if day_msgs and any(m.out for m in day_msgs):
        sugg = db.suggestions_since(contact["id"], since.isoformat(timespec="seconds"))
        sugg_text = "\n\n".join(s["variants"] for s in sugg) or "(подсказок не было)"
        day_review = await llm.complete(
            system_prompt(),
            P("DAILY_REVIEW").format(name=name, history=as_text(day_msgs), suggestions=sugg_text),
            smart=True,
        )
        if day_review.strip(" .-—\n"):  # в отношениях «—» значит «всё в порядке, писать нечего»
            blocks.append(f"📝 {name}\n{day_review}")

    # 3. напоминания
    last = await history(contact, limit=1)
    if not last:
        return blocks
    last_msg = last[0]
    age = now - last_msg.date
    situation = None
    if not last_msg.out and age > timedelta(hours=config.WAITING_HOURS):
        situation = f"последним писал собеседник, {int(age.total_seconds() // 3600)} ч назад, ответа нет"
    elif age > timedelta(days=config.SILENCE_DAYS) and not is_rel():  # в отношениях общаются и вживую
        reminded = contact["last_reminder_at"]
        if not reminded or now - datetime.fromisoformat(reminded) > timedelta(days=config.SILENCE_DAYS):
            situation = f"переписка молчит {age.days} дн."
    if situation:
        fresh = db.get_by_id(contact["id"])  # карточка могла обновиться выше
        msgs = await history(contact, limit=15)
        tip = await llm.complete(
            system_prompt(),
            P("REMINDER").format(name=name, facts=facts_of(fresh), history=as_text(msgs), situation=situation),
            smart=True,
        )
        db.set_reminded(contact["id"])
        blocks.append(f"⏰ {name}: {situation}\n{tip}")
    return blocks


async def daily_run() -> None:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=1)
    report: list[str] = []
    for contact in db.list_contacts(me().id):
        try:
            report += await daily_for(contact, since, now)
        except llm.LimitReached:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("Ошибка разбора для %s", contact["name"])
            report.append(f"⚠️ {contact['name']}: ошибка разбора — {e}")
    body = "\n\n".join(report) if report else "Карточки обновлены, замечаний нет."
    await notify("📊 Вечерний разбор\n\n" + body, kb_main())


async def daily_loop() -> None:
    while True:
        now = datetime.now(TZ)
        target = now.replace(hour=config.DAILY_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        for u in list(USERS.values()):
            if u.status != "active" or not db.list_contacts(u.id):
                continue
            with acting_as(u):
                try:
                    await daily_run()
                except llm.LimitReached as e:
                    with contextlib.suppress(Exception):
                        await notify(f"⏸ Вечерний разбор не сделан: {e}")
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка вечернего разбора пользователя %s", u.id)


# ================================================================ расход

def _before_llm_call() -> None:
    """Перед каждым платным запросом: учёт и дневной лимит (администратору без лимита)."""
    u = cur.get(None)
    if u is None:
        return
    calls = db.add_usage(u.id)
    if not u.is_admin and calls > config.USER_DAILY_LIMIT:
        raise llm.LimitReached(f"Дневной лимит ({config.USER_DAILY_LIMIT} запросов к ИИ) исчерпан — продолжим завтра.")


# ================================================================ запуск

async def main() -> None:
    missing = config.check()
    if missing:
        raise SystemExit(f"Заполните в .env: {', '.join(missing)}")
    db.init()
    for row in db.list_users():
        USERS[row["id"]] = _user_from_row(row)
    llm.before_call = _before_llm_call

    global bot
    bot = TelegramClient(config.BOT_SESSION_NAME, config.API_ID, config.API_HASH)
    bot.add_event_handler(on_bot_message, events.NewMessage(incoming=True, func=lambda e: e.is_private))
    bot.add_event_handler(on_bot_button, events.CallbackQuery())
    bot.add_event_handler(on_bot_album, events.Album(func=lambda e: e.is_private))
    await bot.start(bot_token=config.BOT_TOKEN)
    bot_me = await bot.get_me()
    log.info("Бот: @%s. Пользователей: %d. LLM: %s / %s, %s", bot_me.username, len(USERS),
             config.LLM_PROVIDER, config.LLM_MODEL, config.LLM_MODEL_SMART)

    for u in USERS.values():
        if u.status == "active":
            try:
                await load_reader(u)
            except Exception:  # noqa: BLE001
                log.exception("Не удалось подключить Telegram пользователя %s", u.id)

    admin = USERS[config.ADMIN_ID]
    with acting_as(admin):
        try:
            await notify(f"🤖 Копилот запущен. Пользователей: {len(USERS)} из {config.MAX_USERS}.", kb_main())
        except Exception:  # noqa: BLE001
            log.warning("Не могу написать администратору. Откройте @%s и нажмите Start.", bot_me.username)

    asyncio.create_task(daily_loop())
    if config.WA_ENABLED:
        asyncio.create_task(wa.run(on_wa_event, wa_on_start))
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
