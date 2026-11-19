"""
Бот вакансий: cv.lv, cvmarket.lv, ss.lv
Раз в день собирает вакансии, фильтрует по config.json и сохраняет в data/jobs.json.
Страница index.html читает этот файл и показывает ленту.
"""
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
DATA_FILE = ROOT / "data" / "jobs.json"
TZ = ZoneInfo("Europe/Riga")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "lv,en;q=0.8,ru;q=0.6",
}
PAUSE = 1.2  # секунд между запросами к одному сайту


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
    # у ss.lv название — начало текста объявления, поэтому добавляем профессию из раздела
    title = f'{job["title"]} {job.get("profession", "")}'
    desc = job["description"]
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
    remote = bool(REMOTE and REMOTE.search(text)) or job.get("remote_type") == "FULLY_REMOTE"
    hybrid = bool(HYBRID and HYBRID.search(text)) or job.get("remote_type") == "HYBRID"
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


# ---------- общее ----------

def clean(s):
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def short(s, n):
    s = clean(s)
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"


def get(session, url, **kw):
    r = session.get(url, headers=HEADERS, timeout=30, **kw)
    r.raise_for_status()
    time.sleep(PAUSE)
    return r


REL = re.compile(r"pirms\s+(?:(\d+)\s+)?(min|stund|dien|nedēļ|mēneš|mēnes)", re.IGNORECASE)


def posted_date(text, today):
    """«pirms 5 dienām», «pirms nedēļas», «vakar» → дата."""
    t = text.lower()
    if "vakar" in t:
        return today - timedelta(days=1)
    m = REL.search(t)
    if not m:
        return today
    n, unit = int(m.group(1) or 1), m.group(2).lower()
    if unit.startswith("dien"):
        return today - timedelta(days=n)
    if unit.startswith("nedēļ"):
        return today - timedelta(weeks=n)
    if unit.startswith("mēne"):
        return today - timedelta(days=30 * n)
    return today  # минуты / часы


# ---------- cv.lv (открытый JSON поиска сайта) ----------

def collect_cvlv(session, today):
    api = "https://www.cv.lv/api/v1"
    loc = get(session, f"{api}/locations-service/list").json()
    towns = {t["id"]: t["name"] for t in loc.get("towns", [])}
    counties = {c["id"]: c["name"] for c in loc.get("counties", [])}
    countries = {c["id"]: c["name"] for c in loc.get("countries", [])}

    jobs, offset, total = [], 0, None
    while total is None or offset < total:
        d = get(session, f"{api}/vacancy-search-service/search",
                params={"limit": 100, "offset": offset, "sorting": "LATEST"}).json()
        total = d.get("total", 0)
        batch = d.get("vacancies") or []
        if not batch:
            break
        for v in batch:
            title = clean(v.get("positionTitle"))
            if v.get("countryId") not in (None, 101):  # 101 = Латвия
                location = countries.get(v["countryId"], "ārzemes")
            else:
                location = towns.get(v.get("townId")) or counties.get(v.get("countyId")) or ""
            content = clean(v.get("positionContent"))
            if content.lower().startswith(title.lower()):
                content = content[len(title):]
            dates = [x for x in (v.get("publishDate"), v.get("renewedDate")) if x]
            posted = max(datetime.fromisoformat(x).astimezone(TZ).date() for x in dates) if dates else today
            jobs.append({
                "id": f'cv-{v["id"]}',
                "title": title,
                "url": f'https://www.cv.lv/lv/vacancy/{v["id"]}',
                "company": clean(v.get("employerName")),
                "location": location,
                "description": short(content, 300),
                "source": "CV.lv",
                "posted": posted.isoformat(),
                "remote_type": v.get("remoteWorkType") or "",
            })
        offset += len(batch)
        print(f"  cv.lv: {offset}/{total}")
    return jobs


# ---------- cvmarket.lv (поиск по словам из config.json) ----------

