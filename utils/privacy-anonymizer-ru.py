#!/usr/bin/env python3
"""
Обезличивание русскоязычных текстов.

Схема:
  1. REGEX-слой (чистый Python, детерминированно): документные ПД —
     ИНН, СНИЛС, паспорт РФ, банковская карта, телефон, email.
     Номера с контрольной суммой проверяются (ИНН/СНИЛС/карта).
  2. LLM-слой: смысловые ПД — имена людей и др.
     Модель поднимается, возвращает спаны в JSON, и сразу выгружается
     из памяти (keep_alive=0). Модель НИЧЕГО не переписывает.
  3. МЁРЖ: все спаны объединяются, пересечения разрешаются, замена на
     типизированные плейсхолдеры делается детерминированно в коде.
     Записывается новый файл-копия.

Оба слоя независимо включаются флагами USE_REGEX_LAYER / USE_LLM_LAYER.

Требования:
  - установленный Ollama (скрипт сам поднимет `ollama serve`, если он не запущен,
    и погасит его на выходе; уже запущенный чужой сервер не трогает)
  - ollama pull {MODEL}        # ~2.5 ГБ, dense, Q4_K_M
    Если модель не установлена — скрипт выдаст ошибку, автоскачивания нет.

Запуск:
  1. Создать .venv
    python -m venv .venv
    source .venv/bin/activate
  2. Установить зависимости
    pip install requests
  3. Запустить
    python ru_pii_sanitizer.py <входной_файл> [выходной_файл] # без output_file печатает результат в stdout
"""

from __future__ import annotations

import atexit
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import requests

# ============================================================================
# КОНФИГ
# ============================================================================

USE_REGEX_LAYER = True
USE_LLM_LAYER = True
LLM_DEBUG = False           # True -> печатать сырой JSON-ответ модели в stderr (диагностика)

OLLAMA_HOST = "http://localhost:11434"
OLLAMA_URL = f"{OLLAMA_HOST}/api/chat"
MODEL = "qwen3:4b"          # или "qwen3.5:4b", "gemma3:4b", ...
LLM_TIMEOUT = 600           # сек; первый запрос включает холодную загрузку весов
SERVER_START_TIMEOUT = 30   # сек ожидания старта `ollama serve`, если поднимаем сами

# Какие смысловые категории просить у LLM.
# Доступны: PERSON, LOC, ORG, ADDRESS, LOGIN (определения — в _CATEGORY_DEFS).
LLM_CATEGORIES = ["PERSON", "LOC", "ORG", "ADDRESS", "LOGIN"]

# Что делать со ссылками:
#   False (по умолчанию) — детерминированно маскировать ВСЕ ссылки как <URL>;
#   True                 — наоборот, защищать ссылки: не маскировать их и не давать другим слоям затронуть содержимое URL.
PROTECT_URLS = False

# Плейсхолдеры для замены (тип -> подстановка)
PLACEHOLDERS = {
    "PERSON": "<ИМЯ>",
    "ORG": "<ОРГАНИЗАЦИЯ>",
    "LOC": "<ЛОКАЦИЯ>",
    "ADDRESS": "<АДРЕС>",
    "LOGIN": "<ЛОГИН>",
    "INN": "<ИНН>",
    "SNILS": "<СНИЛС>",
    "PASSPORT": "<ПАСПОРТ>",
    "CARD": "<КАРТА>",
    "PHONE": "<ТЕЛЕФОН>",
    "EMAIL": "<EMAIL>",
    "URL": "<URL>",
}

# Приоритет при пересечении спанов (больше = важнее). Структурные regex-ПД
# выигрывают у смысловых LLM-догадок.
PRIORITY = {
    "URL": 3, "INN": 3, "SNILS": 3, "CARD": 3, "PASSPORT": 3, "PHONE": 3, "EMAIL": 3,
    "PERSON": 2, "ORG": 2, "LOC": 2, "ADDRESS": 2, "LOGIN": 2,
}


@dataclass
class Span:
    start: int
    end: int
    text: str
    type: str
    source: str  # "regex" | "llm"


# ============================================================================
# 1. REGEX-СЛОЙ: валидаторы контрольных сумм (чистый Python)
# ============================================================================

def _digits(text: str) -> List[int]:
    return [int(c) for c in text if c.isdigit()]


