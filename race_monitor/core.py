"""Identity, eligibility and Feishu rendering; no network or credentials here."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

CST = timezone(timedelta(hours=8))
CATEGORIES = {"utmb": "越野跑UTMB", "wtm": "越野跑WTM", "caa": "马拉松A类赛事", "brand": "品牌活动"}
WTM_ASIA_BASELINE = {"hong-kong-100", "mt-fuji-100", "vietnam-mountain-marathon"}


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def safe_url(value):
    """Only preserve public HTTP links, never script or data URLs."""
    try:
        p = urlsplit(str(value or ""))
        if p.scheme not in {"https", "http"} or not p.hostname or p.username or p.password:
            return ""
        return urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))
    except ValueError:
        return ""


def normalized_name(value):
    value = clean(value).lower()
    value = re.sub(r"20\d{2}|凯乐石|kailas|fuga|始祖鸟|arc.?teryx|暨第[一二三四五六七八九十\d]+届", "", value)
    return re.sub(r"[^\w\u4e00-\u9fff]", "", value)


def identity_links(item):
    """An event's own paths, without campaign query strings or site homepages."""
    links = set()
    for value in [item.get("official"), item.get("registration"), *(item.get("identity_links") or [])]:
        p = urlsplit(safe_url(value))
        path = p.path.rstrip("/")
        if not p.hostname or not path:
            continue
        if p.hostname == "reg.zuicool.com":
            match = re.fullmatch(r"/(?:en/)?(\d+)(?:/\d+)?", path)
            if not match:
                continue
            path = "/" + match[1]
            query = ""
        else:
            tracking = {"hmsr", "hmpl", "hmb", "hmkw", "hmci", "campaign", "spm", "gclid", "fbclid"}
            query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                                     if not k.lower().startswith("utm_") and k.lower() not in tracking))
        links.add(urlunsplit(("https", p.hostname.lower(), path, query, "")))
    return links


def brand_families(item):
    text = clean(item.get("name")) + " " + clean(item.get("brand"))
    return {family for family, pattern in [("kailas", r"凯乐石|kailas|fuga"), ("arcteryx", r"始祖鸟|arc.?teryx")]
            if re.search(pattern, text, re.I)}


def discovery_rank(item):
    return ({"utmb": 0, "wtm": 1, "caa": 2, "brand": 3}.get(item.get("category"), 9), item.get("priority", 10))


def stamp(value):
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return d.replace(tzinfo=CST) if d.tzinfo is None else d.astimezone(CST)
    except (ValueError, TypeError):
        return None


def display_time(value):
    if not value:
        return "待公布"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)):
        return str(value)  # Date-only evidence must remain date-only.
    d = stamp(value)
    return d.strftime("%Y-%m-%d %H:%M") if d else clean(value)


def in_scope(item):
    # Use explicit catalog/detail geography, never sponsor names or incidental city
    # mentions. Unknown geography is held back until it can be verified.
    if item.get("category") == "utmb":
        return clean(item.get("continent")).casefold() == "asia"
    if item.get("category") == "caa":
        return clean(item.get("province")) in {"江苏", "江苏省", "浙江", "浙江省", "上海", "上海市"}
    if item.get("category") == "brand":
        return bool(item.get("in_region"))
    return item.get("category") == "wtm"


