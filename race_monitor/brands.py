"""Zuicool brand discovery; platform evidence stays distinct from official claims."""
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from .core import clean, normalized_name, safe_url
from .marathons import parse_detail

BASE = "https://reg.zuicool.com/"
KEYWORDS = ("凯乐石", "KAILAS", "FUGA", "始祖鸟", "ARC'TERYX", "ARCTERYX")
BRAND = re.compile(r"凯乐石|KAILAS|FUGA|始祖鸟|ARC.?TERYX", re.I)
REGION = re.compile(r"^(?:上海|江苏|浙江|安徽|Shanghai|Jiangsu|Zhejiang|Anhui)", re.I)
SPORT = re.compile(r"越野|跑山|跑步|路跑|马拉松|攀岩|抱石|mountain\s*run|trail\s*run|climb|boulder|FUGA\s*训练赛", re.I)


def registration_url(url):
    parsed = urlsplit(safe_url(url))
    match = re.fullmatch(r"/(?:en/)?(\d+)/?", parsed.path)
    return BASE + match[1] if parsed.hostname == "reg.zuicool.com" and match else ""


def discover(client, home, now, read, parse_cards, max_pages=10):
    cards = {registration_url(c["url"]): c for c in home if registration_url(c["url"])}
    stats = {"search_pages": 0, "search_complete": True, "search_counts": {}, "details_read": 0}
    for keyword in KEYWORDS:
        url, visited, total = BASE + "?" + urlencode({"q": keyword}), set(), 0
        while url:
            if url in visited or len(visited) >= max_pages:
                read("brand:zuicool:search:" + keyword + ":pagination", lambda: (_ for _ in ()).throw(ValueError("最酷品牌检索分页循环或达到上限，覆盖不完整")))
                stats["search_complete"] = False
                break
            visited.add(url)

            def page(u=url):
                html = client.get(u)
                root = BeautifulSoup(html, "html.parser")
                query = root.select_one("input[name=q]")
                if not root.select_one("#angReg_frontpage") or not query or query.get("value", "").casefold() != keyword.casefold():
                    raise ValueError("最酷品牌检索未返回对应关键词，可能为验证页或结构变化")
                found = parse_cards(html, allow_empty=True)
                next_page = next((urljoin(u, a["href"]) for a in root.select("#angReg_frontpage a[href]")
                                  if re.search(r"下一页|Next", a.get_text(), re.I)), None)
                if next_page:
                    parsed = urlsplit(next_page)
                    if parsed.scheme != "https" or parsed.netloc != "reg.zuicool.com" or parsed.path != "/" or [q.casefold() for q in parse_qs(parsed.query).get("q", [])] != [keyword.casefold()]:
                        raise ValueError("最酷品牌检索分页跳到未核验入口")
                return found, next_page

            value = read("brand:zuicool:search:" + keyword + ":" + str(len(visited)), page)
            if value is None:
                stats["search_complete"] = False
                break
            found, url = value
            stats["search_pages"] += 1
            total += len(found)
            for card in found:
                key = registration_url(card["url"])
                if key:
                    cards[key] = dict(card, url=key)
        stats["search_counts"][keyword] = total
    candidates = [dict(c, url=url) for url, c in cards.items()
                  if c.get("date") and (c.get("date_end") or c["date"]) >= now.date().isoformat()
                  and c.get("status") not in {"closed", "sold_out", "cancelled"}
                  and REGION.search(c.get("location", ""))
                  and (SPORT.search(c["name"] + " " + c.get("text", "")) or BRAND.search(c["name"]))]
    stats.update(list_entries=len(cards), regional_candidates=len(candidates))
    return sorted(candidates, key=lambda c: c["url"]), stats


def brand_evidence(name, text):
    relation = re.search(r"(?:主办(?:单位)?|冠名(?:品牌)?|赞助(?:商)?|合作伙伴|支持单位)[：:\s]*[^。；;\n]{0,70}(?:凯乐石|KAILAS|FUGA|始祖鸟|ARC.?TERYX)", text, re.I)
    if relation:
        return clean(relation[0]) + "（最酷当届正文所载）", "platform_relationship"
    if re.search(r"凯乐石|KAILAS|始祖鸟|ARC.?TERYX", name, re.I):
        return "最酷当届标题标注品牌（平台信息，合作身份未独立核实）", "platform_title"
    if re.search(r"FUGA\s*(?:训练赛|越野|跑山)", name, re.I):
        return "最酷当届标题标注 FUGA 训练／跑山系列（平台信息，合作身份未独立核实）", "platform_series"
    return "", ""


def inspect(client, card):
    url = registration_url(card["url"])
    if not url:
        raise ValueError("品牌报名详情不是已核验的最酷赛事入口")
    html = client.get(url)
    # parse_detail scopes all fields to this event, excluding site navigation.
    root = BeautifulSoup(html, "html.parser").select_one("#angReg_viewRace")
    if not root:
        raise ValueError("品牌报名详情缺少独立赛事正文，可能为验证页")
    for node in root.select(".events-list,.recommend,.recommendations,nav,footer,#footer,.floating-guide"):
        node.decompose()
    # Regional sport listings also contain ordinary events unrelated to either
    # monitored brand. Their missing edition is not a brand-source failure.
    if not BRAND.search(root.get_text("\n", strip=True)):
        return []
    parsed = parse_detail(str(root), url)
    expected_year = re.search(r"20\d{2}", card["name"])
    if expected_year and parsed["edition"] != expected_year[0]:
        raise ValueError("品牌报名详情届次与列表不一致")
    if normalized_name(parsed["name"]) != normalized_name(card["name"]):
        raise ValueError("品牌报名详情标题与列表不一致")
    text = root.get_text("\n", strip=True)
    brand, kind = brand_evidence(parsed["name"], text)
    if not brand and not BRAND.search(text):
        return []
    if not SPORT.search(parsed["name"] + " " + text):
        return []
    if not parsed["date"] or not parsed["location"]:
        raise ValueError("品牌报名详情缺少活动日期或地点")
    fees = []
    for table in root.select("table"):
        price = table.select_one(".price")
        if price:
            fees.append(clean(price.get_text()))
    return [{"id": f"brand:zui:{urlsplit(url).path.strip('/')}:{parsed['edition']}",
             "edition": parsed["edition"], "name": parsed["name"], "category": "brand", "date": parsed["date"],
             "location": parsed["location"], "closes": parsed["closes"], "groups": parsed["groups"], "status": parsed["status"],
             "official": url, "registration": url, "source": BASE, "evidence": url, "brand": brand or "品牌关系待核验",
             "brand_evidence_kind": kind, "verified": bool(brand) and parsed["status"] != "unknown", "in_region": bool(REGION.search(parsed["location"])),
             "reason": "品牌仅出现在当届正文，缺少标题或合作说明，待核验" if not brand else "报名信息尚未公布" if parsed["status"] == "unknown" else "",
             "method": "先到先得" if re.search(r"先报先得|先到先得", text) else None,
             "fee": "／".join(dict.fromkeys(fees)) or None, "priority": 2}]
