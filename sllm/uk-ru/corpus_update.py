#!/usr/bin/env python3
"""
Загрузчик корпуса НПА для uk-ru.

Скачивает тексты актов в corpus/*.txt и пишет метаданные в corpus/index.json.
Запускается вручную, а не при каждом запуске ассистента: корпус лежит в
репозитории, поэтому смена редакции видна в дифе и не происходит внезапно.

Источники:
  ips    — ИПС «Законодательство России» (pravo.gov.ru), официальный.
           Адресация по ND; номер действующей редакции (rdk) берётся из карточки.
  sudact — зеркало текстов НПА. Используется там, где ND неизвестен: форма
           поиска ИПС работает только через JS, а ND — это порядковый номер
           ввода документа в систему, не выводимый из даты и номера акта.

Чтобы перевести акт на официальный источник, найдите его в ИПС вручную
(pravo.gov.ru/ips → поиск → ND в адресе страницы) и впишите nd в реестр ACTS.

Запуск:
  python3 corpus_update.py              # обновить всё
  python3 corpus_update.py --only zhk   # только один акт
  python3 corpus_update.py --list       # показать реестр и состояние
"""

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
import trafilatura

CORPUS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus")
INDEX_PATH = os.path.join(CORPUS_DIR, "index.json")

IPS_BASE = "http://pravo.gov.ru/proxy/ips/"
SUDACT_BASE = "https://sudact.ru/law/"
UA = "Mozilla/5.0 (compatible; uk-ru corpus updater)"
TIMEOUT = 90
PAUSE = 1.0  # пауза между запросами, чтобы не долбить портал

# ----------------------------------------------------------------------------
# Реестр актов
# ----------------------------------------------------------------------------
# nd     — идентификатор в ИПС (приоритетный, официальный источник)
# sudact — путь на зеркале, используется только при отсутствии nd
# marker — строка, которая обязана встретиться в скачанном тексте; страховка
#          от того, что идентификатор протух и скачался чужой документ
# extract — (от, до): вырезать только фрагмент. Для кодексов, где нужна
#          пара статей, а не мегабайты текста.
ACTS = [
    {
        "slug": "zhk-rf",
        "title": "Жилищный кодекс Российской Федерации",
        "nd": "102090645",
        "marker": "реестр лицензий субъекта",
    },
    {
        "slug": "pp-491",
        "title": "Постановление Правительства РФ от 13.08.2006 № 491 "
                 "(Правила содержания общего имущества в многоквартирном доме)",
        "nd": "102108472",
        "marker": "общего имущества",
    },
    {
        "slug": "pp-290",
        "title": "Постановление Правительства РФ от 03.04.2013 № 290 "
                 "(Минимальный перечень услуг и работ по содержанию общего имущества)",
        "nd": "102164374",
        "marker": "минимальн",
    },
    {
        "slug": "pp-354",
        "title": "Постановление Правительства РФ от 06.05.2011 № 354 "
                 "(Правила предоставления коммунальных услуг)",
        "nd": "102147807",
        "marker": "коммунальн",
    },
    {
        "slug": "pp-416",
        "title": "Постановление Правительства РФ от 15.05.2013 № 416 "
                 "(Правила осуществления деятельности по управлению МКД)",
        "nd": "102165338",
        "marker": "управлени",
    },
    {
        "slug": "gosstroy-170",
        "title": "Постановление Госстроя РФ от 27.09.2003 № 170 "
                 "(Правила и нормы технической эксплуатации жилищного фонда)",
        "url": "https://sudact.ru/law/postanovlenie-gosstroia-rf-ot-27092003-n-170/",
        "crawl_toc": True,  # документ разбит на разделы, собираем по оглавлению
        "marker": "жилищного фонда",
    },
    {
        "slug": "fz-59",
        "title": "Федеральный закон от 02.05.2006 № 59-ФЗ "
                 "(О порядке рассмотрения обращений граждан РФ)",
        "nd": "102106413",
        "marker": "обращени",
    },
    {
        "slug": "koap-7-22",
        "title": "КоАП РФ, статьи 7.22–7.23 (нарушение правил содержания "
                 "и ремонта жилых домов, нормативов обеспечения коммунальными услугами)",
        "nd": "102074277",
        # Кодекс целиком — это мегабайты про всё подряд; в корпусе нужны две статьи.
        "extract": ("Статья 7.22", "Статья 7.24"),
        "marker": "7.22",
    },
]


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def html_to_text(raw: bytes, encoding: str) -> str:
    """HTML → плоский текст. Сущности раскрываются, неразрывные пробелы
    приводятся к обычным: иначе дословный поиск цитаты не найдёт совпадений."""
    doc = raw.decode(encoding, errors="replace")
    doc = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", doc)
    doc = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", doc)
    doc = re.sub(r"<[^>]+>", " ", doc)
    doc = html.unescape(doc).replace("\xa0", " ").replace("​", "")
    doc = re.sub(r"[ \t\r\f\v]+", " ", doc)
    doc = re.sub(r"\n\s*\n\s*\n+", "\n\n", doc)
    return "\n".join(line.strip() for line in doc.split("\n")).strip()


def get(url: str, encoding_hint: str = "utf-8") -> str:
    time.sleep(PAUSE)
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    return html_to_text(r.content, encoding_hint)


def get_raw(url: str) -> bytes:
    time.sleep(PAUSE)
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.content


