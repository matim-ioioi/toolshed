#!/usr/bin/env python3
"""
SLLM / модель "УК-ассистент".

Определяет, отвечает ли управляющая компания за проблему в многоквартирном
доме, и готовит заявление, если отвечает.

Флоу:
  1. Поднимает ollama, если не поднята (и тушит её в конце, если поднимал сам).
  2. Фаза «корпус»: N параллельных агентов ищут нормы в локальном собрании
     НПА. Веб-инструментов у них нет, в интернет уйти неоткуда.
  3. Верификация: цитата каждой нормы ищется в тексте акта дословно.
     Не нашлась — норма отбрасывается скриптом, а не совестью модели.
  4. Фаза «веб» — только для агентов, оставшихся без подтверждённых норм.
     Здесь поднимается SearXNG в докере; если всем хватило корпуса,
     докер не трогается вовсе. Цитаты из веба проверяются так же:
     скрипт сам перезагружает страницу и ищет в ней цитату.
  5. Консенсус по вердикту и нормам, затем финальный ответ модели строго
     из согласованных норм. keep_alive=0 — модель выгружается сразу.

Никаких аккаунтов и ключей: корпус локальный, поиск — через свой SearXNG.

Зависимости: pip install requests trafilatura rank_bm25
Требуется: ollama + модель; docker (OrbStack/Docker Desktop/colima) — только
           если понадобится веб-фаза.

Запуск:
  python3 corpus_update.py                       # один раз, собрать корпус
  python3 uk-ru.py "Входная дверь в подъезд не закрывается..."
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

import corpus
import websearch
from runtime import SearxRuntime

# ----------------------------------------------------------------------------
# Конфигурация (переопределяется переменными окружения)
# ----------------------------------------------------------------------------
MODEL = os.environ.get("SLLM_MODEL", "qwen3.5:9b")
OLLAMA_HOST = os.environ.get("SLLM_OLLAMA_HOST", "http://127.0.0.1:11434")
CHAT_PATH = os.environ.get("SLLM_CHAT_PATH", "/api/chat")
AGENTS = int(os.environ.get("SLLM_AGENTS", "3"))
# По умолчанию — большинство от числа агентов: фиксированный порог становится
# недостижимым, стоит уменьшить AGENTS. Явно заданное значение берётся как есть.
MIN_CONSENSUS = int(os.environ.get("SLLM_MIN_CONSENSUS", str(AGENTS // 2 + 1)))
USE_WEB = os.environ.get("SLLM_WEB", "1") not in ("0", "false", "no")
# Отладочный режим: пропустить корпус и сразу пойти в веб. Нужен, чтобы
# проверить подъём докера, не подбирая вопрос, которого нет в корпусе.
FORCE_WEB = os.environ.get("SLLM_FORCE_WEB", "0") not in ("0", "false", "no")

# Тонкие настройки
NUM_CTX = int(os.environ.get("SLLM_NUM_CTX", "16384"))
# У thinking-моделей без явного think=false ответ уходит в поле thinking, а
# content остаётся пустым — structured output в этом случае не распарсить.
THINK = os.environ.get("SLLM_THINK", "0") not in ("0", "false", "no")
DEBUG = os.environ.get("SLLM_DEBUG", "0") not in ("0", "false", "no")
MAX_TOOL_ROUNDS = 8           # максимум циклов "модель -> инструмент" на агента
CHAT_TIMEOUT = 900            # сек на один запрос к модели
OLLAMA_START_TIMEOUT = 30     # сек ожидания поднятия ollama serve
# Тела найденных фрагментов копятся в истории и съедают контекст, из-за чего
# финальный JSON обрывается на середине. Целиком держим только последние.
HISTORY_KEEP_FULL = 2
HISTORY_TRIM = 400

# ----------------------------------------------------------------------------
# Промпты
# ----------------------------------------------------------------------------
BASE_ROLE = """Ты — юридический ассистент по вопросам управления многоквартирными домами в РФ.

