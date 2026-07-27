#!/usr/bin/env python3
"""
Поиск по локальному корпусу НПА и верификация цитат.

Зачем это вместо веб-поиска: корпус позволяет проверять цитаты кодом.
Модель не «утверждает», что открыла страницу и списала норму дословно —
скрипт сам ищет её цитату в тексте акта, и не найденная норма отбрасывается
до того, как попадёт в консенсус.

Наружу:
    load()                     — прочитать корпус (один раз на процесс)
    search(query, k)           — BM25-поиск фрагментов
    get(slug, clause)          — фрагменты конкретного пункта акта
    verify_quote(quote)        — дословно ли цитата присутствует в корпусе
    TOOLS                      — описания инструментов для модели
    exec_tool(name, args)      — выполнение вызова инструмента
"""

import json
import os
import re
import threading

from rank_bm25 import BM25Okapi

CORPUS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus")
INDEX_PATH = os.path.join(CORPUS_DIR, "index.json")

CHUNK_CHARS = 1100        # целевой размер фрагмента
MIN_QUOTE_CHARS = 25      # цитаты короче считаем неинформативными
PARTIAL_RATIO = 0.6       # какую долю цитаты обязано подтвердить частичное совпадение
SNIPPET_CHARS = 900       # сколько текста фрагмента отдаём модели

# Заголовки, по которым определяется «пункт» акта: «Статья 161.», «4.2.1.», «10.»
# Номер статьи с индексом в ИПС набран через пробел («Статья 161 1» = 161.1),
# поэтому он тоже входит в номер — иначе 161 и 161.1 склеятся в один пункт.
CLAUSE_RE = re.compile(
    r"^(?:(Статья\s+\d+(?:\s\d+)?)\s*\.?\s|(\d+(?:\.\d+){0,3})\s*[.)]\s)",
    re.MULTILINE,
)

_lock = threading.Lock()
_state = {"loaded": False, "chunks": [], "bm25": None, "acts": {}, "norm_texts": {}}


# ----------------------------------------------------------------------------
# Нормализация и токенизация
# ----------------------------------------------------------------------------
def normalize(text: str) -> str:
    """Форма для дословного сравнения: регистр, ё/е, пробелы и переносы строк
    не должны мешать совпадению — они не меняют содержания нормы."""
    text = text.lower().replace("ё", "е").replace("\xa0", " ")
    text = re.sub(r"[«»\"'`]", '"', text)
    text = re.sub(r"[–—−]", "-", text)
    return re.sub(r"\s+", " ", text).strip()


def tokenize(text: str):
    """Слова длиннее двух букв, усечённые до 7 символов — грубая замена
    стеммеру: «содержания»/«содержание»/«содержанию» дают общий токен."""
    words = re.findall(r"[а-яa-z0-9]+", text.lower().replace("ё", "е"))
    return [w[:7] for w in words if len(w) > 2]


# ----------------------------------------------------------------------------
# Загрузка и нарезка
# ----------------------------------------------------------------------------
def _split(text: str, act: dict):
    """Нарезает акт на фрагменты, запоминая для каждого ближайший
    вышестоящий пункт — он потом попадёт в ссылку на норму."""
    positions = [(m.start(), (m.group(1) or m.group(2))) for m in CLAUSE_RE.finditer(text)]
    chunks = []
    if not positions:
        positions = [(0, "")]
    for i, (start, clause) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        body = text[start:end].strip()
        if len(body) < 40:
            continue
        # Длинные пункты режем на части, чтобы фрагмент влезал в контекст модели
        for off in range(0, len(body), CHUNK_CHARS):
            piece = body[off:off + CHUNK_CHARS]
            if len(piece) < 40:
                continue
            chunks.append({
                "slug": act["slug"],
                "act": act["title"],
                "url": act.get("url", ""),
                "clause": clause,
                "text": piece,
            })
    return chunks


def load():
    """Читает корпус и строит индекс. Повторные вызовы бесплатны."""
    with _lock:
        if _state["loaded"]:
            return _state
        if not os.path.exists(INDEX_PATH):
            raise FileNotFoundError(
                f"Корпус не собран: нет {INDEX_PATH}. "
                "Запустите: python3 corpus_update.py"
            )
        with open(INDEX_PATH, encoding="utf-8") as f:
            acts = json.load(f)["acts"]

        chunks, norm_texts = [], {}
        for act in acts:
            path = os.path.join(CORPUS_DIR, f"{act['slug']}.txt")
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                text = f.read()
            norm_texts[act["slug"]] = (normalize(text), act)
            chunks.extend(_split(text, act))

        if not chunks:
            raise RuntimeError("Корпус пуст — нечего искать")

        _state.update({
            "loaded": True,
            "chunks": chunks,
            "bm25": BM25Okapi([tokenize(c["text"]) for c in chunks]),
            "acts": {a["slug"]: a for a in acts},
            "norm_texts": norm_texts,
        })
        return _state


# ----------------------------------------------------------------------------
# Поиск
# ----------------------------------------------------------------------------
def search(query: str, k: int = 5):
    st = load()
    scores = st["bm25"].get_scores(tokenize(query))
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
    out = []
    for i in order:
        if scores[i] <= 0:
            continue
        c = st["chunks"][i]
        out.append({
            "act": c["act"],
            "slug": c["slug"],
            "clause": c["clause"],
            "text": c["text"][:SNIPPET_CHARS],
            "score": round(float(scores[i]), 2),
        })
    return out