def validate_inn(text: str) -> bool:
    d = _digits(text)
    if len(d) == 10:
        coef = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        return sum(c * x for c, x in zip(coef, d[:9])) % 11 % 10 == d[9]
    if len(d) == 12:
        c1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        c2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        n11 = sum(c * x for c, x in zip(c1, d[:10])) % 11 % 10
        n12 = sum(c * x for c, x in zip(c2, d[:11])) % 11 % 10
        return n11 == d[10] and n12 == d[11]
    return False


def validate_snils(text: str) -> bool:
    d = _digits(text)
    if len(d) != 11:
        return False
    number, check = d[:9], d[9] * 10 + d[10]
    if int("".join(map(str, number))) <= 1001998:
        return True
    s = sum(number[i] * (9 - i) for i in range(9))
    control = s if s < 100 else (0 if s in (100, 101) else (s % 101))
    if control == 100:
        control = 0
    return control == check


def validate_luhn(text: str) -> bool:
    d = _digits(text)
    if not (13 <= len(d) <= 19):
        return False
    total, parity = 0, len(d) % 2
    for i, x in enumerate(d):
        if i % 2 == parity:
            x = x * 2 - 9 if x * 2 > 9 else x * 2
        total += x
    return total % 10 == 0


def _passport_ok(text: str) -> bool:
    d = _digits(text)
    if len(d) != 10 or len(set(d)) == 1:
        return False
    return 1 <= d[0] * 10 + d[1] <= 99


# (pattern, type, validator | None)
_REGEX_RULES = [
    (re.compile(r"\b\d{10}\b|\b\d{12}\b"), "INN", validate_inn),
    (re.compile(r"\b\d{3}[-\s]?\d{3}[-\s]?\d{3}[-\s]?\d{2}\b"), "SNILS", validate_snils),
    (re.compile(r"\b\d{2}\s?\d{2}\s?\d{6}\b"), "PASSPORT", _passport_ok),
    (re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b"), "CARD", validate_luhn),
    (re.compile(r"(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"), "PHONE", None),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "EMAIL", None),
]


def find_regex_pii(text: str) -> List[Span]:
    spans: List[Span] = []
    for pattern, etype, validator in _REGEX_RULES:
        for m in pattern.finditer(text):
            frag = m.group()
            if validator is not None and not validator(frag):
                continue
            spans.append(Span(m.start(), m.end(), frag, etype, "regex"))
    return spans


# ============================================================================
# 2. LLM-СЛОЙ: MODEL через Ollama
# ============================================================================

_CATEGORY_DEFS = {
    "PERSON": "имена, фамилии, отчества КОНКРЕТНЫХ людей",
    "ORG": "названия конкретных компаний/организаций",
    "LOC": "конкретные географические места (город, улица, страна)",
    "ADDRESS": "почтовые адреса, юридические адреса, фактические адреса, адреса проживания",
    "LOGIN": "логины/никнеймы/учётные записи конкретных людей",
}

# Подстраховка на случай, если бэкенд/модель проигнорируют enum из JSON Schema:
# синонимы типов -> наши коды. При работающей схеме это фактически no-op (можно удалить).
_TYPE_ALIASES = {
    "NAME": "PERSON", "PER": "PERSON", "PERSON": "PERSON", "FULLNAME": "PERSON",
    "ORG": "ORG", "ORGANIZATION": "ORG", "ORGANISATION": "ORG", "COMPANY": "ORG",
    "LOC": "LOC", "LOCATION": "LOC", "GPE": "LOC", "CITY": "LOC", "PLACE": "LOC",
    "ADDRESS": "ADDRESS", "ADDR": "ADDRESS",
    "LOGIN": "LOGIN", "USERNAME": "LOGIN", "USER": "LOGIN", "NICK": "LOGIN", "NICKNAME": "LOGIN",
}

_SYSTEM_PROMPT = (
    "Ты — система обезличивания текста. Ты находишь в тексте персональные и "
    "идентифицирующие данные ЗАПРОШЕННЫХ категорий (люди, организации, локации, "
    "адреса, логины) и возвращаешь их строго в JSON. "
    "Ты ничего не переписываешь, не переводишь и не комментируешь."
)


