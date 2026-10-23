"""Public source adapters. A changed/unreadable schema is an error, never no news."""
from __future__ import annotations

import io
import ipaddress
import json
import os
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

from .core import CST, clean, eligible, in_scope, normalized_name, safe_url, stamp
from .catalog import cached_directory, certification
from .browser import hk_browser_pages
from .brands import discover as brand_catalog, inspect as brand_detail
from .letour import collect as letour_items
from .marathons import discover as marathon_catalog, candidates as marathon_candidates, inspect as marathon_detail

UTMB_URL = "https://utmb.world/en/utmb-world-series-events"
ZUI_URL = "https://reg.zuicool.com/"
WTM_URL = "https://hk100ultra.com/world-trail-majors/"
DIRECTORY_URL = "https://file.shuzixindong.com/changzheng/84554/fddbe29f6e434035918201d2a17dbcac.pdf"
SPORT = re.compile(r"越野|跑山|跑步|路跑|马拉松|攀岩|抱石|mountain\s*run|trail\s*run|climb|boulder", re.I)
REGION = re.compile(r"上海|江苏|浙江|安徽|杭州|南京|苏州|无锡|宁波|绍兴|湖州|莫干山|德清|常州|嘉兴|合肥|黄山|温州|金华|台州|丽水|南通|扬州|镇江|舟山|Shanghai|Jiangsu|Zhejiang|Anhui|Mogan", re.I)
BRAND = re.compile(r"凯乐石|KAILAS|FUGA|始祖鸟|ARC.?TERYX", re.I)


class SourceError(RuntimeError):
    pass


def public_url(url):
    if not safe_url(url):
        raise SourceError("来源 URL 无效")
    host = urlsplit(url).hostname
    try:
        addresses = socket.getaddrinfo(host, None)
    except OSError:
        raise SourceError(f"来源域名无法解析：{host}") from None
    if not addresses or any(not ipaddress.ip_address(v[4][0]).is_global for v in addresses):
        raise SourceError("来源不是公网地址")


class Client:
    def __init__(self, attempts=3, timeout=20):
        self.attempts, self.timeout = attempts, timeout

    def get(self, url, binary=False):
        original = url
        for attempt in range(self.attempts):
            retry_after = 1
            try:
                url = original
                for _ in range(6):
                    public_url(url)
                    r = requests.get(url, timeout=(5, self.timeout), allow_redirects=False, stream=True,
                                     headers={"User-Agent": "Mozilla/5.0 (compatible; RaceRegistrationMonitor/1.0; +https://github.com/biblioth/race-monitor)", "Accept-Language": "zh-CN,en;q=0.8"})
                    if r.is_redirect:
                        url = urljoin(url, r.headers.get("Location", ""))
                        r.close()
                        continue
                    if r.status_code == 429:
                        value = r.headers.get("Retry-After", "1")
                        retry_after = min(60, int(value)) if value.isdigit() else 30
                    r.raise_for_status()
                    chunks, size = [], 0
                    for part in r.iter_content(65536):
                        size += len(part)
                        if size > 15_000_000:
                            raise SourceError("来源响应过大")
                        chunks.append(part)
                    r.close()
                    data = b"".join(chunks)
                    if binary:
                        return data
                    return data.decode("utf-8-sig", errors="replace")
                raise SourceError("来源重定向次数过多")
            except (requests.RequestException, SourceError) as e:
                alternate_status = ""
                wp_page = {"https://hk100ultra.com/": 2, "https://hk100ultra.com/entry/": 4, WTM_URL: 291}.get(original)
                if wp_page:
                    # These public API links are advertised in the official
                    # HTML's rel=alternate tags. No login or cookies are used.
                    try:
                        api = requests.get(f"https://hk100ultra.com/wp-json/wp/v2/pages/{wp_page}", timeout=(5, 20))
                        if api.status_code == 200:
                            value = api.json()
                            content = value.get("content", {}).get("rendered")
                            if isinstance(content, str) and len(content) > 200:
                                return content
                        alternate_status = f"；公开API HTTP {api.status_code}"
                    except (requests.RequestException, ValueError):
                        alternate_status = "；公开API读取失败"
                fallback = {"https://hk100ultra.com/": "hk_home", "https://hk100ultra.com/entry/": "hk_entry", WTM_URL: "wtm_members",
                            "https://www.kailas.com.cn/Home/Article/BrandNews/type/1.html": "kailas_news"}.get(original)
                proxy = os.environ.get("RACE_MONITOR_URL", "").rstrip("/")
                token = os.environ.get("RACE_MONITOR_ADMIN_TOKEN", "")
                if fallback and not binary and proxy.startswith("https://") and token:
                    try:
                        alternate = requests.get(proxy + "/races/source", params={"name": fallback}, headers={"Authorization": f"Bearer {token}"}, timeout=25)
                        if alternate.status_code == 200 and isinstance(alternate.json().get("html"), str):
                            return alternate.json()["html"]
                        alternate_status += f"；备用读取 HTTP {alternate.status_code}"
                        if alternate.status_code == 502:
                            alternate_status += f"（源站 {alternate.json().get('upstreamStatus', '读取失败')}）"
                    except (requests.RequestException, ValueError):
                        pass
                error = f"{urlsplit(original).hostname}：{type(e).__name__}"
                if isinstance(e, requests.HTTPError):
                    error += f" HTTP {e.response.status_code}"
                error += alternate_status
                if attempt + 1 < self.attempts:
                    time.sleep(max(retry_after, 2 ** attempt))
        raise SourceError(f"{self.attempts} 次尝试失败（{error}）")