def get(slug: str, clause: str):
    st = load()
    want = normalize(clause)
    hits = [c for c in st["chunks"]
            if c["slug"] == slug and (not want or normalize(c["clause"]) == want)]
    if not hits:  # запрошенного пункта нет — отдаём похожие по номеру
        hits = [c for c in st["chunks"] if c["slug"] == slug and want in normalize(c["clause"])]
    return [{"act": c["act"], "clause": c["clause"], "text": c["text"]} for c in hits[:4]]


# ----------------------------------------------------------------------------
# Верификация цитат
# ----------------------------------------------------------------------------
def match_quote(quote: str, text_norm: str) -> str:
    """Ищет цитату в уже нормализованном тексте. Возвращает подтверждённый
    фрагмент («» если не нашлось). Общая логика для корпуса и для веба."""
    q = normalize(quote)
    if len(q) < MIN_QUOTE_CHARS:
        return ""
    if q in text_norm:
        return quote.strip()
    # Частичное совпадение: отрезаем хвост по границам слов, пока не найдём.
    words = q.split(" ")
    floor = max(MIN_QUOTE_CHARS, int(len(q) * PARTIAL_RATIO))
    while len(" ".join(words)) >= floor:
        candidate = " ".join(words)
        if candidate in text_norm:
            return candidate
        words.pop()
    return ""


def resolve_ref(act_hint: str, clause: str):
    """Находит фрагмент корпуса по номеру пункта (и подсказке об акте).

    Нужен, чтобы не полагаться на цитирование моделью: выбрать норму она
    способна, а скопировать её текст символ в символ — уже не всегда."""
    st = load()
    want = normalize(clause)
    if not want:
        return None
    exact = [c for c in st["chunks"] if normalize(c["clause"]) == want]
    if not exact:
        return None
    hint = normalize(act_hint or "")
    if hint:
        same_act = [c for c in exact
                    if normalize(c["act"])[:40] in hint or hint[:40] in normalize(c["act"])
                    or c["slug"] in hint]
        if same_act:
            return same_act[0]
    return exact[0]


def lead_sentence(text: str, limit: int = 300) -> str:
    """Первое осмысленное предложение фрагмента — запасная цитата."""
    body = re.sub(r"\s+", " ", text).strip()
    cut = body[:limit]
    dot = cut.rfind(". ")
    return (cut[:dot + 1] if dot > 60 else cut).strip()


def verify_quote(quote: str):
    """Есть ли цитата в каком-нибудь акте корпуса.

    Возвращает (найдено, название акта, url, подтверждённый фрагмент).

    Сравнение по нормализованной форме: регистр, пробелы и ё/е содержания
    нормы не меняют. Если целиком цитата не совпала, ищется её самое длинное
    начало — модель часто цитирует верно, но прихватывает лишнее в конце или
    склеивает два предложения. В ответ при этом уходит ровно тот фрагмент,
    который реально найден в тексте, а не то, что прислала модель. Пересказ
    своими словами не даст и минимального совпадения — он и отсеивается."""
    st = load()
    for slug, (text, act) in st["norm_texts"].items():
        matched = match_quote(quote, text)
        if matched:
            return True, act["title"], act.get("url", ""), matched
    return False, "", "", ""


def corpus_summary() -> str:
    st = load()
    return ", ".join(
        f"{a['title'].split('(')[0].strip()}" for a in st["acts"].values()
    )


# ----------------------------------------------------------------------------
# Инструменты для модели
# ----------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "corpus_search",
            "description": (
                "Поиск по локальному собранию нормативных актов о содержании "
                "и управлении многоквартирными домами. Возвращает фрагменты с "
                "названием акта и номером пункта."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Запрос словами из текста нормы, например "
                                       "'входная дверь подъезда исправность запирающих устройств'",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "corpus_get",
            "description": "Полный текст конкретного пункта акта из корпуса.",
            "parameters": {
                "type": "object",
                "properties": {
                    "act": {
                        "type": "string",
                        "description": "Идентификатор акта из результатов поиска (поле slug)",
                    },
                    "clause": {
                        "type": "string",
                        "description": "Номер пункта или статьи, например '4.2.1' или 'Статья 161'",
                    },
                },
                "required": ["act", "clause"],
            },
        },
    },
]


def exec_tool(name: str, args: dict) -> str:
    try:
        if name == "corpus_search":
            return json.dumps({"results": search(args.get("query", ""))},
                              ensure_ascii=False)
        if name == "corpus_get":
            return json.dumps({"fragments": get(args.get("act", ""), args.get("clause", ""))},
                              ensure_ascii=False)
        return json.dumps({"error": f"неизвестный инструмент {name}"}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001 — сбой инструмента не должен ронять агента
        return json.dumps({"error": str(e)}, ensure_ascii=False)


if __name__ == "__main__":
    import sys
    st = load()
    print(f"Актов: {len(st['acts'])}, фрагментов: {len(st['chunks'])}")
    q = " ".join(sys.argv[1:]) or "дверь подъезда не закрывается запирающее устройство"
    for r in search(q, 5):
        print(f"\n[{r['score']}] {r['act'][:60]} — {r['clause']}")
        print("   ", r["text"][:200].replace("\n", " "))