def _build_user_prompt(text: str) -> str:
    cats = "\n".join(
        f'  - {c}: {_CATEGORY_DEFS.get(c, c)}' for c in LLM_CATEGORIES
    )
    return f"""Найди в тексте персональные данные следующих категорий:
{cats}

НЕ считай персональными данными (это НЕ ПД, игнорируй):
  - роли и должности: продавец, консьерж, покупатель, администратор, модератор, актор;
  - обобщённые обозначения: пользователь, клиент, система, платформа, сервис;
  - названия полей форм, кнопок и элементов интерфейса (например «Фонд для пожертвования»);
  - названия функций и продуктов (например «Price Checker»);
  - заголовки, статусы, единицы измерения, числовые параметры.

Правила:
  1. Возвращай только подстроки, ДОСЛОВНО присутствующие в тексте (посимвольно,
     в том же регистре и падеже). Ничего не нормализуй.
  2. Формат ответа строго: {{"pii": [{{"text": "...", "type": "PERSON"}}, ...]}}
     Если персональных данных нет — верни {{"pii": []}}.
  3. Никакого текста вне JSON.

Пример.
Текст: «Аналитик Иванов Пётр (логин pivanov), ООО «Ромашка», г. Казань, ул. Баумана, д. 3. Продавец публикует товар на платформе.»
Ответ: {{"pii": [{{"text": "Иванов Пётр", "type": "PERSON"}}, {{"text": "pivanov", "type": "LOGIN"}}, {{"text": "ООО «Ромашка»", "type": "ORG"}}, {{"text": "г. Казань", "type": "LOC"}}, {{"text": "ул. Баумана, д. 3", "type": "ADDRESS"}}]}}
(«Продавец» и «платформа» — роль и обобщение, в ответ не попали.)

Текст для анализа:
<<<
{text}
>>>"""


def _strip_to_json(raw: str) -> str:
    """Убирает <think>...</think>, ```-ограждения и текст вокруг JSON-объекта."""
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"```(?:json)?", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    return raw[start:end + 1] if start != -1 and end != -1 else raw.strip()


def _installed_models() -> Optional[set]:
    """Множество установленных моделей (из /api/tags). None -> сервер недоступен."""
    try:
        r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=2)
        r.raise_for_status()
    except requests.exceptions.RequestException:
        return None
    return {m.get("name", "") for m in r.json().get("models", [])}


def _model_present(installed: set) -> bool:
    if MODEL in installed:
        return True
    # если тег не указан, Ollama подразумевает :latest
    return ":" not in MODEL and f"{MODEL}:latest" in installed


INSTALL_HINTS = (
    "Для слоя обезличивания через LLM установите Ollama:\n"
    "  - macOS:   brew install ollama   (или .dmg с https://ollama.com/download)\n"
    "  - Windows: winget install Ollama.Ollama   (или установщик с https://ollama.com/download)\n"
    "  - Linux:   curl -fsSL https://ollama.com/install.sh | sh"
)

_server_proc: Optional[subprocess.Popen] = None
_llm_available: Optional[bool] = None   # кэш результата preflight_llm()


def _warn(msg: str) -> None:
    sys.stderr.write(f"ВНИМАНИЕ: {msg}\n")


def _ollama_installed() -> bool:
    return shutil.which("ollama") is not None


def _stop_server() -> None:
    """Останавливает сервер, но ТОЛЬКО если его поднял этот скрипт."""
    global _server_proc
    if _server_proc is not None:
        _server_proc.terminate()
        try:
            _server_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _server_proc.kill()
        _server_proc = None


