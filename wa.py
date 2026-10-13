"""WhatsApp через мост на Node.js (wa/bridge.mjs, библиотека Baileys). Только чтение.

Мост — дочерний процесс, один на всех пользователей: присылает события JSON-строками в stdout
(в каждом есть uid), а run() передаёт их в on_event. Если мост падает, он перезапускается.
"""
import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Awaitable, Callable

import config

log = logging.getLogger("copilot.wa")
WA_DIR = config.BASE_DIR / "wa"
_proc: asyncio.subprocess.Process | None = None


def send(command: dict) -> bool:
    """Команда мосту (например, запросить код привязки). False — мост не запущен."""
    if _proc is None or _proc.returncode is not None or _proc.stdin is None:
        return False
    _proc.stdin.write((json.dumps(command) + "\n").encode())
    return True


def normalize_phone(raw: str) -> str | None:
    """+7 701 123-45-67 / 87011234567 -> 77011234567@s.whatsapp.net. None, если не похоже на номер."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 11 and digits.startswith("8"):  # казахстанский/российский формат 8XXXXXXXXXX
        digits = "7" + digits[1:]
    elif len(digits) == 10:                           # номер без кода страны
        digits = "7" + digits
    if not 11 <= len(digits) <= 15:
        return None
    return f"{digits}@s.whatsapp.net"


def jid_to_phone(jid: str) -> str:
    return "+" + jid.split("@")[0]


async def _drain_stderr(stream: asyncio.StreamReader) -> None:
    async for line in stream:
        log.warning("wa-bridge: %s", line.decode(errors="replace").rstrip()[:500])


def has_session(user_id: int) -> bool:
    """Есть ли у пользователя сохранённая привязка WhatsApp."""
    return (Path(config.WA_SESSION_DIR) / str(user_id) / "creds.json").exists()


async def run(on_event: Callable[[dict], Awaitable[None]], on_start: Callable[[], None] | None = None) -> None:
    """Держит мост запущенным. on_start — после каждого запуска (подключить сохранённые сессии)."""
    node = shutil.which(config.NODE_BIN)
    if not node:
        log.warning("Node.js не найден (%s) — WhatsApp отключён.", config.NODE_BIN)
        return
    if not (WA_DIR / "node_modules").exists():
        log.warning("Нет зависимостей моста: выполните `npm install` в папке wa — WhatsApp отключён.")
        return

    global _proc
    while True:
        proc = _proc = await asyncio.create_subprocess_exec(
            node, str(WA_DIR / "bridge.mjs"), config.WA_SESSION_DIR,
            cwd=str(WA_DIR),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=4 * 1024 * 1024,  # длинные сообщения и аудио голосовых — одной строкой
        )
        stderr_task = asyncio.create_task(_drain_stderr(proc.stderr))
        if on_start:
            on_start()
        try:
            async for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                try:
                    await on_event(event)
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка обработки события WhatsApp")
        finally:
            if proc.returncode is None:
                proc.kill()
            code = await proc.wait()
            stderr_task.cancel()
        log.warning("Мост WhatsApp завершился (код %s), перезапуск через 10 с", code)
        await asyncio.sleep(10)
