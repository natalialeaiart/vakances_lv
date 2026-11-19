"""
Бот вакансий kurdarbs.lv
Раз в день ищет вакансии по словам из config.json, фильтрует и сохраняет в data/jobs.json.
Страница index.html читает этот файл и показывает ленту.
"""
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
DATA_FILE = ROOT / "data" / "jobs.json"
BASE = "https://www.kurdarbs.lv"
TZ = ZoneInfo("Europe/Riga")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "lv,en;q=0.8,ru;q=0.6",
}


# ---------- сопоставление слов ----------

def make_pattern(words):
    """Короткие слова (<=4 символа) — только целиком, длинные — как часть слова."""
    parts = []
    for w in words:
        w = w.strip().lower()
        if not w:
            continue
        esc = re.escape(w)
        if len(w) <= 4:
            parts.append(rf"(?<!\w){esc}(?!\w)")
        else:
            parts.append(esc)
    return re.compile("|".join(parts), re.IGNORECASE) if parts else None


def whole_words(words):
    parts = [rf"(?<!\w){re.escape(w.lower())}(?!\w)" for w in words if w.strip()]
    return re.compile("|".join(parts), re.IGNORECASE) if parts else None


GROUPS = []
for g in CONFIG["groups"]:
    GROUPS.append({
        "id": g["id"],
        "title": make_pattern(g["title"]),
        "desc": make_pattern(g.get("description", [])),
    })
EXCLUDE = make_pattern(CONFIG["exclude_title"])
REMOTE = make_pattern(CONFIG["remote_words"])
HYBRID = make_pattern(CONFIG["hybrid_words"])
CITIES = whole_words(CONFIG["allowed_cities"])
UNKNOWN_LOC = {w.lower() for w in CONFIG["unknown_location_words"]}


def classify(job):
    """Возвращает (группы, совпало_по_описанию) или None, если вакансия не подходит."""
    title, desc = job["title"], job["description"]
    if EXCLUDE and EXCLUDE.search(title):
        return None
    groups, by_desc = [], False
    for g in GROUPS:
        if g["title"] and g["title"].search(title):
            groups.append(g["id"])
    if not groups:
        for g in GROUPS:
            if g["desc"] and g["desc"].search(desc):
                groups.append(g["id"])
        by_desc = bool(groups)
    return (groups, by_desc) if groups else None


def location_tags(job):
    """Удалёнка — вся Латвия; офис/гибрид — только разрешённые города; без города — оставляем."""
    text = f'{job["title"]} {job["description"]} {job["location"]}'
    tags = []
    remote = bool(REMOTE and REMOTE.search(text))
    hybrid = bool(HYBRID and HYBRID.search(text))
    loc = job["location"].strip().lower()
    if remote:
        tags.append("remote")
    if hybrid:
        tags.append("hybrid")
    if remote:
        return tags
    if CITIES and CITIES.search(loc):
        return tags
    if loc in UNKNOWN_LOC:
        return tags + ["no_city"]
    return None  # другой город, не удалёнка


# ---------- разбор страницы ----------

REL = re.compile(r"pirms\s+(\d+)\s+(min|stund|dien|nedēļ|mēneš)", re.IGNORECASE)


def posted_date(text, today):
    t = text.lower()
    if "vakar" in t:
        return today - timedelta(days=1)
    m = REL.search(t)
    if not m:
        return today
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit.startswith("dien"):
        return today - timedelta(days=n)
    if unit.startswith("nedēļ"):
        return today - timedelta(weeks=n)
    if unit.startswith("mēneš"):
        return today - timedelta(days=30 * n)
    return today  # минуты / часы