def soup(html):
    s = BeautifulSoup(html, "html.parser")
    for node in s.select("script:not([type='application/json']), style, noscript"):
        node.decompose()
    return s


def next_data(html):
    tag = BeautifulSoup(html, "html.parser").select_one("#__NEXT_DATA__")
    if not tag:
        raise SourceError("UTMB 页面缺少结构化赛事数据")
    try:
        return json.loads(tag.string)["props"]["pageProps"]
    except (ValueError, KeyError, TypeError):
        raise SourceError("UTMB 数据结构已变化") from None


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def utmb_index(html):
    p = next_data(html)
    events = {}
    for node in walk(p):
        if node.get("tenant") and node.get("dateBegin") and node.get("title") and safe_url(node.get("url")):
            events[(node["tenant"], node["dateBegin"][:4])] = node
    if len(events) < 2:
        raise SourceError("UTMB 完整赛历为空或结构异常")
    return list(events.values())


def utmb_event(client, event):
    url = event["url"]
    html = client.get(url)
    p = next_data(html)
    current = p.get("event", {})
    begin = str(current.get("begin") or event["dateBegin"])[:10]
    end = str(current.get("end") or event.get("dateEnd") or begin)[:10]
    races = {str(x["id"]): x for x in walk(p.get("slices", [])) if x.get("id") and x.get("raceStatus") and x.get("name")}
    # Global menus can contain OTHER events' registration links. The current
    # page header and current site's explicit Register control are the only CTAs.
    own_link = p.get("pageHeader", {}).get("link") or {}
    registration = safe_url(urljoin(url, own_link.get("slug") or own_link.get("url") or "")) if re.search(r"register|报名", str(own_link.get("label", "")), re.I) else ""
    if not registration:
        registration = next((urljoin(url, a["href"]) for a in soup(html).select("a[href]") if re.search(r"^(?:register(?: now)?|registration|报名)$", clean(a.get_text()), re.I)
                             and (urlsplit(urljoin(url, a["href"])).hostname == urlsplit(url).hostname
                                  or event["tenant"].replace("-", "") in re.sub(r"[^a-z0-9]", "", a["href"].lower()))), "")
    groups = []
    statuses = []
    for race in races.values():
        distance = next((x.get("value") for x in walk(race.get("details", {})) if x.get("name") == "distance"), None)
        race_status = race["raceStatus"].get("status")
        suffix = {"registration_sold_out": "（已满）", "registration_closed": "（已截止）", "registration_open": "（报名中）"}.get(race_status, "")
        groups.append(f"{race['name']}" + (f" {distance}km" if distance else "") + suffix)
        statuses.append(race["raceStatus"].get("status"))
    status = "open" if "registration_open" in statuses else {"registration_open": "open", "registration_closed": "closed", "registration_sold_out": "sold_out"}.get(event.get("status", {}).get("status"), "preview")
    if statuses and all(x in {"registration_closed", "registration_sold_out"} for x in statuses):
        status = "closed"
    return [{"id": f"utmb:{event['tenant']}:{begin[:4]}", "series_id": f"utmb:{event['tenant']}", "edition": begin[:4],
             "name": f"{begin[:4]} {clean(current.get('name') or event['title'])}", "category": "utmb", "date": begin, "date_end": end,
             "continent": event.get("continent"),
             "location": clean(f"{event.get('country', '')} · {event.get('placeName', '')}"), "groups": "；".join(groups)[:1600] or "／".join(event.get("raceCategories", [])),
             "status": status, "registration": safe_url(registration), "official": url, "source": UTMB_URL,
             "verified": True, "priority": 0}]