# ----------------------------------------------------------------------------
# Источник: ИПС pravo.gov.ru
# ----------------------------------------------------------------------------
def fetch_ips(nd: str):
    """Карточка → номер последней редакции → полный текст.

    page=all обязателен: без него портал отдаёт документ обрезанным
    (ЖК РФ обрывается на статье 131 из 202)."""
    card = get_raw(f"{IPS_BASE}?docbody=&nd={nd}")
    rdks = {int(x) for x in re.findall(rb"rdk=(\d+)", card)}
    rdk = max(rdks) if rdks else 0
    url = f"{IPS_BASE}?docview&page=all&print=1&nd={nd}&rdk={rdk}"
    text = html_to_text(get_raw(url), "cp1251")
    return text, url, f"редакция rdk={rdk}"


# ----------------------------------------------------------------------------
# Источник: произвольная страница (извлечение основного контента)
# ----------------------------------------------------------------------------
def extract_main(raw_html: str) -> str:
    """Только содержательная часть страницы. Забирать её целиком нельзя:
    у зеркал в разметке висит навигация по всему кодексу — для КоАП это
    160 тысяч символов меню против 5 тысяч собственно статьи."""
    text = trafilatura.extract(raw_html, include_tables=True, include_comments=False)
    return (text or "").replace("\xa0", " ").strip()


def fetch_page(url: str, crawl_toc: bool = False):
    doc = get_raw(url).decode("utf-8", errors="replace")
    text = extract_main(doc)
    if not crawl_toc:
        return text, url, "одной страницей"

    # Документ разбит по разделам: собираем ссылки оглавления и склеиваем части.
    base = re.sub(r"https?://[^/]+", "", url).rstrip("/") + "/"
    host = re.match(r"https?://[^/]+", url).group(0)
    links, seen = [], set()
    for href in re.findall(r'href="(' + re.escape(base) + r'[^"#?]+)"', doc):
        if href not in seen:
            seen.add(href)
            links.append(href)
    log(f"    оглавление: {len(links)} разделов")
    parts = [text]
    for i, href in enumerate(links, 1):
        try:
            part = extract_main(get_raw(host + href).decode("utf-8", errors="replace"))
            if part:
                parts.append(part)
        except requests.RequestException as e:
            log(f"    раздел {i}/{len(links)} не скачался: {e}")
        if i % 20 == 0:
            log(f"    скачано разделов: {i}/{len(links)}")
    return "\n\n".join(parts), url, f"оглавление + {len(links)} разделов"


# ----------------------------------------------------------------------------
def extract_range(text: str, start: str, end: str) -> str:
    i = text.find(start)
    if i < 0:
        return text
    j = text.find(end, i + len(start))
    return text[i:j] if j > 0 else text[i:]


def fetch_act(act: dict):
    if act.get("nd"):
        text, url, note = fetch_ips(act["nd"])
        source = "ips"
    elif act.get("url"):
        text, url, note = fetch_page(act["url"], act.get("crawl_toc", False))
        source = "mirror"
    else:
        raise ValueError(
            "не задан источник: впишите в реестр nd (ИПС) или url. "
            "См. комментарий рядом с актом в ACTS"
        )

    if act.get("extract"):
        text = extract_range(text, *act["extract"])

    marker = act.get("marker", "")
    if marker and marker.lower() not in text.lower():
        raise ValueError(
            f"в тексте нет ожидаемого маркера {marker!r} — "
            f"вероятно, идентификатор указывает на другой документ"
        )
    return text, url, source, note


def main() -> None:
    parser = argparse.ArgumentParser(description="Обновление корпуса НПА для uk-ru")
    parser.add_argument("--only", help="обновить только этот slug")
    parser.add_argument("--list", action="store_true", help="показать реестр и выйти")
    args = parser.parse_args()

    if args.list:
        index = {}
        if os.path.exists(INDEX_PATH):
            with open(INDEX_PATH, encoding="utf-8") as f:
                index = {a["slug"]: a for a in json.load(f)["acts"]}
        for act in ACTS:
            got = index.get(act["slug"])
            if act.get("nd"):
                src = "ips (официальный)"
            elif act.get("url"):
                src = "зеркало"
            else:
                src = "ИСТОЧНИК НЕ ЗАДАН"
            state = f"{got['chars']} симв, {got['fetched']}" if got else "не скачан"
            print(f"{act['slug']:<15} {src:<20} {state}")
            print(f"    {act['title']}")
        return

    os.makedirs(CORPUS_DIR, exist_ok=True)
    acts = [a for a in ACTS if not args.only or a["slug"] == args.only]
    if not acts:
        sys.exit(f"Нет акта со slug {args.only!r}. Доступные: "
                 + ", ".join(a["slug"] for a in ACTS))

    index, failed = [], []
    for act in acts:
        log(f"[corpus] {act['slug']}: качаю...")
        try:
            text, url, source, note = fetch_act(act)
        except Exception as e:  # noqa: BLE001 — один упавший акт не должен ронять остальные
            log(f"[corpus] {act['slug']}: ОШИБКА — {e}")
            failed.append(act["slug"])
            continue
        path = os.path.join(CORPUS_DIR, f"{act['slug']}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        index.append({
            "slug": act["slug"],
            "title": act["title"],
            "source": source,
            "url": url,
            "note": note,
            "chars": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            "fetched": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        })
        log(f"[corpus] {act['slug']}: {len(text)} символов, {source}, {note}")

    # Частичное обновление не должно стирать из индекса всё остальное.
    if args.only and os.path.exists(INDEX_PATH):
        with open(INDEX_PATH, encoding="utf-8") as f:
            old = {a["slug"]: a for a in json.load(f)["acts"]}
        for entry in index:
            old[entry["slug"]] = entry
        index = list(old.values())

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump({"acts": index}, f, ensure_ascii=False, indent=2)

    total = sum(a["chars"] for a in index)
    log(f"[corpus] готово: {len(index)} актов, {total} символов")
    if failed:
        sys.exit(f"Не скачались: {', '.join(failed)}")


if __name__ == "__main__":
    main()