def _start_server() -> bool:
    """Поднимает `ollama serve` подпроцессом и ждёт готовности. True при успехе."""
    global _server_proc
    exe = shutil.which("ollama")
    if exe is None:
        return False
    _server_proc = subprocess.Popen(
        [exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    atexit.register(_stop_server)   # backstop: погасить свой сервер при выходе
    deadline = time.time() + SERVER_START_TIMEOUT
    while time.time() < deadline:
        if _installed_models() is not None:
            return True
        time.sleep(0.5)
    return False


def preflight_llm() -> bool:
    """Готовит LLM-слой (выполняется один раз, результат кэшируется).
    Возвращает True, если слоем можно пользоваться. При любой проблеме выводит
    ВНИМАНИЕ и возвращает False — скрипт продолжит работу только с REGEX-слоем,
    без жёсткого падения."""
    global _llm_available
    if _llm_available is not None:
        return _llm_available

    _llm_available = False
    # 1. установлена ли Ollama вообще
    if not _ollama_installed():
        _warn(INSTALL_HINTS)
        return False
    # 2. запущена ли; если нет — поднимаем сами (и запомним, что свой сервер)
    if _installed_models() is None:
        if not _start_server():
            _warn("не удалось запустить `ollama serve`; LLM-слой пропущен.")
            return False
    installed = _installed_models() or set()
    # 3. установлена ли нужная модель (автоскачивания нет — только подсказка)
    if not _model_present(installed):
        _warn(
            f"Для слоя обезличивания через LLM установите модель {MODEL}: "
            f"`ollama pull {MODEL}`"
        )
        return False

    _llm_available = True
    return True


def shutdown_ollama() -> None:
    """Гасит сервер, поднятый нами (модель уже выгружена через keep_alive=0).
    Если сервер был чужой — ничего не делает."""
    _stop_server()


def _pii_schema() -> dict:
    """JSON Schema для ответа: массив объектов {text, type}, где type — enum из
    LLM_CATEGORIES. Ollama грамматически ограничивает вывод по этой схеме, поэтому
    модель физически не может вернуть невалидный JSON или тип вне списка."""
    return {
        "type": "object",
        "properties": {
            "pii": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "type": {"type": "string", "enum": list(LLM_CATEGORIES)},
                    },
                    "required": ["text", "type"],
                },
            }
        },
        "required": ["pii"],
    }


def call_llm(text: str) -> str:
    """Один запрос к Ollama. keep_alive=0 -> модель выгружается сразу после ответа."""
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(text)},
        ],
        "stream": False,
        "format": _pii_schema(),  # JSON Schema: жёстко фиксируем структуру и enum типов
        "think": False,       # отключаем режим размышлений
        "keep_alive": 0,      # <-- выгрузить модель из памяти после ответа
        "options": {"temperature": 0},
    }
    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=LLM_TIMEOUT)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        _warn(f"запрос к Ollama не удался ({e}); LLM-слой для этого текста пропущен.")
        return ""
    return resp.json().get("message", {}).get("content", "")


def _occurrences(frag: str, text: str):
    """Все дословные вхождения frag в text. Если фрагмент начинается/кончается
    словесным символом — обрамляем \\b, чтобы «Иван» не совпал внутри «Иванов»
    (границы слова в Python re по умолчанию учитывают кириллицу)."""
    pattern = re.escape(frag)
    if frag[:1].isalnum() or frag[:1] == "_":
        pattern = r"\b" + pattern
    if frag[-1:].isalnum() or frag[-1:] == "_":
        pattern = pattern + r"\b"
    return re.finditer(pattern, text)


def find_llm_pii(text: str) -> List[Span]:
    """Запрашивает LLM, валидирует и превращает найденные строки в спаны.
    Анти-галлюцинация: принимаются только строки, ДОСЛОВНО встречающиеся
    в исходном тексте; каждое вхождение маскируется отдельно."""
    raw = call_llm(text)
    if LLM_DEBUG:
        sys.stderr.write(f"[LLM RAW] {raw}\n")
    if not raw.strip():
        return []
    try:
        data = json.loads(_strip_to_json(raw))
    except json.JSONDecodeError:
        _warn("LLM вернула невалидный JSON; LLM-слой для этого текста пропущен.")
        return []

    items = data.get("pii", data) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []

    allowed = set(LLM_CATEGORIES)
    spans: List[Span] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        frag = (item.get("text") or "").strip()
        etype = (item.get("type") or "").strip().upper()
        etype = _TYPE_ALIASES.get(etype, etype)
        if not frag or etype not in allowed:
            continue
        # найти все дословные вхождения (с границами слова)
        for m in _occurrences(frag, text):
            spans.append(Span(m.start(), m.end(), frag, etype, "llm"))
    return spans


# ============================================================================
# 3. МЁРЖ И ЗАМЕНА
# ============================================================================

