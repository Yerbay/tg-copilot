"""Единый интерфейс к LLM. Провайдер выбирается в .env (LLM_PROVIDER).

Gemini, DeepSeek, OpenAI и Ollama работают через OpenAI-совместимый API,
Claude — через SDK Anthropic. Чтобы добавить провайдера, допишите строку в PROVIDERS.
"""
import base64
import json
from typing import Awaitable, Callable

import config

# провайдер -> (тип клиента, base_url по умолчанию)
PROVIDERS: dict[str, tuple[str, str | None]] = {
    "gemini": ("openai", "https://generativelanguage.googleapis.com/v1beta/openai/"),
    "deepseek": ("openai", "https://api.deepseek.com"),
    "openai": ("openai", None),
    "ollama": ("openai", "http://localhost:11434/v1"),
    "claude": ("anthropic", None),
}

# Запас токенов на «размышления»: думающие модели (DeepSeek, Gemini 2.5) тратят их до ответа,
# и без запаса длинный ответ обрезается. Платится только то, что реально израсходовано.
REASONING_HEADROOM = 4000

_client = None


class LimitReached(Exception):
    """Дневной лимит запросов пользователя исчерпан."""


# Вызывается перед каждым платным запросом (ИИ и распознавание речи): учёт расхода и лимиты.
# main.py подставляет свою функцию; она бросает LimitReached, если лимит исчерпан.
before_call: Callable[[], None] | None = None


def count_call() -> None:
    if before_call:
        before_call()


def _get_client():
    global _client
    if _client is not None:
        return _client
    if config.LLM_PROVIDER not in PROVIDERS:
        raise ValueError(
            f"Неизвестный LLM_PROVIDER '{config.LLM_PROVIDER}'. "
            f"Доступны: {', '.join(PROVIDERS)}"
        )
    kind, default_url = PROVIDERS[config.LLM_PROVIDER]
    base_url = config.LLM_BASE_URL or default_url
    if kind == "openai":
        from openai import AsyncOpenAI

        _client = AsyncOpenAI(api_key=config.LLM_API_KEY or "ollama", base_url=base_url)
    else:
        from anthropic import AsyncAnthropic

        _client = AsyncAnthropic(api_key=config.LLM_API_KEY, base_url=base_url)
    return _client


async def chat(system: str, messages: list[dict], max_tokens: int = 1500, smart: bool = False) -> str:
    """Диалог: messages = [{"role": "user"|"assistant", "content": "..."}, ...].
    smart=True — разборы и анализ (LLM_MODEL_SMART), иначе быстрые подсказки (LLM_MODEL)."""
    count_call()
    client = _get_client()
    kind = PROVIDERS[config.LLM_PROVIDER][0]
    model = config.LLM_MODEL_SMART if smart else config.LLM_MODEL

    if kind == "openai":
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, *messages],
            max_tokens=max_tokens + REASONING_HEADROOM,
            temperature=0.8,
        )
        return (resp.choices[0].message.content or "").strip()

    resp = await client.messages.create(
        model=model,
        system=system,
        messages=messages,
        max_tokens=max_tokens,
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


async def complete(system: str, user: str, max_tokens: int = 1500, smart: bool = False) -> str:
    """Один запрос — один ответ."""
    return await chat(system, [{"role": "user", "content": user}], max_tokens, smart)


async def vision(prompt: str, images: list[bytes], max_tokens: int = 4000) -> str:
    """Запрос с картинками (PNG/JPEG) к быстрой модели — она должна уметь читать изображения."""
    count_call()
    client = _get_client()
    kind = PROVIDERS[config.LLM_PROVIDER][0]
    if kind == "openai":
        content = [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": f"data:{_mime(img)};base64,{base64.b64encode(img).decode()}"}}
            for img in images
        ]
        resp = await client.chat.completions.create(
            model=config.LLM_MODEL, messages=[{"role": "user", "content": content}],
            max_tokens=max_tokens + REASONING_HEADROOM, temperature=0.1,
        )
        return (resp.choices[0].message.content or "").strip()
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": _mime(img), "data": base64.b64encode(img).decode()}}
        for img in images
    ] + [{"type": "text", "text": prompt}]
    resp = await client.messages.create(
        model=config.LLM_MODEL, messages=[{"role": "user", "content": content}], max_tokens=max_tokens,
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def _mime(img: bytes) -> str:
    return "image/png" if img[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"


# ---------------------------------------------------------------- агент с инструментами

ToolHandler = Callable[[str, dict], Awaitable[str]]


async def run_tools(system: str, messages: list[dict], tools: list[dict], call_tool: ToolHandler,
                    max_steps: int = 6, max_tokens: int = 1500) -> str:
    """Модель сама вызывает инструменты, пока не ответит текстом. Быстрая модель (LLM_MODEL).

    tools — [{"name", "description", "parameters": JSON Schema}], call_tool(name, args) -> текст результата.
    """
    client = _get_client()
    kind = PROVIDERS[config.LLM_PROVIDER][0]

    if kind == "openai":
        spec = [{"type": "function", "function": t} for t in tools]
        msgs = [{"role": "system", "content": system}, *messages]
        for _ in range(max_steps):
            count_call()
            resp = await client.chat.completions.create(
                model=config.LLM_MODEL, messages=msgs, tools=spec,
                max_tokens=max_tokens + REASONING_HEADROOM, temperature=0.7,
            )
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return (msg.content or "").strip()
            msgs.append(msg.model_dump(exclude_none=True))  # вместе с reasoning_content — так просит DeepSeek
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except ValueError:
                    args = {}
                msgs.append({"role": "tool", "tool_call_id": tc.id, "content": await call_tool(tc.function.name, args)})
    else:
        spec = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]
        msgs = list(messages)
        for _ in range(max_steps):
            count_call()
            resp = await client.messages.create(
                model=config.LLM_MODEL, system=system, messages=msgs, tools=spec, max_tokens=max_tokens,
            )
            if resp.stop_reason != "tool_use":
                return "".join(b.text for b in resp.content if b.type == "text").strip()
            msgs.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": await call_tool(b.name, b.input)}
                for b in resp.content if b.type == "tool_use"
            ]})
    return "Не успел закончить — сформулируйте запрос чуть проще."
