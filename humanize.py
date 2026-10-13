"""«Человеческая» грамотность предлагаемых сообщений: безупречный текст в переписке выдаёт ИИ.

Стиль (сокращения, манеру) задаёт промпт (prompts.LITERACY), а ошибки добавляем здесь, кодом:
модель плохо держит «70% грамотности», а вероятности — держат.
"""
import random
import re

LEVELS = ("100", "95", "85", "70", "me")
LABELS = {"100": "100% — безупречно", "95": "95% — как в обычной переписке", "85": "85% — небрежно",
          "70": "70% — совсем просто", "me": "как пишу я"}


class _Level:
    def __init__(self, lower_first: float, drop_commas: float, drop_inner_dots: float, typos: float):
        self.lower_first = lower_first          # первая буква сообщения строчная
        self.drop_commas = drop_commas          # доля пропущенных запятых
        self.drop_inner_dots = drop_inner_dots  # точка между предложениями -> пробел/перенос
        self.typos = typos                      # вероятность опечатки в слове (не короче 4 букв)


_LEVELS = {
    "95": _Level(lower_first=0.5, drop_commas=0.15, drop_inner_dots=0.0, typos=0.0),
    "85": _Level(lower_first=0.8, drop_commas=0.5, drop_inner_dots=0.3, typos=0.025),
    "70": _Level(lower_first=1.0, drop_commas=0.85, drop_inner_dots=0.7, typos=0.06),
}

# соседние клавиши ЙЦУКЕН — опечатка «промахнулся пальцем»
_ROWS = ["йцукенгшщзхъ", "фывапролджэ", "ячсмитьбю"]
_NEIGHBORS: dict[str, str] = {}
for _r, _row in enumerate(_ROWS):
    for _i, _ch in enumerate(_row):
        near = _row[max(0, _i - 1) : _i] + _row[_i + 1 : _i + 2]
        _NEIGHBORS[_ch] = near
_WORD = re.compile(r"[а-яё]{4,}", re.IGNORECASE)


def _typo(word: str, rnd: random.Random) -> str:
    """Одна опечатка: соседняя клавиша, пропуск, удвоение или перестановка. Первую букву не трогаем."""
    i = rnd.randrange(1, len(word))
    kind = rnd.choice(("near", "near", "skip", "double", "swap"))
    ch = word[i]
    if kind == "near" and (near := _NEIGHBORS.get(ch.lower())):
        new = rnd.choice(near)
        return word[:i] + (new.upper() if ch.isupper() else new) + word[i + 1 :]
    if kind == "skip" and len(word) > 4:
        return word[:i] + word[i + 1 :]
    if kind == "double":
        return word[:i] + ch + word[i:]
    if kind == "swap" and i < len(word) - 1:
        return word[:i] + word[i + 1] + ch + word[i + 2 :]
    return word


def humanize(text: str, level: str, rnd: random.Random | None = None) -> str:
    """Текст одного сообщения с ошибками заданного уровня. 100 и «как я» — без изменений."""
    lv = _LEVELS.get(level)
    if lv is None or not text:
        return text
    rnd = rnd or random.Random()
    text = text.replace("ё", "е").replace("Ё", "Е")
    text = re.sub(r"\.(?=\s*$)", "", text)  # точку в конце сообщения в переписке не ставят
    text = re.sub(r",(?=\s)", lambda m: "" if rnd.random() < lv.drop_commas else ",", text)
    if lv.drop_inner_dots:
        text = re.sub(r"\.\s+(\w)", lambda m: m.group(0) if rnd.random() >= lv.drop_inner_dots
                      else " " + m.group(1).lower(), text)  # без точки и заглавная не нужна
    if lv.typos:
        text = _WORD.sub(lambda m: _typo(m.group(0), rnd) if rnd.random() < lv.typos else m.group(0), text)
    # строчная буква в начале сообщения (и после точки — на «небрежных» уровнях)
    starts = r"^(\W*)(\w)" if lv.drop_inner_dots == 0 else r"(^\W*|[.!?…]\s+)(\w)"
    text = re.sub(starts, lambda m: m.group(1) + (m.group(2).lower() if rnd.random() < lv.lower_first else m.group(2)),
                  text, flags=re.MULTILINE)
    return text