def merge_spans(spans: List[Span]) -> List[Span]:
    """Выбирает непересекающиеся спаны, отдавая приоритет более важным (PRIORITY),
    затем более длинным. Порядок в тексте на ВЫБОР не влияет — иначе LLM-спан,
    начавшийся чуть раньше, мог перебить структурный regex-ПД (ИНН/СНИЛС/…)."""
    ordered = sorted(
        spans,
        key=lambda s: (-PRIORITY.get(s.type, 1), -(s.end - s.start), s.start),
    )
    kept: List[Span] = []
    for s in ordered:
        if any(s.start < k.end and s.end > k.start for k in kept):
            continue  # пересекается с уже принятым (более приоритетным) — пропускаем
        kept.append(s)
    kept.sort(key=lambda s: s.start)
    return kept


def apply_spans(text: str, spans: List[Span]) -> str:
    """Заменяет спаны на плейсхолдеры, идя с конца, чтобы не сбить смещения."""
    for s in sorted(spans, key=lambda s: s.start, reverse=True):
        placeholder = PLACEHOLDERS.get(s.type, "<ПД>")
        text = text[:s.start] + placeholder + text[s.end:]
    return text


# http(s)/ftp/www-ссылки. Класс символов исключает пробелы и закрывающие
# скобки/кавычки, чтобы в markdown вида [текст](url) не съесть завершающую ')'.
_URL_RE = re.compile(r"(?:https?|ftp)://[^\s<>\"'\]})]+|www\.[^\s<>\"'\]})]+")


def find_url_spans(text: str) -> List[Span]:
    """Все ссылки в тексте как спаны типа URL (для сплошного маскирования).
    Хвостовая пунктуация (. , ; : ! ? ») в спан не включается."""
    spans: List[Span] = []
    for m in _URL_RE.finditer(text):
        s, e = m.start(), m.end()
        while e > s and text[e - 1] in ".,;:!?»":
            e -= 1
        spans.append(Span(s, e, text[s:e], "URL", "regex"))
    return spans


def _drop_spans_in_urls(text: str, spans: List[Span]) -> List[Span]:
    """Выбрасывает ПД-спаны, пересекающиеся с URL, чтобы не ломать ссылки."""
    urls = [(m.start(), m.end()) for m in _URL_RE.finditer(text)]
    if not urls:
        return spans
    return [
        s for s in spans
        if not any(s.start < ue and s.end > us for us, ue in urls)
    ]


def sanitize_text(text: str) -> str:
    spans: List[Span] = []
    if USE_REGEX_LAYER:
        spans += find_regex_pii(text)
    if USE_LLM_LAYER and preflight_llm():
        spans += find_llm_pii(text)
    if PROTECT_URLS:
        spans = _drop_spans_in_urls(text, spans)   # защищаем ссылки: не маскируем
    else:
        spans += find_url_spans(text)               # наоборот: маскируем все ссылки как <URL>
    return apply_spans(text, merge_spans(spans))


# ============================================================================
# 4. Файлы + кэш + CLI
# ============================================================================

def sanitize_file_cached(in_path: str, cache_dir: str = ".pii_cache") -> str:
    """Возвращает путь к обезличенной копии. Кэш по SHA-256 содержимого:
    если файл не менялся, LLM повторно не запускается. Хук должен читать
    именно этот путь, а не оригинал."""
    src = Path(in_path)
    raw = src.read_bytes()
    h = hashlib.sha256(raw).hexdigest()[:16]
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    dst = cache / f"{src.stem}.{h}.sanitized{src.suffix or '.txt'}"
    if not dst.exists():
        dst.write_text(sanitize_text(raw.decode("utf-8", errors="replace")),
                       encoding="utf-8")
    return str(dst)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python ru_pii_sanitizer.py <input> [output]", file=sys.stderr)
        sys.exit(2)

    # Предполётные проверки LLM-слоя — в начале, чтобы предупреждения были видны сразу
    if USE_LLM_LAYER:
        preflight_llm()

    try:
        text_in = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
        clean = sanitize_text(text_in)

        if len(sys.argv) > 2:
            Path(sys.argv[2]).write_text(clean, encoding="utf-8")
            print(f"Готово: {sys.argv[2]}", file=sys.stderr)
        else:
            sys.stdout.write(clean)
    finally:
        shutdown_ollama()   # гасим сервер, если поднимали сами (модель уже выгружена)