def eligible(item, now):
    if item.get("category") not in CATEGORIES or not item.get("verified"):
        return False
    if not in_scope(item):
        return False
    if item.get("status") in {"closed", "sold_out", "cancelled", "past", "unknown"}:
        return False
    windows = item.get("registration_windows", [])
    end = max((stamp(w.get("closes")) for w in windows if stamp(w.get("closes"))), default=None) if windows else stamp(item.get("closes"))
    if end and end < now:
        return False
    race = str(item.get("date_end") or item.get("date") or "")[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", race) and race < now.date().isoformat():
        return False
    return bool(item.get("id") and item.get("edition") and item.get("name"))


def find_identity(item, known):
    if item["id"] in known:
        return item["id"]
    # Official IDs handle renamed/rescheduled editions. Cross-platform aliases
    # require the same edition; two different sessions on different dates stay separate.
    name = normalized_name(item["name"])
    for key, old in known.items():
        if old.get("edition") != item.get("edition"):
            continue
        both_brand = old.get("category") == item.get("category") == "brand"
        same_date = item.get("date") and item.get("date") == old.get("date")
        if both_brand and item.get("date") and old.get("date") and not same_date:
            continue
        # Explicit shared signup/event paths also join branded marathons or UTMB
        # reposts across categories. A generic website homepage cannot join them.
        shared = identity_links(item) & identity_links(old)
        own_shared = identity_links({"official": item.get("official")}) & identity_links({"official": old.get("official")})
        if own_shared or any(urlsplit(link).hostname == "reg.zuicool.com" for link in shared):
            return key
        if old.get("category") != item.get("category") and "brand" not in {old.get("category"), item.get("category")}:
            continue
        if item.get("series_id") and old.get("series_id") == item["series_id"]:
            return key
        families, old_families = brand_families(item), brand_families(old)
        if families and old_families and not families & old_families:
            continue
        def province(value):
            match = re.match(r"^(上海|江苏|浙江|安徽)", clean(value))
            return match[1] if match else None
        a, b = province(item.get("location")), province(old.get("location"))
        if a and b and a != b:
            continue
        other = normalized_name(old.get("name"))
        if same_date and len(name) >= 5 and SequenceMatcher(None, name, other).ratio() >= .92:
            return key
    return item["id"]


def merge_discoveries(state, items, now):
    """Only first discovery may notify; changes to known items never do."""
    known = state.setdefault("known", {})
    fresh = []
    for item in sorted(items, key=discovery_rank):
        key = find_identity(item, known)
        old = known.get(key)
        if old is None and not eligible(item, now):
            continue
        item = dict(item, id=key, first_seen=(old or {}).get("first_seen", now.isoformat()), last_seen=now.isoformat())
        aliases = sorted(identity_links(item) | identity_links(old or {}))
        item["identity_links"] = aliases
        if old is None and eligible(item, now):
            fresh.append(item)
        if old is None or discovery_rank(item) <= discovery_rank(old):
            known[key] = item
        else:
            known[key]["last_seen"] = now.isoformat()
            known[key]["identity_links"] = aliases
    return fresh


def item_text(item):
    labels = {"open": "报名中", "preview": "报名预告", "announced": "报名公告已发布，状态见原文", "ballot": "抽签登记", "charity": "慈善名额入口（余位未核实）"}
    date = display_time(item.get("date"))
    if item.get("date_end") and item["date_end"] != item.get("date"):
        date += " ～ " + display_time(item["date_end"])
    lines = [f"🆕 {clean(item['name'])}", f"分类：{CATEGORIES[item['category']]}",
             f"{date}｜{clean(item.get('location')) or '地点待公布'}"]
    groups = clean(item.get("groups"))
    if groups and groups != "请查看活动规程":
        lines.append("项目：" + (groups if len(groups) <= 120 else groups[:120] + "…（完整组别见入口）"))
    status = labels.get(item.get("status"), "待公布")
    if "抽签" in clean(item.get("method")) and item.get("status") != "ballot":
        status += "｜抽签／先到先得（分组别）" if "先到先得" in item["method"] else "｜抽签"
    opens, closes = item.get("opens"), item.get("closes")
    windows = item.get("registration_windows") or []
    if not windows:
        if opens and closes:
            status += f"｜{display_time(opens)} ～ {display_time(closes)}"
        elif closes:
            status += f"｜截至 {display_time(closes)}"
        elif opens:
            status += f"｜{display_time(opens)} 起"
        if any(stamp(value) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)) for value in [opens, closes]):
            status += "（北京时间）"
    lines.append("报名：" + status)
    for window in windows:
        suffix = "（北京时间）" if any(stamp(window.get(k)) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(window.get(k))) for k in ["opens", "closes"]) else ""
        lines.append(f"{clean(window['label'])}：{display_time(window.get('opens'))} ～ {display_time(window.get('closes'))}{suffix}")
    if item.get("fee"):
        lines.append("费用：" + clean(item["fee"]))
    host = urlsplit(safe_url(item.get("official"))).hostname or ""
    if host in {"zuicool.com", "reg.zuicool.com"}:
        source = "来源：最酷"
        if item.get("certification"):
            source += "（A类为目录计划类别，在线更新未核实）" if "计划类别" in item["certification"] else "（A类为平台声明，最终认证未核实）"
        elif item.get("brand_evidence_kind") == "platform_relationship":
            source += "（正文标注品牌合作）"
        lines.append(source)
    entry = safe_url(item.get("registration")) or clean(item.get("entry_path")) or safe_url(item.get("official")) or safe_url(item.get("source"))
    lines.append("入口：" + (entry or "待公布"))
    return "\n".join(lines)


def digest_events(items, now, coverage, max_bytes=15000):
    """Split on whole entries, comfortably below the text webhook byte limit."""
    blocks, chunks = [], []
    rank = {"utmb": 0, "wtm": 1, "caa": 2, "brand": 3}
    for item in sorted(items, key=lambda x: (rank[x["category"]], "上海" not in (x.get("location") or ""), x.get("date") or "", x["id"])):
        block = item_text(item)
        if blocks and len(("\n\n".join(blocks + [block])).encode()) > max_bytes - 1500:
            chunks.append(blocks)
            blocks = []
        blocks.append(block)
    if blocks:
        chunks.append(blocks)
    events = []
    for index, blocks in enumerate(chunks, 1):
        header = f"🏃 新增活动 {len(items)} 项｜{now:%Y-%m-%d}"
        if len(chunks) > 1:
            header += f"（{index}/{len(chunks)}）"
        text = header + "\n\n" + "\n\n".join(blocks)
        fingerprint = hashlib.sha256(text.encode()).hexdigest()[:24]
        events.append({"key": f"races:digest:{fingerprint}", "type": "race_digest", "payload": {"message": text}})
    return events


def member_events(state, members, now):
    known = state.setdefault("wtm_members", sorted(WTM_ASIA_BASELINE))
    events = []
    for member in members:
        if member["id"] in known or not member.get("asia") or not member.get("verified"):
            continue
        known.append(member["id"])
        text = (f"🌏 World Trail Majors 新增亚洲站\n\n赛事：{member['name']}\n"
                f"地点：{member.get('location') or '亚洲（具体地点待公布）'}\n"
                f"官方确认：成员赛事官方页面已列入 WTM 名单\n加入公告日期：{member.get('announced') or '未公布'}\n比赛及报名信息：请查看赛事官网\n"
                f"官方依据：{safe_url(member.get('evidence'))}\n赛事官网：{safe_url(member.get('url')) or '待公布'}\n\n"
                f"当前WTM报名监控仍为环富士山、港百。\n核验时间：{now:%Y-%m-%d %H:%M}（北京时间）")
        events.append({"key": f"races:wtm-asia:{member['id']}", "type": "race_wtm_asia", "payload": {"message": text}})
    return events
