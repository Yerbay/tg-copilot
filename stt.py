"""Gemini: распознавание речи (голосовые из переписок и голосовые боту) и запасное чтение скриншотов.

Расшифровки голосовых кэшируются в базе по ключу — одно голосовое не распознаём дважды.
"""
import base64
import logging

import httpx

import config
import db
import llm

log = logging.getLogger("copilot.stt")

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
PROMPT = ("Расшифруй голосовое сообщение дословно, с пунктуацией, на языке оригинала. "
          "Верни только текст. Если речи нет — верни пустую строку.")
MAX_BYTES = 15 * 1024 * 1024  # ~ полчаса голосового; длиннее не шлём
# модель снята (404), лимит (429), перегрузка (500/503) — пробуем следующую из STT_MODEL
RETRY_NEXT = {404, 429, 500, 502, 503, 504}


def enabled() -> bool:
    return bool(config.STT_API_KEY)


async def _generate(parts: list[dict], what: str) -> str | None:
    """Запрос к Gemini с перебором моделей из STT_MODEL. None — ни одна не ответила."""
    body = {"contents": [{"parts": parts}]}
    async with httpx.AsyncClient(timeout=120) as client:
        for model in config.STT_MODELS:  # первая недоступная или перегруженная — пробуем следующую
            try:
                # ключ — в заголовке, не в адресе: адреса запросов попадают в логи
                resp = await client.post(URL.format(model=model), headers={"x-goog-api-key": config.STT_API_KEY},
                                         json=body)
                resp.raise_for_status()
                candidate = resp.json()["candidates"][0]
                text = " ".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", [])).strip()
                if not text and candidate.get("finishReason") not in (None, "STOP"):
                    log.warning("%s: модель %s не ответила (%s)", what, model, candidate.get("finishReason"))
                    continue
                return text
            except httpx.HTTPStatusError as e:
                log.warning("%s: модель %s ответила %s", what, model, e.response.status_code)
                if e.response.status_code not in RETRY_NEXT:
                    return None
            except Exception as e:  # noqa: BLE001 — без Gemini всё продолжает работать
                log.warning("%s: модель %s — %s", what, model, e)
    return None


async def transcribe(audio: bytes, mime: str = "audio/ogg", cache_key: str | None = None) -> str:
    """Текст голосового. Пустая строка — если не удалось или распознавание не настроено."""
    if cache_key and (cached := db.get_transcript(cache_key)) is not None:
        return cached
    if not enabled() or not audio or len(audio) > MAX_BYTES:
        return ""
    llm.count_call()  # учёт расхода и дневной лимит — как у запросов к ИИ
    text = await _generate([{"text": PROMPT},
                            {"inline_data": {"mime_type": mime, "data": base64.b64encode(audio).decode()}}],
                           "Распознавание речи")
    if text is None:
        log.warning("Не удалось распознать голосовое ни одной моделью")
        return ""
    if cache_key:
        db.save_transcript(cache_key, text)
    return text


async def read_images(prompt: str, images: list[bytes]) -> str | None:
    """Запасное чтение скриншотов, если основная модель не справилась."""
    if not enabled():
        return None
    llm.count_call()
    parts = [{"text": prompt}] + [
        {"inline_data": {"mime_type": llm._mime(img), "data": base64.b64encode(img).decode()}} for img in images
    ]
    return await _generate(parts, "Чтение скриншота (Gemini)")