def directory_rows(data, url):
    if not data.startswith(b"%PDF"):
        raise SourceError("田协目录响应不是 PDF")
    pages = [p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages]
    text = "\n".join(pages)
    edition = re.search(r"(20\d{2})\s*年(?:全国)?马拉松赛事目录", text)
    if not edition:
        raise SourceError("无法确认田协目录年份和标题")
    rows = []
    for page_no, page in enumerate(pages, 1):
        # Row numbers are unique, and each row ends in A/B/C; wrapping inside
        # names/organisers is allowed. A row's province and name are not guessed.
        pattern = r"(?:^|\n)\s*(\d{1,4})\s+([^\s]+)\s+((?:20\d{2})[\s\S]*?)\s+(全程\s*/\s*半程|全程|半程)\s+([ABC])(?=\s|$)"
        for m in re.finditer(pattern, page):
            body = clean(m[3])
            date = re.search(r"(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)?", body)
            if not date:
                continue
            name = clean(body[:date.start()])
            rows.append({"id": f"caa:{edition[1]}:{m[1]}", "edition": edition[1], "number": int(m[1]), "province": m[2], "name": name,
                         "category": m[5], "groups": clean(m[4]), "date": f"{edition[1]}-{int(date[1]):02d}" + (f"-{int(date[2]):02d}" if date[2] else ""), "evidence": url, "page": page_no})
    if len(rows) < 20 or len({r["number"] for r in rows}) != len(rows):
        raise SourceError("田协目录行数不足或存在重复序号")
    return rows


def match_directory(name, year, rows):
    n = normalized_name(name)
    exact = [x for x in rows if x["edition"] == str(year) and normalized_name(x["name"]) == n]
    # Don't fuzzy-match A-class certification: similar names may be different races.
    return exact[0] if len(exact) == 1 else None


def chinese_dates(text, default_year=None):
    result = []
    for m in re.finditer(r"(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日?", text):
        year = m[1] or default_year
        if not year:
            continue
        try:
            result.append(datetime(int(year), int(m[2]), int(m[3]), tzinfo=CST).date().isoformat())
        except ValueError:
            continue
    return result


def fuji_items(client):
    home = client.get("https://www.mtfuji100.com/")
    s = soup(home)
    info_link = next((urljoin("https://www.mtfuji100.com/", a["href"]) for a in s.select("a[href]") if "大会要項" in a.get_text()), None)
    if not info_link:
        raise SourceError("环富士山大会要项链接缺失")
    info = soup(client.get(info_link)).get_text(" ", strip=True)
    entry_url = "https://www.mtfuji100.com/racers/regarding/"
    entry_s = soup(client.get(entry_url))
    text = entry_s.get_text(" ", strip=True)
    edition = re.search(r"(?:Mt\.?FUJI\s*100|大会名)\D{0,12}(20\d{2})", info, re.I)
    if not edition:
        years = re.findall(r"(20\d{2})年のレース", text)
        if not years:
            raise SourceError("环富士山无法核实当届年份")
        year = years[0]
    else:
        year = edition[1]
    # Extract the general entry section, never the preceding priority entry.
    section = re.search(r"一般エントリー[\s\S]{0,100}?エントリー期間\s*(20\d{2})年(\d+)月(\d+)日[\s\S]{0,12}?(\d+)時(\d+)分[〜～~]\s*(?:(20\d{2})年)?(\d+)月(\d+)日[\s\S]{0,12}?(\d+)時(\d+)分", text)
    if not section:
        raise SourceError("环富士山普通报名窗口格式已变化")
    v = section.groups()
    jst = timezone(timedelta(hours=9))
    opens = datetime(*map(int, v[:5]), tzinfo=jst).isoformat()
    closes = datetime(int(v[5] or v[0]), *map(int, v[6:]), tzinfo=jst).isoformat()
    dates = chinese_dates(info, year)
    dates = [d for d in dates if d.startswith(year)]
    groups = list(dict.fromkeys(re.findall(r"FUJI\s*100mi|KAI\s*70k|ASUMI\s*40k|SAKUYA|Mt\.FUJI100 Mini", text, re.I)))
    windows = [{"label": "FUJI100mi／KAI70k／ASUMI40k 普通报名", "opens": opens, "closes": closes}]
    short = re.search(r"④\s*SAKUYA[\s\S]{0,60}?エントリー期間\s*(20\d{2})年(\d+)月(\d+)日[\s\S]{0,15}?[～〜~](20\d{2})年(\d+)月(\d+)日", text)
    if short:
        v = list(map(int, short.groups()))
        windows.append({"label": "SAKUYA／Mt.FUJI100 Mini（仅公布日期，日本当地日期）", "opens": f"{v[0]:04d}-{v[1]:02d}-{v[2]:02d}", "closes": f"{v[3]:04d}-{v[4]:02d}-{v[5]:02d}"})
    return [{"id": f"wtm:fuji:{year}", "series_id": "wtm:fuji", "edition": year, "name": f"{year} 环富士山 Mt.FUJI100", "category": "wtm",
             "date": dates[0] if dates else None, "date_end": dates[-1] if dates else None, "location": "日本·富士山地区", "groups": "／".join(groups),
             "status": "preview", "opens": opens, "closes": closes, "registration_windows": windows, "method": "主要组别普通报名超额抽签；捐赠名额及SAKUYA／Mini先到先得",
             "qualification": "按组别核对ITRA完赛要求；优先报名另有资格条件", "official": entry_url, "registration": entry_url,
             "source": entry_url, "verified": True, "priority": 0}]