Пользователь описывает проблемную ситуацию в многоквартирном доме. Твоя задача — определить ровно одно из трёх:
- uk_obligated — управляющая компания (УК) ОБЯЗАНА устранить проблему сама;
- via_uk — за устранение отвечает не УК, но жилец вправе решить вопрос ЧЕРЕЗ УК (УК обязана передать обращение ответственному и контролировать устранение);
- not_uk — это не зона ответственности УК и через УК не решается.

Типовые зоны ответственности для ориентира (каждую всё равно подтверждай нормой): УК — содержание общего имущества; РСО — качество коммунальных ресурсов до границы; региональный фонд — капитальный ремонт; собственник — имущество внутри квартиры; муниципалитет/полиция/Роспотребнадзор — вопросы вне договора управления.

Не запрашивай и не используй персональные данные пользователя."""

CORPUS_SYSTEM_PROMPT = BASE_ROLE + """

В твоём распоряжении локальное собрание нормативных актов. Ищи нормы ТОЛЬКО в нём, через инструменты corpus_search и corpus_get.

Жёсткие правила:
1. ЗАПРЕЩЕНО ссылаться на законы и пункты по памяти. Каждая норма должна быть найдена в корпусе.
2. Цитата обязана быть скопирована из найденного фрагмента ДОСЛОВНО, символ в символ. Пересказ своими словами не принимается: скрипт проверяет цитату поиском по тексту акта и молча отбрасывает всё, что не совпало.
3. Цитата — одно ключевое предложение, не длиннее 300 символов.
4. Если подходящей нормы в корпусе нет — верни пустой legal_refs. Это нормальный результат, выдумывать норму нельзя.
5. В corpus_search бери конкретные слова из описания проблемы — названия деталей, устройств, конструкций («доводчик», «мусоропровод», «отмостка»), а не только общие юридические обороты. Поиск идёт по тексту актов, и деталь из вопроса пользователя находит норму точнее, чем формулировка «содержание общего имущества».
6. Не останавливайся на первом же найденном пункте: если он общий, поищи более конкретный. Нормальная цепочка — 2–3 поиска разными словами."""

WEB_SYSTEM_PROMPT = BASE_ROLE + """

В корпусе подходящих норм не нашлось, поэтому ищи в интернете через web_search и web_fetch.

Жёсткие правила:
1. ЗАПРЕЩЕНО ссылаться на законы и пункты по памяти. Норму нужно найти через web_search, открыть страницу через web_fetch и убедиться, что текст нормы там есть.
2. Приоритет источников: pravo.gov.ru, publication.pravo.gov.ru, затем consultant.ru, garant.ru, dom.gosuslugi.ru. Блоги, форумы и Q&A-сайты источниками не считаются.
2.1. Открывай ссылки на pravo.gov.ru: справочники вроде consultant.ru часто отдают только оглавление без текста нормы, и процитировать оттуда нечего. Если в выдаче есть адрес вида pravo.gov.ru/proxy/ips/?nd=..., открывай именно его.
3. Цитата — дословно со страницы, которую ты открыл, не длиннее 300 символов. В поле url указывай адрес именно той страницы, откуда скопирована цитата. НЕ подменяй его на более авторитетный сайт: скрипт ищет цитату по тексту реально открытых страниц, и подмена ссылки норму не улучшит.
4. Если подтвердить норму не удалось — верни пустой legal_refs."""

FINAL_JSON_INSTRUCTION = """Заверши исследование. Выведи только JSON по схеме, без пояснений:
- verdict: одно из "uk_obligated", "via_uk", "not_uk";
- legal_refs: массив норм (не более 4), каждая из которых реально найдена: {"act": наименование акта, "clause": статья/пункт, "quote": дословная цитата не длиннее 300 символов, "url": адрес источника или идентификатор акта из корпуса};
- summary: краткое обоснование вердикта (2–4 предложения)."""

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["uk_obligated", "via_uk", "not_uk"]},
        "legal_refs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "act": {"type": "string"},
                    "clause": {"type": "string"},
                    "quote": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": ["act", "clause", "quote", "url"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["verdict", "legal_refs", "summary"],
}

FINAL_SYSTEM_PROMPT = """Ты — юридический ассистент по вопросам управления многоквартирными домами в РФ.
Тебе передан согласованный вердикт и подтверждённые нормы (акт, пункт, дословная цитата, источник). Сформируй окончательный ответ пользователю на русском языке.

