"""Настройки из файла .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


# --- Telegram ---
API_ID = _int("TG_API_ID", 0)
API_HASH = os.getenv("TG_API_HASH", "").strip()
SESSIONS_DIR = BASE_DIR / "sessions"  # sessions/<id пользователя>/tg.session — чтение его Telegram


def tg_session_path(user_id: int) -> str:
    return str(SESSIONS_DIR / str(user_id) / "tg")


# --- Бот и пользователи ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()        # от @BotFather
BOT_SESSION_NAME = str(BASE_DIR / "copilot_bot")
ADMIN_ID = _int("ADMIN_ID", 0) or _int("OWNER_ID", 0)  # Telegram ID администратора (раньше — OWNER_ID)
MAX_USERS = _int("MAX_USERS", 10)                      # сколько человек, включая администратора
USER_DAILY_LIMIT = _int("USER_DAILY_LIMIT", 150)       # запросов к ИИ в день на пользователя (админу без лимита)

# --- WhatsApp (мост на Node.js в папке wa/) ---
WA_ENABLED = os.getenv("WA_ENABLED", "true").strip().lower() in ("1", "true", "yes")
WA_SESSION_DIR = str(BASE_DIR / os.getenv("WA_SESSION_DIR", "wa-session"))  # внутри — папка на пользователя
NODE_BIN = os.getenv("NODE_BIN", "node").strip()

# --- LLM ---
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "deepseek").strip().lower()
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-flash").strip()  # быстрые подсказки в переписке
LLM_MODEL_SMART = os.getenv("LLM_MODEL_SMART", "").strip() or LLM_MODEL  # разборы, консультации, вечерний отчёт
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip() or None  # переопределить эндпоинт при необходимости

# --- Распознавание речи (Gemini) ---
STT_API_KEY = os.getenv("STT_API_KEY", "").strip()
# Несколько моделей через запятую: если первая недоступна или перегружена — берётся следующая
STT_MODELS = [m.strip() for m in os.getenv(
    "STT_MODEL", "gemini-3.5-flash-lite,gemini-flash-lite-latest,gemini-3-flash-preview").split(",") if m.strip()]

# --- Поведение ---
DB_PATH = str(BASE_DIR / os.getenv("DB_PATH", "copilot.db"))
TIMEZONE = os.getenv("TIMEZONE", "Asia/Almaty")
DAILY_HOUR = _int("DAILY_HOUR", 21)             # час вечернего разбора
DEBOUNCE_SECONDS = _int("DEBOUNCE_SECONDS", 15)  # тишина после её сообщения (или «печатает»), потом подсказка
DEBOUNCE_MAX_SECONDS = _int("DEBOUNCE_MAX_SECONDS", 120)  # но не дольше стольких секунд от первого сообщения серии
HISTORY_LIMIT = _int("HISTORY_LIMIT", 30)        # сколько последних сообщений давать модели
NUM_VARIANTS = _int("NUM_VARIANTS", 3)
WAITING_HOURS = _int("WAITING_HOURS", 20)        # «она ждёт ответа» после стольких часов
SILENCE_DAYS = _int("SILENCE_DAYS", 3)           # «давно не общались» после стольких дней


def check() -> list[str]:
    """Возвращает список незаполненных обязательных настроек."""
    missing = []
    if not API_ID:
        missing.append("TG_API_ID")
    if not API_HASH:
        missing.append("TG_API_HASH")
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not ADMIN_ID:
        missing.append("ADMIN_ID")
    if LLM_PROVIDER != "ollama" and not LLM_API_KEY:
        missing.append("LLM_API_KEY")
    return missing