def hk_items(client):
    try:
        home = client.get("https://hk100ultra.com/")
        entry = client.get("https://hk100ultra.com/entry/")
        return hk_page_items(home, entry, "official_http")
    except SourceError as original:
        try:
            home, entry = hk_browser_pages()
            return hk_page_items(home, entry, "official_browser")
        except Exception as e:
            raise SourceError(f"{original}；隔离浏览器备用失败（{type(e).__name__}）") from None


def hk_page_items(home, entry_html, transport):
    url = "https://hk100ultra.com/"
    s = soup(home)
    text = s.get_text(" ", strip=True)
    edition = re.search(r"(20\d{2})\s+(?:Edition|Events)", text)
    if not edition:
        raise SourceError("港百官网缺少当届年份")
    entry_url = urljoin(url, "entry/")
    entry = soup(entry_html).get_text(" ", strip=True)
    if "ENTRY PROCEDURES" not in entry or "Charity" not in entry:
        raise SourceError("港百报名规则结构已变化")
    charity = next((safe_url(urljoin(url, a["href"])) for a in s.select("a[href]") if "CHARITY REGISTRATION" in a.get_text().upper()), "")
    published_entry = next((safe_url(urljoin(url, a["href"])) for a in s.select("a[href]") if "raceresults.com.hk" in a["href"] and clean(a.get_text()).lower() == "register"), "")
    # Do not infer available regular entries from a generic Register button.
    days = [int(x) for x in re.findall(r"Jan(?:uary)?\s+(\d{1,2})[ ,]+" + edition[1], text, re.I)]
    days += [int(x) for x in re.findall(r"(\d{1,2})\s+January\s+" + edition[1], text)]
    date = f"{edition[1]}-01-{min(days):02d}" if days else None
    return [{"id": f"wtm:hk100:{edition[1]}", "series_id": "wtm:hk100", "edition": edition[1], "name": f"{edition[1]} 港百 Hong Kong 100", "category": "wtm",
             "date": date, "location": "中国香港", "groups": "HK100／The Half／The Third／Grand Sam",
             "status": "charity" if charity else "announced", "method": "普通名额抽签；慈善名额另行报名，实际余位请在报名平台核实",
             "qualification": "年龄及越野经验要求请查看当届规程；出现报名入口不代表当前开放或有剩余名额", "official": entry_url, "registration": charity or published_entry or entry_url,
             "source": url, "verified": True, "verification_method": transport, "priority": 0}]


