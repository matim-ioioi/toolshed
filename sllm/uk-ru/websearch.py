#!/usr/bin/env python3
"""
Веб-слой второй фазы: поиск через локальный SearXNG, загрузка страниц напрямую.

Ни ключей, ни аккаунтов: поиск идёт через свой контейнер, страницы качаются
обычным requests. Единственное внешнее ограничение — вежливость к чужим
сайтам, а не чья-то квота.
"""

import json
import re
import threading

import requests
import trafilatura

import corpus_update

SEARCH_MAX_RESULTS = 5
SEARCH_TRIM = 800     # обрезка сниппета одного результата
FETCH_TRIM = 4000     # обрезка текста страницы
SEARCH_TIMEOUT = 30
FETCH_TIMEOUT = 45
UA = "Mozilla/5.0 (compatible; uk-ru legal assistant)"

# Что считаем источником права. Блоги и Q&A-сайты нормой не подтверждают,
# поэтому выдача поднимает официальные публикаторы наверх.
PREFERRED_HOSTS = (
    "pravo.gov.ru", "publication.pravo.gov.ru", "consultant.ru",
    "garant.ru", "base.garant.ru", "dom.gosuslugi.ru", "minstroyrf.gov.ru",
)


def _rank(url: str) -> int:
    host = re.sub(r"^https?://(www\.)?", "", url or "").split("/")[0]
    for i, preferred in enumerate(PREFERRED_HOSTS):
        if host.endswith(preferred):
            return i
    return len(PREFERRED_HOSTS)


class WebTools:
    """Инструменты второй фазы. Создаётся только когда SearXNG уже поднят."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        # Всё, что агенты реально открыли: {url: текст}. Верификация цитат идёт
        # по этому кэшу, а не по URL из ответа модели — модель охотно подменяет
        # прочитанную страницу на более авторитетный домен из промпта.
        self.fetched = {}
        self.snippets = {}   # то, что пришло в выдаче поиска, но не открывалось
        self._fetch_lock = threading.Lock()

    def search(self, query: str) -> str:
        r = requests.get(
            f"{self.base_url}/search",
            params={"q": query, "format": "json", "language": "ru"},
            timeout=SEARCH_TIMEOUT,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        results.sort(key=lambda item: _rank(item.get("url", "")))
        out = []
        for item in results[:SEARCH_MAX_RESULTS]:
            url = item.get("url", "")
            content = (item.get("content") or "")[:SEARCH_TRIM]
            out.append({"title": item.get("title", ""), "url": url, "content": content})
            # Сниппеты тоже проверяемый источник: модель нередко цитирует прямо
            # из выдачи, не открывая страницу. Без этого её честная цитата
            # отбрасывалась бы как ненайденная.
            if content:
                with self._fetch_lock:
                    self.snippets.setdefault(url, content)
        return json.dumps({"results": out}, ensure_ascii=False)

    def fetch(self, url: str) -> str:
        text = ""
        nd = re.search(r"pravo\.gov\.ru.*[?&]nd=(\d+)", url)
        if nd:
            # ИПС отдаёт документ обрезанным и в cp1251, зато отдаёт целиком —
            # в отличие от справочников, которые боту показывают только меню.
            try:
                text, url, _ = corpus_update.fetch_ips(nd.group(1))
            except Exception:  # noqa: BLE001 — не вышло, пробуем как обычную страницу
                text = ""
        if not text:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=FETCH_TIMEOUT)
            r.raise_for_status()
            text = trafilatura.extract(r.text, include_tables=True, include_comments=False) or ""
        text = text.replace("\xa0", " ").strip()
        if not text:  # страница без распознаваемого текста — отдаём честный пустой результат
            return json.dumps({"url": url, "content": "", "note": "текст не извлёкся"},
                              ensure_ascii=False)
        # Модели уходит обрезанный кусок, в кэш кладём всё: цитата может быть
        # дальше по тексту, а проверять её надо по полной странице.
        with self._fetch_lock:
            self.fetched[url] = text
        return json.dumps({"url": url, "content": text[:FETCH_TRIM]}, ensure_ascii=False)

    def find_quote(self, quote: str, match_quote, normalize):
        """Ищет цитату среди реально загруженных страниц.
        Возвращает (url, подтверждённый фрагмент) либо (None, "")."""
        with self._fetch_lock:
            # Открытые страницы первее: там полный текст, а сниппет обрезан.
            pages = list(self.fetched.items()) + list(self.snippets.items())
        for url, text in pages:
            matched = match_quote(quote, normalize(text))
            if matched:
                return url, matched
        return None, ""

    def exec_tool(self, name: str, args: dict) -> str:
        try:
            if name == "web_search":
                return self.search(args.get("query", ""))
            if name == "web_fetch":
                return self.fetch(args.get("url", ""))
            return json.dumps({"error": f"неизвестный инструмент {name}"}, ensure_ascii=False)
        except requests.RequestException as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Поиск в интернете. Возвращает title, url, content. "
                           "Официальные публикаторы права идут первыми.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Поисковый запрос"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Загружает страницу по URL и возвращает её текст.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "Полный URL страницы"}},
                "required": ["url"],
            },
        },
    },
]


if __name__ == "__main__":
    import sys
    from runtime import SearxRuntime

    rt = SearxRuntime()
    try:
        base = rt.ensure()
        if not base:
            sys.exit("SearXNG не поднялся")
        tools = WebTools(base)
        q = " ".join(sys.argv[1:]) or "постановление 491 состав общего имущества"
        print(tools.search(q)[:900])
    finally:
        rt.shutdown()