def parse_page(html, today):
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    for ad in soup.select("div.ad"):
        a = ad.select_one("h2 a")
        if not a or not a.get("href"):
            continue
        href = a["href"]
        if href.startswith("/"):
            href = BASE + href
        m = re.search(r"goItem\('[^']*',\s*'(\d+)'", a.get("onclick", "") or a.get("OnClick", ""))
        job_id = m.group(1) if m else re.sub(r"[?#].*$", "", href)

        comp = ad.select_one("p.company")
        comp_text = comp.get_text(" ", strip=True).replace("\xa0", " ") if comp else ""
        company, _, location = (s.strip() for s in comp_text.partition(" - "))
        comp_link = comp.select_one("a") if comp else None
        if comp_link:
            company = comp_link.get_text(strip=True)

        desc = ad.select_one("p.description")
        site = ad.select_one("p.site")
        site_text = site.get_text(" ", strip=True).replace("\xa0", " ") if site else ""
        source = site_text.split(" - ")[0].strip()

        jobs.append({
            "id": job_id,
            "title": a.get_text(" ", strip=True),
            "url": href,
            "company": company,
            "location": location,
            "description": desc.get_text(" ", strip=True) if desc else "",
            "source": source,
            "posted": posted_date(site_text, today).isoformat(),
        })
    has_next = bool(soup.select_one("div.paging a[href*='/p/']")) and "»" in (
        soup.select_one("div.paging").get_text() if soup.select_one("div.paging") else "")
    return jobs, has_next


# ---------- сбор ----------

def fetch(session, query, page):
    url = f"{BASE}/vakance/{quote(query.replace(' ', '+'), safe='+')}"
    if page > 1:
        url += f"/p/{page}"
    r = session.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def all_queries():
    qs = []
    for g in CONFIG["groups"]:
        qs += g["search"]
    qs += CONFIG.get("extra_searches", [])
    seen, out = set(), []
    for q in qs:
        k = q.lower()
        if k not in seen:
            seen.add(k)
            out.append(q)
    return out


def main():
    now = datetime.now(TZ)
    today = now.date()
    session = requests.Session()
    found, errors = {}, 0

    for q in all_queries():
        for page in range(1, CONFIG["max_pages_per_search"] + 1):
            try:
                html = fetch(session, q, page)
            except Exception as e:
                errors += 1
                print(f"  ! {q} p{page}: {e}", file=sys.stderr)
                break
            jobs, has_next = parse_page(html, today)
            for j in jobs:
                found.setdefault(j["id"], j)
            print(f"{q!r} p{page}: {len(jobs)}")
            time.sleep(1.2)
            if not has_next or not jobs:
                break

    if not found and errors:
        sys.exit("Сайт не ответил ни на один запрос — данные не обновлены.")

    # слияние с прошлыми днями (старые тоже перепроверяются — если вы поменяли config.json)
    old = {}
    if DATA_FILE.exists():
        old = {j["id"]: j for j in json.loads(DATA_FILE.read_text(encoding="utf-8")).get("jobs", [])}
    for jid, j in found.items():
        prev = old.get(jid, {})
        j["first_seen"] = prev.get("first_seen", today.isoformat())
        j["posted"] = min(j["posted"], prev.get("posted", j["posted"]))
        old[jid] = j

    # фильтры
    cutoff = (today - timedelta(days=CONFIG["archive_days"])).isoformat()
    jobs = []
    for j in old.values():
        if j["posted"] < cutoff:
            continue
        c = classify(j)
        if not c:
            continue
        loc = location_tags(j)
        if loc is None:
            continue
        j["groups"], by_desc = c
        j["tags"] = loc + (["by_desc"] if by_desc else [])
        jobs.append(j)
    jobs.sort(key=lambda j: (j["first_seen"], j["posted"]), reverse=True)
    matched = [j for j in jobs if j["first_seen"] == today.isoformat()]

    DATA_FILE.parent.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps({
        "updated": now.strftime("%Y-%m-%d %H:%M"),
        "today": today.isoformat(),
        "groups": [{"id": g["id"], "name": g["name"]} for g in CONFIG["groups"]],
        "jobs": jobs,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Просмотрено: {len(found)}, подошло сегодня: {len(matched)}, в ленте: {len(jobs)}")


if __name__ == "__main__":
    main()