def wtm_members(html, evidence=WTM_URL):
    s = soup(html)
    members = []
    cards = []
    # Mt.FUJI's official member list is a second public source with direct
    # organizer links; it remains usable when Hong Kong's page is blocked.
    for entry in s.select("ul.disc > li"):
        link = entry.select_one("b a[href]")
        if link:
            cards.append((clean(link.get_text(" ", strip=True)), entry.get_text(" ", strip=True), urljoin(evidence, link["href"])))
    # Each official member card has its own heading and series Details link.
    for heading in s.select("h3"):
        name = clean(heading.get_text(" ", strip=True))
        if not name or not re.search(r"100|Ultra|Marathon|Trail|MIUT|Transgrancanaria", name, re.I):
            continue
        parent = heading.parent
        card = parent.get_text(" ", strip=True)
        # Ascend only inside this card, not a parent that contains every country.
        for _ in range(3):
            if len(parent.select("h3")) != 1:
                break
            card = parent.get_text(" ", strip=True)
            if any("worldtrailmajors.com/" in a["href"] for a in parent.select("a[href]")):
                break
            parent = parent.parent
        link = next((safe_url(a["href"]) for a in parent.select("a[href]") if "worldtrailmajors.com/" in a["href"]), "")
        cards.append((name, card, link))
    for name, card, link in cards:
        slug = normalized_name(name)
        if "hongkong100" in slug:
            key = "hong-kong-100"
        elif "fuji100" in slug:
            key = "mt-fuji-100"
        elif "vietnammountainmarathon" in slug:
            key = "vietnam-mountain-marathon"
        else:
            key = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or slug
        asia = key in {"hong-kong-100", "mt-fuji-100", "vietnam-mountain-marathon"} or bool(re.search(r"\b(?:Asia|China|Japan|Hong Kong|Vietnam|Thailand|Malaysia|Taiwan|Korea|Indonesia|Philippines|Nepal|India|Bhutan|Laos|Cambodia|Singapore|Oman|Kazakhstan|Kyrgyzstan|Mongolia|Taipei|United Arab Emirates|Saudi Arabia)\b|亚洲|中国|日本|韩国|泰国|越南|香港|ベトナム|タイ王国|マレーシア|台湾|韓国|インドネシア|フィリピン|ネパール|ブータン|モンゴル|オマーン", card, re.I))
        members.append({"id": key, "name": name, "asia": asia, "location": "亚洲" if asia else "", "url": link, "evidence": evidence, "verified": True})
    if len(members) < 3 or not any(m["id"] == "hong-kong-100" for m in members):
        raise SourceError("WTM 官方成员清单结构已变化")
    return members


def member_catalog(client):
    errors = []
    for url in [WTM_URL, "https://www.mtfuji100.com/world-trail-majors/"]:
        try:
            return wtm_members(client.get(url), evidence=url)
        except SourceError as e:
            errors.append(str(e))
    raise SourceError("；".join(errors))


def zui_cards(html, allow_empty=False):
    s = soup(html)
    cards = []
    for card in s.select(".reg-race-block"):
        a = card.select_one(".race-name-a[href]")
        if not a:
            continue
        tag = a.select_one(".label")
        title = clean(a.get_text(" ", strip=True).replace(tag.get_text() if tag else "", ""))
        text = card.get_text(" ", strip=True)
        # A promotional paragraph can precede the actual date/location block.
        # Read a dated paragraph with its own <br> location, never the first <p>.
        fields = next((list(p.stripped_strings) for p in card.select("p")
                       if len(list(p.stripped_strings)) >= 2 and chinese_dates(next(p.stripped_strings, ""))), [])
        dates = chinese_dates(fields[0] if fields else "")
        location = clean(fields[-1]) if len(fields) > 1 else ""
        cards.append({"name": title, "url": urljoin(ZUI_URL, a["href"]), "text": text, "date": dates[0] if dates else None,
                      "date_end": dates[-1] if dates else None,
                      "location": location,
                      "status": {"报名中": "open", "已截止": "closed", "已关闭": "closed", "已满": "sold_out", "报名预告": "preview", "即将报名": "preview"}.get(tag.get_text(strip=True) if tag else "", "unknown")})
    if not cards and allow_empty and s.select_one("#angReg_frontpage") and s.select_one("input[name=q]") and "抱歉，暂时没有相关赛事可以报名" in s.get_text():
        return []
    if not cards or not any(c["date"] and c["location"] for c in cards):
        raise SourceError("最酷赛事列表为空或未读到日期／地点，结构可能已变化")
    return cards


def zui_detail(client, card):
    return brand_detail(client, card)


