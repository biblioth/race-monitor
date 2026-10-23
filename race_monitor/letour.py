"""Anonymous Chinese Letour listings and their own event data; no JS execution."""
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import json5
from bs4 import BeautifulSoup

from .brands import BRAND, REGION, SPORT
from .core import CST, clean, safe_url

# The official Chinese host currently serves a mismatched HTTPS certificate.
# Fetch public pages over its working HTTP entry; never disable TLS verification
# or send authentication/cookies. Registration is always left to the user.
BASE = "http://www.letoursports.com/"
HOST = "www.letoursports.com"
EXCLUDED = re.compile(r"勋章|志愿|媒体注册|摄影|证书|纪念品|装备购买|邮寄|赛后|押金|展位|补差|退款|寄存|开票")


def own_url(value, path, key):
    parsed = urlsplit(safe_url(urljoin(BASE, value)))
    values = parse_qs(parsed.query).get(key, [])
    if parsed.hostname != HOST or parsed.path not in {path, "/m" + path} or len(values) != 1 or not values[0].isdigit():
        return ""
    return urljoin(BASE, path.lstrip("/")) + "?" + urlencode({key: values[0]})


def parse_list(html, url):
    root = BeautifulSoup(html, "html.parser")
    if not root.select_one(".sas-name") or "朗途" not in root.select_one(".sas-name").get_text():
        raise ValueError("朗途中文列表缺少站点标识，可能为验证页")
    # Some navigation categories redirect directly to one event detail page.
    if root.select_one("#SocialShareTitle") and root.select_one("#mainPane"):
        match = re.search(r"\bvar\s+eventId\s*=\s*(\d+)\s*;", "\n".join(n.get_text() for n in root.select("script:not([src])")))
        if not match:
            raise ValueError("朗途中文分类详情缺少活动 ID")
        name = clean(root.select_one("#SocialShareTitle").get_text())
        if EXCLUDED.search(name):
            return [], None
        return [{"name": name, "url": BASE + "event?id=" + match[1]}], None
    found = []
    container = root.select_one(".events-container") or root
    for node in container.select(".outline,ul.events > li"):
        title = node.select_one("a.title[href]")
        target = own_url(title["href"], "/event", "id") if title else ""
        if not target:
            continue
        location = node.select_one(".data .icon-flag + span")
        flag = node.select_one(".event-data .icon-flag")
        location_text = clean(location.get_text()) if location else clean(flag.parent.get_text()) if flag else ""
        state = node.select_one(".state")
        name = clean(title.get_text())
        if EXCLUDED.search(name):
            continue
        if location_text and not REGION.search(location_text):
            continue
        if state and re.search(r"已结束|已截止|已关闭|进行中|已满|取消", state.get_text()):
            continue
        # Brand cooperation can be stated only in the event's own regulations.
        if SPORT.search(name) or BRAND.search(name):
            found.append({"name": name, "url": target, "location": location_text})
    next_page = next((urljoin(url, a["href"]) for a in root.select(".pagination a[href],.paginator a[href],.page a[href]")
                      if re.search(r"下一页|下页|Next", a.get_text(), re.I) or a.get("rel") == ["next"]), None)
    if next_page:
        current, parsed = urlsplit(url), urlsplit(next_page)
        if parsed.hostname != HOST or parsed.path != "/events" or parse_qs(parsed.query).get("mid") != parse_qs(current.query).get("mid"):
            raise ValueError("朗途中文列表分页跳到未核验入口")
    # Empty pages are valid only when the actual list or explicit empty message
    # is present; site headers on a login/challenge page cannot imply no events.
    if not container.select_one(".outline,ul.events > li") and not re.search(r"暂无活动|没有相关活动|没有找到.*活动", container.get_text()):
        raise ValueError("朗途中文列表未读到活动或明确空列表")
    return found, next_page