def collect_cvmarket(session, today):
    base = "https://www.cvmarket.lv"
    jobs = []
    for q in all_queries():
        r = get(session, f"{base}/darba-piedavajumi", params={"op": "search", "search[keyword]": q})
        soup = BeautifulSoup(r.content, "html.parser")
        n = 0
        for art in soup.select('article[data-component="jobad"]'):
            a = art.select_one("a.jobad-url")
            h = art.select_one("h2")
            if not a or not h:
                continue
            job_id = art.get("data-component-jobid") or re.sub(r"\D", "", a["href"][-12:])
            comp = art.select_one(".job-company")
            city = art.select_one(".location")
            date = next((s.get_text(strip=True) for s in art.select("span")
                         if re.match(r"(pirms|vakar|šodien)", s.get_text(strip=True), re.I)), "")
            href = a["href"]
            jobs.append({
                "id": f"cvm-{job_id}",
                "title": clean(h.get_text(" ")),
                "url": href if href.startswith("http") else base + href,
                "company": clean(comp.get_text(" ")) if comp else "",
                "location": clean(city.get_text(" ")) if city else "",
                "description": "",
                "source": "CVMarket.lv",
                "posted": posted_date(date, today).isoformat(),
            })
            n += 1
        print(f"  cvmarket {q!r}: {n}")
    return jobs


# ---------- ss.lv (разделы профессий из config.json) ----------

def collect_ss(session, today):
    base = "https://www.ss.lv"
    jobs = []
    for section, profession in CONFIG.get("ss_sections", {}).items():
        url = f"{base}/lv/work/are-required/{section}/"
        # даты берём из RSS этого же раздела
        dates = {}
        try:
            rss = ET.fromstring(get(session, url + "rss/").content)
            for it in rss.iter("item"):
                link, pub = it.findtext("link", "").strip(), it.findtext("pubDate", "").strip()
                if link and pub:
                    dates[link] = parsedate_to_datetime(pub).astimezone(TZ).date()
        except Exception as e:
            print(f"  ! ss.lv {section} rss: {e}", file=sys.stderr)

        soup = BeautifulSoup(get(session, url).content, "html.parser")
        n = 0
        for tr in soup.select('tr[id^="tr_"]'):
            if not tr["id"][3:].isdigit():
                continue  # рекламные строки
            a = tr.select_one("a.am")
            if not a:
                continue
            text = clean(a.get_text(" "))
            region = tr.select_one(".ads_region")
            cells = tr.find_all("td", recursive=False)
            link = base + a["href"]
            jobs.append({
                "id": f'ss-{tr["id"][3:]}',
                "title": short(text, 110),
                "url": link,
                "company": clean(cells[3].get_text(" ")) if len(cells) >= 5 else "",
                "location": clean(region.get_text(" ")) if region else "",
                "description": text if len(text) > 110 else "",
                "source": "ss.lv",
                "posted": dates.get(link, today).isoformat(),
                "profession": profession,
            })
            n += 1
        print(f"  ss.lv {section}: {n}")
    return jobs


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


SOURCES = {"cv.lv": collect_cvlv, "cvmarket.lv": collect_cvmarket, "ss.lv": collect_ss}


# ---------- сбор ----------

def main():
    now = datetime.now(TZ)
    today = now.date()
    session = requests.Session()
    found, keys, failed = {}, set(), []

    enabled = CONFIG.get("sources", {k: True for k in SOURCES})
    for name, collect in SOURCES.items():
        if not enabled.get(name, False):
            continue
        print(f"== {name}")
        try:
            jobs = collect(session, today)
        except Exception as e:
            failed.append(name)
            print(f"  ! {name} не ответил: {e}", file=sys.stderr)
            continue
        added = 0
        for j in jobs:
            # одна и та же вакансия может быть на двух сайтах
            key = (j["title"].lower(), j["company"].lower())
            if j["id"] in found or key in keys:
                continue
            found[j["id"]] = j
            keys.add(key)
            added += 1
        print(f"  {name}: {len(jobs)} объявлений, новых в выборке {added}")

    if not found:
        sys.exit("Ни один источник не ответил — данные не обновлены.")

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
    print(f"Просмотрено: {len(found)}, подошло сегодня: {len(matched)}, в ленте: {len(jobs)}"
          + (f". Не ответили: {', '.join(failed)}" if failed else ""))


if __name__ == "__main__":
    main()