Правила:
1. Используй ТОЛЬКО переданные нормы. Не добавляй ни одной новой ссылки на законы. Цитаты приводи дословно, как переданы.
1.1. Начни ответ строкой «Вердикт: » и дословно переданной формулировкой вердикта. Не заменяй её своей: «обязана устранить» и «решается через УК» — это разные выводы.
1.2. Не называй конкретные сроки, суммы, периодичность и прочие числа, если их нет в переданных цитатах. Вместо выдуманного срока пиши «в срок, установленный договором управления».
2. Если вердикт uk_obligated или via_uk — включи в ответ:
   а) краткое объяснение, почему вопрос решается через УК;
   б) готовый шаблон заявления в УК с плейсхолдерами: {НАИМЕНОВАНИЕ_УК}, {АДРЕС_УК}, {ФИО}, {АДРЕС_ПОМЕЩЕНИЯ}, {ТЕЛЕФОН}, {ДАТА}, {ПОДПИСЬ}. В тексте заявления сошлись на переданные нормы и потребуй устранения с указанием срока и письменного ответа;
   в) дальнейшие шаги: какие доказательства собрать (фото/видео с датой и т.п.), как подать (2 экземпляра с отметкой о приёме / ГИС ЖКХ / заказное письмо с уведомлением), что делать при отказе или молчании (жалоба в ГЖИ, далее прокуратура).
