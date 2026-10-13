"""Вход в Telegram по QR — если код входа не приходит.

Запуск: python login_qr.py [id пользователя бота]   (по умолчанию — администратор)
Сканируете QR в Telegram на телефоне (Настройки → Устройства → Подключить устройство),
создаётся sessions/<id>/tg.session, и дальше main.py входит без вопросов.
Обычно не нужен: пользователи подключают Telegram прямо в боте («подключи телеграм»).
"""
import asyncio
import getpass
import sys
from pathlib import Path

import qrcode
from telethon import TelegramClient, functions
from telethon.errors import SessionPasswordNeededError

import config

HINT = ("Telegram на телефоне → Настройки → Устройства → Подключить устройство → наведите камеру на код.\n"
        "Код обновляется каждые 30 секунд. Ctrl+C — отмена.\n")


def show(url: str) -> None:
    print("\033[2J\033[H", end="")  # очистить экран, чтобы коды не копились
    print(HINT)
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.print_ascii(invert=True)


async def main() -> None:
    # чей вход: python login_qr.py [id пользователя бота]; по умолчанию — администратор
    user_id = int(sys.argv[1]) if len(sys.argv) > 1 else config.ADMIN_ID
    path = config.tg_session_path(user_id)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(path, config.API_ID, config.API_HASH)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            login = await client.qr_login()
            while True:
                show(login.url)
                try:
                    await login.wait(timeout=30)
                    break
                except TimeoutError:
                    await login.recreate()
                except SessionPasswordNeededError:
                    await client.sign_in(password=getpass.getpass("Облачный пароль Telegram (не отображается): "))
                    break
        me = await client.get_me()
        print(f"\n✅ Вход выполнен: {me.first_name} (ID {me.id}). Сессия сохранена.")
        print("Проверяю 2 минуты, что Telegram не отзовёт сессию (не закрывайте окно)…")
        for second in range(0, 121, 10):
            try:
                await client(functions.updates.GetStateRequest())
            except Exception as e:  # noqa: BLE001
                print(f"❌ Через {second} с Telegram отозвал сессию: {type(e).__name__}")
                return
            await asyncio.sleep(10)
        print("✅ Сессия держится 2 минуты — можно переносить на сервер.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