def parse_detail(html, url, now):
    root = BeautifulSoup(html, "html.parser")
    title, body, pane = root.select_one("#SocialShareTitle"), root.select_one("#contentBox"), root.select_one("#mainPane")
    if not title or not body or not pane:
        raise ValueError("朗途中文详情缺少当届标题／正文／状态")
    name = clean(title.get_text())
    if EXCLUDED.search(name):
        return []
    text = body.get_text("\n", strip=True)
    compact = re.sub(r"\s+", "", text)
    if not SPORT.search(name + " " + text):
        return []
    brand = BRAND.search(name) or re.search(r"(?:主办单位|冠名品牌|赞助商|合作伙伴)[：:]?[^。；;]{0,60}(?:凯乐石|KAILAS|FUGA|始祖鸟|ARC.?TERYX)", compact, re.I)
    if not brand:
        return []
    location_match = re.search(r"(?:赛事地点|活动地点|比赛地点)[：:]\s*([^\n]{2,150})", text)
    location = clean(location_match[1]) if location_match else ""
    if not location:
        label = next((n for n in pane.select(".label") if "集合地" in n.get_text()), None)
        location = clean(label.find_next_sibling().get_text()) if label and label.find_next_sibling() else ""
    if not REGION.search(location):
        return []
    # The site publishes a literal batches array. Parse only that JSON5 data;
    # templates such as {{batch.startTime}} and JS functions are never executed.
    script = "\n".join(node.get_text() for node in root.select("script:not([src])"))
    match = re.search(r"\bvar\s+batches\s*=\s*(\[.*?\])\s*;", script, re.S)
    if not match or len(match[1]) > 200_000:
        raise ValueError("朗途中文详情未读到有限的场次数据")
    try:
        batches = json5.loads(match[1])
        valid = [b for b in batches if b.get("valid") is True and isinstance(b.get("startTime"), (int, float)) and isinstance(b.get("endTime"), (int, float))]
        dates = [(datetime.fromtimestamp(b["startTime"] / 1000, CST), datetime.fromtimestamp(b["endTime"] / 1000, CST)) for b in valid]
    except (ValueError, TypeError, OverflowError, OSError, AttributeError):
        raise ValueError("朗途中文场次数据格式已变化") from None
    if not dates:
        raise ValueError("朗途中文详情未读到有效活动日期")
    begin, end = min(x[0] for x in dates), max(x[1] for x in dates)
    year = re.search(r"20\d{2}", name)
    if year and int(year[0]) != begin.year:
        raise ValueError("朗途中文标题届次与场次日期不符")
    action = pane.select_one("button.join")
    status_text = clean(action.get_text()) if action else ""
    closed = bool(re.search(r"已截止|已结束|已关闭|进行中|已满|取消", status_text))
    opened = action is not None and not action.has_attr("disabled") and bool(re.search(r"报名|参加|立即加入", status_text))
    status = "closed" if closed else "open" if opened else "unknown"
    event_id = parse_qs(urlsplit(url).query)["id"][0]
    return [{"id": f"brand:letour-cn:{event_id}:{begin.year}", "edition": str(begin.year), "name": name,
             "date": begin.date().isoformat(), "date_end": end.date().isoformat(), "location": location,
             "category": "brand", "status": status, "official": url, "registration": url, "source": BASE,
             "groups": "；".join(dict.fromkeys(clean(b.get("title")) for b in valid))[:1000],
             "fee": "／".join(dict.fromkeys(str(b["price"]) + "元" for b in valid if isinstance(b.get("price"), (int, float)))),
             "brand": "朗途中文官方当届标题／规程标注：" + clean(brand[0]), "verified": status != "unknown",
             "reason": "" if status != "unknown" else "报名状态待核验", "in_region": True, "priority": 1}]


def collect(client, now, read):
    home = BeautifulSoup(client.get(BASE), "html.parser")
    if "朗途" not in (home.title.get_text() if home.title else ""):
        raise ValueError("朗途中文首页未读到站点标识")
    lists = sorted({target for a in home.select("a[href]") if (target := own_url(a["href"], "/events", "mid"))})
    if not lists:
        raise ValueError("朗途中文首页未读到赛事／活动分类")
    cards = {}

    def scan(url):
        visited, found = set(), []
        while url:
            if url in visited or len(visited) >= 10:
                read("brand:letour:pagination:" + url, lambda: (_ for _ in ()).throw(ValueError("朗途中文分页循环或达到上限")))
                break
            visited.add(url)
            value = read("brand:letour:list:" + url, lambda u=url: parse_list(client.get(u), u))
            if value is None:
                break
            page, url = value
            found.extend(page)
        return found

    with ThreadPoolExecutor(max_workers=4) as pool:
        for page in pool.map(scan, lists):
            for card in page:
                cards[card["url"]] = card
    items = []
    for card in cards.values():
        result = read("brand:letour:detail:" + card["url"], lambda c=card: parse_detail(client.get(c["url"]), c["url"], now))
        items.extend(result or [])
    return items