def collect(now, client=None, directory_cache=None):
    client = client or Client()
    directory_cache = dict(directory_cache or {})
    items, members, reports, tasks, registry, leads, marathon_checks = [], [], {}, [], [], [], []

    def run(name, fn):
        try:
            value = fn()
            reports[name] = {"ok": True, "checked_at": now.isoformat()}
            return value
        except Exception as e:
            reports[name] = {"ok": False, "error": clean(str(e))[:220], "checked_at": now.isoformat()}
            return None

    index = run("utmb:index", lambda: utmb_index(client.get(UTMB_URL)))
    for event in index or []:
        if in_scope(dict(event, category="utmb")) and event.get("dateEnd", event["dateBegin"]) >= now.date().isoformat():
            tasks.append((f"utmb:{event['tenant']}", lambda e=event: utmb_event(client, e)))
    tasks.extend([("wtm:fuji", lambda: fuji_items(client)), ("wtm:hk100", lambda: hk_items(client)),
                  ("brand:letour", lambda: letour_items(client, now, run))])
    m = run("wtm:members", lambda: member_catalog(client))
    members.extend(m or [])
    # Annual catalog facts are optional evidence. Daily discovery never waits
    # for a PDF download or an aggregator unrelated to the chosen platform.
    auxiliary = {}
    for url in sorted(set(directory_cache) | {DIRECTORY_URL}):
        cached = cached_directory(url, directory_cache.get(url))
        for row in (cached or {}).get("rows", []):
            auxiliary[(row["edition"], row["id"])] = row
    registry = list(auxiliary.values())
    cards = run("platform:zuicool", lambda: zui_cards(client.get(ZUI_URL)))
    catalog, marathon_stats = marathon_catalog(client, now, run)
    candidates = marathon_candidates(catalog, cards or [], now)
    marathon_stats["regional_candidates"] = len(candidates)
    for candidate in candidates:
        name = "caa:zuicool:detail:" + urlsplit(candidate["url"]).path.strip("/").replace("/", ":")
        tasks.append((name, lambda c=candidate: marathon_detail(client, c, registry, run)))
    brand_cards, brand_stats = brand_catalog(client, cards or [], now, run, zui_cards)
    for card in brand_cards:
        tasks.append(("brand:zuicool:detail:" + urlsplit(card["url"]).path.strip("/"), lambda c=card: zui_detail(client, c)))
    brand_checks = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fn): name for name, fn in tasks}
        for future in as_completed(futures):
            name = futures[future]
            try:
                found = future.result()
                reports[name] = {"ok": True, "checked_at": now.isoformat(), "items": len(found)}
                if name.startswith("brand:zuicool:detail:"):
                    brand_stats["details_read"] += 1
                    brand_checks.extend(found)
                for item in found:
                    if item.get("category") == "caa":
                        marathon_checks.append(item)
                    if item.get("verified") and in_scope(item):
                        items.append(item)
                    elif not item.get("verified"):
                        leads.append({k: item.get(k) for k in ["id", "name", "category", "province", "source", "official", "brand", "reason", "certification_state"]})
            except Exception as e:
                reports[name] = {"ok": False, "error": clean(str(e))[:220], "checked_at": now.isoformat()}
    marathon_stats.update(details_read=len(marathon_checks), verified=sum(bool(x["verified"]) for x in marathon_checks), pending=sum(not x["verified"] for x in marathon_checks), notify_eligible=sum(eligible(x, now) for x in marathon_checks), detail_failures=len(candidates) - len(marathon_checks))
    brand_stats.update(verified=sum(bool(x["verified"]) for x in brand_checks), pending=sum(not x["verified"] for x in brand_checks),
                       notify_eligible=sum(eligible(x, now) for x in brand_checks), detail_failures=len(brand_cards) - brand_stats["details_read"])
    return {"items": items, "members": members, "sources": reports, "marathon_coverage": marathon_stats, "brand_coverage": brand_stats, "directory_cache": directory_cache, "registry_count": len(registry), "a_class_count": sum(r["category"] == "A" for r in registry),
            "paused_sources": {"brand:kailas": {"since": "2026-10-23", "reason": "官网云端405，按用户选择暂用朗途／最酷补漏"}},
            "review_leads": leads, "gaps": ["凯乐石官网暂停采集，暂用朗途／最酷补漏，官网独有活动可能遗漏", "品牌会员小程序、微信群及仅图片发布的招募尚未接入；始祖鸟仅通过最酷补充发现，官网已按用户要求移除", "马拉松发现以最酷列表及分页为主，平台未收录、仅海报信息及待核验A类条目可能漏检或延迟", "WTM总站受限时使用港百／环富士山官方成员清单，成员同步可能延迟"]}