3. Если вердикт not_uk — прямо скажи, что это не вопрос УК, и укажи, кто отвечает, исходя из переданных норм и обоснования.
4. Никаких выдуманных фактов. Пиши компактно и по делу."""

VERDICT_RU = {
    "uk_obligated": "УК обязана устранить проблему",
    "via_uk": "Решается через УК (УК — посредник и контролёр)",
    "not_uk": "Не зона ответственности УК",
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------
# Ollama
# ----------------------------------------------------------------------------
def ollama_is_up() -> bool:
    try:
        return requests.get(f"{OLLAMA_HOST}/api/version", timeout=2).ok
    except requests.RequestException:
        return False


def ensure_ollama():
    """Возвращает Popen, если ollama подняли мы, иначе None."""
    if ollama_is_up():
        return None
    log(f"[sllm] ollama не поднята, запускаю на {OLLAMA_HOST}...")
    env = {
        **os.environ,
        "OLLAMA_HOST": urlparse(OLLAMA_HOST).netloc,
        "OLLAMA_NUM_PARALLEL": str(AGENTS),
    }
    try:
        proc = subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        )
    except FileNotFoundError:
        sys.exit("Не найден исполняемый файл `ollama`. Установите Ollama: https://ollama.com/download")
    deadline = time.time() + OLLAMA_START_TIMEOUT
    while time.time() < deadline:
        if ollama_is_up():
            return proc
        if proc.poll() is not None:
            sys.exit("Процесс `ollama serve` завершился с ошибкой сразу после запуска.")
        time.sleep(0.5)
    proc.terminate()
    sys.exit(f"ollama не поднялась за {OLLAMA_START_TIMEOUT} с на {OLLAMA_HOST}.")


def model_installed() -> bool:
    r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=10)
    r.raise_for_status()
    names = {m.get("name", "") for m in r.json().get("models", [])}
    return MODEL in names or (":" not in MODEL and f"{MODEL}:latest" in names)


_think_param_supported = True  # сбрасывается, если модель не принимает параметр think


def chat(messages, tools=None, fmt=None, keep_alive=None):
    global _think_param_supported
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0, "num_ctx": NUM_CTX},
    }
    if tools:
        payload["tools"] = tools
    if fmt is not None:
        payload["format"] = fmt
    if keep_alive is not None:
        payload["keep_alive"] = keep_alive
    if _think_param_supported and not THINK:
        payload["think"] = False
    r = requests.post(f"{OLLAMA_HOST}{CHAT_PATH}", json=payload, timeout=CHAT_TIMEOUT)
    if r.status_code == 400 and "think" in payload and "think" in r.text.lower():
        # Модель не поддерживает переключение режима размышлений — повторяем без него.
        _think_param_supported = False
        payload.pop("think")
        r = requests.post(f"{OLLAMA_HOST}{CHAT_PATH}", json=payload, timeout=CHAT_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if DEBUG:
        log(f"[chat] контекст {NUM_CTX}: промпт {data.get('prompt_eval_count')} "
            f"токенов, ответ {data.get('eval_count')}")
    return data["message"]


def unload_model() -> None:
    try:
        requests.post(f"{OLLAMA_HOST}{CHAT_PATH}",
                      json={"model": MODEL, "messages": [], "keep_alive": 0}, timeout=30)
    except requests.RequestException:
        pass


def compact_history(messages, keep_full: int = HISTORY_KEEP_FULL) -> None:
    """Ужимает ответы инструментов, кроме keep_full последних."""
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    stale = tool_idx[:-keep_full] if keep_full else tool_idx
    for i in stale:
        content = messages[i].get("content") or ""
        if len(content) > HISTORY_TRIM:
            messages[i] = {**messages[i], "content": content[:HISTORY_TRIM] + " …[обрезано]"}


def msg_text(msg: dict) -> str:
    content = (msg.get("content") or "").strip()
    return content or (msg.get("thinking") or "").strip()


# ----------------------------------------------------------------------------
# Агент
# ----------------------------------------------------------------------------
def corpus_seed(problem: str) -> str:
    """Затравка: поиск по словам самого пользователя, до того как модель
    переформулирует их общими юридическими оборотами и потеряет деталь,
    по которой норма и находится."""
    try:
        hits = corpus.search(problem, k=3)
    except Exception:  # noqa: BLE001 — затравка не обязательна
        return ""
    if not hits:
        return ""
    block = "\n".join(
        f"- {h['act'][:60]} (act={h['slug']}), пункт {h['clause']}: {h['text'][:280]}"
        for h in hits
    )
    return ("\n\nПредварительный поиск по твоим же словам дал такие фрагменты "
            f"(проверь их и поищи сам, если нужно точнее):\n{block}")


def run_agent(agent_id: int, problem: str, system_prompt: str, tools, exec_tool, tag: str):
    """Возвращает dict {verdict, legal_refs, summary} либо None при сбое."""
    user_msg = problem
    if tag == "корпус":
        user_msg += corpus_seed(problem)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            msg = chat(messages, tools=tools)
            messages.append(msg)
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                break
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                log(f"[agent {agent_id}/{tag}] {name}({json.dumps(args, ensure_ascii=False)[:110]})")
                messages.append({"role": "tool", "tool_name": name,
                                 "content": exec_tool(name, args)})
            compact_history(messages)

        messages.append({"role": "user", "content": FINAL_JSON_INSTRUCTION})
        result = None
        for attempt in range(2):
            if attempt:
                compact_history(messages, keep_full=0)
                log(f"[agent {agent_id}/{tag}] ответ оборвался, повтор с ужатой историей")
            msg = chat(messages, fmt=VERDICT_SCHEMA)
            content = msg_text(msg)
            if not content:
                raise ValueError("модель вернула пустой ответ (content и thinking пусты)")
            try:
                result = json.loads(content)
                break
            except json.JSONDecodeError:
                m = re.search(r"\{.*\}", content, re.DOTALL)
                if m:
                    try:
                        result = json.loads(m.group(0))
                        break
                    except json.JSONDecodeError:
                        pass
                if attempt:
                    raise
        if result.get("verdict") not in VERDICT_RU:
            raise ValueError(f"недопустимый verdict: {result.get('verdict')!r}")
        return result
    except Exception as e:  # noqa: BLE001 — один упавший агент не валит остальных
        log(f"[agent {agent_id}/{tag}] сбой: {e}")
        return None


# ----------------------------------------------------------------------------
# Верификация норм
# ----------------------------------------------------------------------------
def verify_corpus_refs(agent_id: int, result: dict) -> dict:
    """Приводит нормы к тексту корпуса.

    Сначала пункт ищется по номеру: если он есть, цитатой становится текст
    из корпуса, а не присланный моделью. Требовать побуквенного копирования
    от небольшой модели бессмысленно — она уверенно выбирает нужную норму,
    но пересказывает её формулировку, и строгая проверка теряла правильно
    найденные пункты. Норма без опознанного пункта проверяется по цитате,
    а если и та не нашлась — отбрасывается."""
    kept = []
    for ref in result.get("legal_refs", []):
        chunk = corpus.resolve_ref(ref.get("act", ""), ref.get("clause", ""))
        if chunk:
            matched = corpus.match_quote(ref.get("quote", ""), corpus.normalize(chunk["text"]))
            if not matched:
                matched = corpus.lead_sentence(chunk["text"])
                log(f"[agent {agent_id}] цитата заменена текстом пункта "
                    f"{chunk['clause']}: присланная не совпала с корпусом")
            kept.append({**ref, "act": chunk["act"], "clause": chunk["clause"],
                         "quote": matched, "url": chunk["url"], "origin": "корпус"})
            continue

        # Пункт не опознан — последний шанс — найти цитату по всему корпусу.
        ok, act_title, url, matched = corpus.verify_quote(ref.get("quote", ""))
        if ok:
            kept.append({**ref, "act": act_title, "quote": matched,
                         "url": url, "origin": "корпус"})
        else:
            log(f"[agent {agent_id}] норма отброшена: в корпусе нет ни пункта "
                f"{ref.get('clause', '')!r}, ни этой цитаты")
    result["legal_refs"] = kept
    return result


def verify_web_refs(agent_id: int, result: dict, tools: websearch.WebTools) -> dict:
    """То же для веба.

    Цитата ищется среди страниц, которые агенты реально открыли, а не по
    URL из ответа: модель охотно подменяет прочитанный источник на более
    авторитетный домен из промпта, и проверка по её ссылке отбрасывала бы
    вполне добросовестные нормы. В ответ идёт тот URL, где цитата нашлась."""
    kept = []
    for ref in result.get("legal_refs", []):
        quote, claimed = ref.get("quote", ""), ref.get("url", "")
        real_url, matched = tools.find_quote(quote, corpus.match_quote, corpus.normalize)
        if not real_url and claimed.startswith("http"):
            # Указанную страницу мог не открыть никто — пробуем скачать сами.
            try:
                tools.fetch(claimed)
                real_url, matched = tools.find_quote(quote, corpus.match_quote, corpus.normalize)
            except Exception:  # noqa: BLE001 — недоступная страница = неподтверждённая норма
                real_url = None

        if real_url:
            if claimed != real_url:
                log(f"[agent {agent_id}] источник исправлен: цитата взята с {real_url[:60]}, "
                    f"а не с указанного {claimed[:40]}")
            kept.append({**ref, "quote": matched, "url": real_url, "origin": "веб"})
        else:
            log(f"[agent {agent_id}] норма отброшена: цитаты нет ни на одной "
                f"открытой странице ({ref.get('act', '')[:40]})")
    result["legal_refs"] = kept
    return result


# ----------------------------------------------------------------------------
# Консенсус
# ----------------------------------------------------------------------------
def ref_key(ref: dict):
    """Нормализованный ключ нормы: (идентификатор акта, статья/пункт).
    Сопоставление намеренно строгое: лучше недосчитать пересечения, чем склеить разные нормы."""
    act = (ref.get("act") or "").lower()
    codes = (("жилищ", "жк"), ("градостроит", "грк"), ("гражданск", "гк"), ("коап", "коап"))
    act_id = next((tag for marker, tag in codes if marker in act), None)
    if act_id is None:
        m = re.search(r"№\s*(\d+)", act)
        act_id = m.group(1) if m else re.sub(r"[^0-9a-zа-яё]+", "", act)[:24]
    clause = ".".join(re.findall(r"\d+", ref.get("clause") or ""))
    return (act_id, clause)


def build_consensus(results):
    """Возвращает (verdict | None, согласованные нормы, распределение вердиктов)."""
    verdict_counts = Counter(r["verdict"] for r in results)
    top_verdict, top_count = verdict_counts.most_common(1)[0]
    if top_count < MIN_CONSENSUS:
        return None, [], verdict_counts

    seen_by_agent = []
    for r in results:
        keys = {}
        for ref in r.get("legal_refs", []):
            k = ref_key(ref)
            if k not in keys:
                keys[k] = ref
        seen_by_agent.append(keys)
    key_counts = Counter(k for keys in seen_by_agent for k in keys)
    consensus_refs, added = [], set()
    for keys in seen_by_agent:
        for k, ref in keys.items():
            if key_counts[k] >= MIN_CONSENSUS and k not in added:
                consensus_refs.append({**ref, "agents": key_counts[k]})
                added.add(k)
    return top_verdict, consensus_refs, verdict_counts


# ----------------------------------------------------------------------------
# Финальный ответ
# ----------------------------------------------------------------------------
def final_answer(problem: str, verdict: str, refs: list) -> str:
    refs_block = "\n".join(
        f"- {r['act']}, {r['clause']} (подтверждена {r['agents']} из {AGENTS} агентов, "
        f"источник: {r.get('origin', '—')})\n"
        f"  Дословная цитата: «{r['quote']}»\n  Ссылка: {r.get('url', '')}"
        for r in refs
    ) or "(согласованных норм нет)"
    user = (
        f"Проблема пользователя:\n{problem}\n\n"
        f"Согласованный вердикт: {verdict} — {VERDICT_RU[verdict]}\n\n"
        f"Подтверждённые нормы:\n{refs_block}\n\n"
        "Сформируй окончательный ответ пользователю."
    )
    msg = chat(
        [{"role": "system", "content": FINAL_SYSTEM_PROMPT}, {"role": "user", "content": user}],
        keep_alive=0,  # выгрузить модель сразу после результирующего ответа
    )
    return msg_text(msg)


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="SLLM: УК-ассистент")
    parser.add_argument("problem", help="Описание проблемной ситуации в многоквартирном доме")
    args = parser.parse_args()

    try:
        corpus.load()
    except (FileNotFoundError, RuntimeError) as e:
        sys.exit(str(e))

    proc, searx = None, None
    try:
        proc = ensure_ollama()
        if not model_installed():
            sys.exit(f"Не установлена модель {MODEL}. "
                     f"Чтобы установить, воспользуйтесь командой: ollama pull {MODEL}")

        # --- фаза 1: корпус ------------------------------------------------
        results = {}
        if FORCE_WEB:
            log("[sllm] SLLM_FORCE_WEB=1: корпус пропущен, сразу веб-фаза")
        else:
            log(f"[sllm] фаза 1 (корпус): {AGENTS} агентов, модель {MODEL}, "
                f"порог консенсуса {MIN_CONSENSUS}")
            with ThreadPoolExecutor(max_workers=AGENTS) as pool:
                futures = [pool.submit(run_agent, i + 1, args.problem, CORPUS_SYSTEM_PROMPT,
                                       corpus.TOOLS, corpus.exec_tool, "корпус")
                           for i in range(AGENTS)]
                raw = [f.result() for f in futures]

            for i, r in enumerate(raw, 1):
                if r is not None:
                    results[i] = verify_corpus_refs(i, r)
                    log(f"[agent {i}/корпус] вердикт: {r['verdict']}, "
                        f"подтверждённых норм: {len(r['legal_refs'])}")

        # --- фаза 2: веб, только для агентов без подтверждённых норм --------
        need_web = [i for i in range(1, AGENTS + 1)
                    if i not in results or not results[i]["legal_refs"]]
        if need_web and USE_WEB:
            log(f"[sllm] фаза 2 (веб): агентам {need_web} корпуса не хватило")
            searx = SearxRuntime()
            base = searx.ensure()
            if base:
                tools = websearch.WebTools(base)
                with ThreadPoolExecutor(max_workers=len(need_web)) as pool:
                    futures = {i: pool.submit(run_agent, i, args.problem, WEB_SYSTEM_PROMPT,
                                              websearch.TOOLS, tools.exec_tool, "веб")
                               for i in need_web}
                    for i, f in futures.items():
                        r = f.result()
                        if r is not None:
                            r = verify_web_refs(i, r, tools)
                            log(f"[agent {i}/веб] вердикт: {r['verdict']}, "
                                f"подтверждённых норм: {len(r['legal_refs'])}")
                            if i not in results or not results[i]["legal_refs"]:
                                results[i] = r
        elif need_web:
            log(f"[sllm] агентам {need_web} корпуса не хватило, веб-фаза отключена (SLLM_WEB=0)")
        else:
            log("[sllm] корпуса хватило всем агентам, веб-поиск не понадобился")

        # --- консенсус -----------------------------------------------------
        # Голос без единой подтверждённой нормы не учитывается: иначе «я ничего
        # не нашёл» весит столько же, сколько разобранная со ссылками позиция.
        voting = [r for r in results.values() if r.get("legal_refs")]
        if len(voting) < MIN_CONSENSUS:
            sys.exit(
                f"Норм с подтверждёнными цитатами хватило только у {len(voting)} агентов "
                f"из {AGENTS} — консенсус (минимум {MIN_CONSENSUS}) недостижим.\n"
                "Переформулируйте проблему конкретнее или добавьте нужный акт в корпус "
                "(corpus_update.py)."
            )

        verdict, refs, dist = build_consensus(voting)
        dist_str = ", ".join(f"{VERDICT_RU[v]}: {c}" for v, c in dist.most_common())
        if verdict is None:
            sys.exit(f"Агенты не сошлись во мнении (порог {MIN_CONSENSUS}): {dist_str}.\n"
                     "Ответ не сформирован — переформулируйте проблему или запустите повторно.")

        # Детерминированный блок консенсуса — печатается скриптом, а не моделью.
        print("=" * 72)
        print(f"ВЕРДИКТ: {VERDICT_RU[verdict]}  [{dist_str}]")
        print(f"Согласованные нормы (подтверждены ≥{MIN_CONSENSUS} из {AGENTS} агентов):")
        if refs:
            for r in refs:
                print(f"  • {r['act']}, {r['clause']} [{r.get('origin', '—')}] {r.get('url', '')}")
                print(f"    «{r['quote']}»")
        else:
            print("  (пересечений по нормам не набралось — проверьте обоснование вручную)")
        print("=" * 72, flush=True)

        log("[sllm] генерирую финальный ответ...")
        print(final_answer(args.problem, verdict, refs))
    finally:
        if searx is not None:
            searx.shutdown()
        if ollama_is_up():
            unload_model()
        if proc is not None:
            log("[sllm] тушу ollama, поднятую скриптом...")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
