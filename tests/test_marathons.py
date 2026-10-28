import unittest
from datetime import datetime, timedelta

from race_monitor.core import CST, merge_discoveries, item_text
from race_monitor.marathons import A_CATALOG, candidates, catalog_page, discover, evidence_state, inspect, parse_detail
from race_monitor.service import plan
from race_monitor.sources import zui_cards

NOW = datetime(2026, 10, 23, 9, tzinfo=CST)


def card(name="2027杭州半程马拉松", url="https://reg.zuicool.com/123", location="浙江·杭州", **fields):
    return dict({"name": name, "url": url, "location": location, "date": "2027-03-01", "status": "open"}, **fields)


def reg_html(claim="中国田径协会A1类认证赛事", **fields):
    values = dict(name="2027杭州半程马拉松", label="报名中", location="浙江·杭州", related="", claim=claim)
    values.update(fields)
    return '''<div id="angReg_viewRace"><h4>{name}<span>{label}</span></h4>
    <p>赛事日期：2027年3月1日</p><p>赛事地点：{location}</p>
    <p>报名截止时间：2027年2月1日 18:00:00</p><p>{claim}</p>
    <h5>半程马拉松21.0975公里</h5>{related}</div>'''.format(**values)


def event_html(claim="", action="点此报名", related="https://reg.zuicool.com/123", news=""):
    return f'''<div class="event-meta"><h1>2027杭州半程马拉松</h1>
    <div class="start_datetime-loc">2027.03.01 · 浙江 杭州</div></div>
    <div class="event-actions"><a href="{related}">{action}</a></div>
    <div class="event-desc_lead">{claim}</div><div class="event-wpposts">{news}</div>
    <div class="events-list">推荐另一场2027马拉松：中国田径协会A类认证
    <a href="https://reg.zuicool.com/999">点此报名</a></div>'''


def catalog_html(date="2027.03.01", next_url=None):
    value = f'''<div class="event-body"><h4><a class="event-a" href="https://zuicool.com/event/1">2027杭州半程马拉松</a></h4>
    <div class="info"><p>{date} · 浙江 杭州</p></div><div class="meta">点此报名</div></div>'''
    return value + (f'<a href="{next_url}">下一页 →</a>' if next_url else "")


class Client:
    def __init__(self, pages):
        self.pages, self.calls = pages, []
    def get(self, url):
        self.calls.append(url)
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return value


class MarathonTests(unittest.TestCase):
    def reader(self, reports):
        def read(name, fn):
            try:
                value = fn()
                reports[name] = {"ok": True}
                return value
            except Exception as e:
                reports[name] = {"ok": False, "error": str(e)}
        return read

    def test_pagination_is_followed_until_the_historical_cutoff(self):
        next_url = A_CATALOG + "?page=2"
        client = Client({A_CATALOG: catalog_html(next_url=next_url), next_url: catalog_html("2026.01.01", A_CATALOG + "?page=3")})
        _, stats = discover(client, NOW, self.reader({}))
        self.assertEqual(client.calls, [A_CATALOG, next_url])
        self.assertTrue(stats["catalog_complete"])
        self.assertEqual(stats["catalog_pages"], 2)

    def test_partial_pagination_failure_keeps_earlier_discoveries_and_reports_failure(self):
        next_url = A_CATALOG + "?page=2"
        reports = {}
        found, stats = discover(Client({A_CATALOG: catalog_html(next_url=next_url), next_url: RuntimeError("timeout")}), NOW, self.reader(reports))
        self.assertEqual(len(found), 1)
        self.assertFalse(stats["catalog_complete"])
        self.assertFalse(reports["caa:zuicool:catalog:2"]["ok"])
        reports = {}
        discover(Client({A_CATALOG: catalog_html(next_url=A_CATALOG)}), NOW, self.reader(reports))
        self.assertFalse(reports["caa:zuicool:pagination"]["ok"])

    def test_both_lists_filter_region_merge_duplicates_and_keep_unverified_candidates(self):
        catalog = [card(url="https://zuicool.com/event/1", status="unknown")]
        registrations = [card(), card(name="2027合肥马拉松", location="安徽合肥"), card(name="2027上海越野马拉松", location="上海"), card(name="2027上海马拉松", location="上海")]
        registrations[-1]["status"] = "closed"
        found = candidates(catalog, registrations, NOW)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["registration_hint"], "https://reg.zuicool.com/123")

    def test_registration_detail_can_confirm_a_class_without_a_pdf(self):
        item = inspect(Client({card()["url"]: reg_html()}), card(), [], self.reader({}))[0]
        self.assertTrue(item["verified"])
        self.assertEqual(item["province"], "浙江")
        self.assertIn("平台转载", item["certification"])
        self.assertIn("来源：最酷", item_text(item))

    def test_application_and_previous_edition_claims_are_not_confirmed(self):
        for text in ["拟申报中国田协A1类认证", "中国田协A类认证中", "尚未取得A类认证", "2026年中国田协A类认证赛事"]:
            item = inspect(Client({card()["url"]: reg_html(text)}), card(), [], self.reader({}))[0]
            self.assertFalse(item["verified"], text)
        self.assertEqual(evidence_state("中国田径协会A类认证赛事", "2027")[0], "declared")

    def test_recommendations_cannot_supply_certification_or_registration(self):
        source = card(url="https://zuicool.com/event/1")
        html = event_html(action="关注", related="https://zuicool.com/event/1/watch")
        item = inspect(Client({source["url"]: html}), source, [], self.reader({}))[0]
        self.assertFalse(item["verified"])
        self.assertFalse(item["registration"])
        self.assertEqual(item["status"], "unknown")

    def test_only_the_current_linked_regulation_can_supply_evidence(self):
        source = card(url="https://zuicool.com/event/1")
        news_url = "https://zuicool.com/news/archives/7"
        html = event_html(news=f'<a href="{news_url}">2027竞赛规程</a>')
        pages = {source["url"]: html, "https://reg.zuicool.com/123": reg_html(""), news_url: '<h1 class="entry-title">2027竞赛规程</h1><article class="entry-content">中国田协A类认证赛事</article>'}
        item = inspect(Client(pages), source, [], self.reader({}))[0]
        self.assertTrue(item["verified"])
        self.assertEqual(item["evidence"], news_url)
        pages[news_url] = '<h1 class="entry-title">2026竞赛规程</h1><article class="entry-content">中国田协A类认证赛事</article>'
        reports = {}
        item = inspect(Client(pages), source, [], self.reader(reports))[0]
        self.assertFalse(item["verified"])
        self.assertFalse(reports["caa:zuicool:news:7"]["ok"])

    def test_pending_evidence_does_not_consume_the_first_notification(self):
        source = card()
        candidate = inspect(Client({source["url"]: reg_html("")}), source, [], self.reader({}))[0]
        state, _ = plan({}, {"items": [], "sources": {}, "members": [], "review_leads": [candidate]}, NOW)
        confirmed = inspect(Client({source["url"]: reg_html()}), source, [], self.reader({}))[0]
        self.assertEqual(len(merge_discoveries(state, [confirmed], NOW + timedelta(days=1))), 1)
        self.assertEqual(merge_discoveries(state, [dict(confirmed, groups="新增组别")], NOW + timedelta(days=2)), [])

    def test_source_migration_preserves_an_already_notified_edition(self):
        current = inspect(Client({card()["url"]: reg_html()}), card(), [], self.reader({}))[0]
        old = dict(current, id="caa:2027:100", series_id="old-official-id", name="2027冠名赞助商杭州半程马拉松", official=current["registration"] + "?hmsr=mainbanner", registration=current["registration"] + "?hmsr=mainbanner")
        state = {"known": {old["id"]: old}}
        self.assertEqual(merge_discoveries(state, [current], NOW), [])
        next_year = dict(current, id=current["id"] + ":2028", edition="2028", name="2028杭州半程马拉松", date="2028-03-01")
        self.assertEqual(len(merge_discoveries(state, [next_year], NOW)), 1)

    def test_postponement_note_cannot_override_the_actual_list_date(self):
        html = '<div class="reg-race-block"><a class="race-name-a" href="https://reg.zuicool.com/123">2027杭州半程马拉松<span class="label">报名中</span></a><small>从2026年10月1日延期</small><p>2027年3月1日<br>浙江・杭州</p></div>'
        found = zui_cards(html)[0]
        self.assertEqual(found["date"], "2027-03-01")
        self.assertEqual(found["location"], "浙江・杭州")

    def zhoushan(self, body, reg_date='比赛时间待定', meta_date='2026.11 待定'):
        source = card(name='2026舟山群岛马拉松', url='https://zuicool.com/event/84480', date='2026-11')
        reg = reg_html(claim=body, name=source['name'], related='<a href="https://zuicool.com/event/84480">更多赛事信息</a>')
        reg = reg.replace('2027年3月1日', reg_date).replace('2027年2月1日', '2026年11月18日')
        meta = event_html(body, related='https://reg.zuicool.com/34604').replace('2027杭州半程马拉松', source['name']).replace('2027.03.01', meta_date)
        pages = {source['url']: meta, 'https://reg.zuicool.com/34604': reg}
        return inspect(Client(pages), source, [], self.reader({}))[0]

    def test_zhoushan_body_date_wins_over_pending_old_month_without_using_signup_or_shuttle_dates(self):
        body = ('中国田径协会A类认证赛事！2026舟山群岛马拉松定于12月6日（周日）上午7:00开赛。'
                '2026年10月28日至11月18日开放报名。领物接驳将于12月5日举行。')
        item = self.zhoushan(body)
        self.assertTrue(item['verified'])
        self.assertEqual(item['date'], '2026-12-06')
        self.assertEqual(item['date_note'], '正文公布，日期栏待定')
        self.assertEqual(item['date_status'], 'body_published')
        rendered = item_text(item)
        self.assertIn('2026-12-06（正文公布，日期栏待定）', rendered)
        self.assertNotIn('2026-11｜', rendered)
        self.assertIn('2026-11-18', rendered)  # The signup deadline remains independent.
        self.assertTrue(any(e['kind'] == 'body' and e['source'] == 'https://reg.zuicool.com/34604' for e in item['date_evidence']))

    def test_pending_date_without_a_race_statement_does_not_fall_back_to_catalog_month(self):
        item = self.zhoushan('中国田径协会A类认证赛事。报名中，比赛时间待定。')
        self.assertTrue(item['verified'])
        self.assertIsNone(item['date'])
        self.assertEqual(item['date_status'], 'pending')
        self.assertIn('时间待定｜', item_text(item))

    def test_signup_pickup_deadline_and_other_races_cannot_supply_the_race_date(self):
        statements = [
            '报名时间：2026年10月28日10:00；报名截止时间：2026年11月18日12:00。',
            '领物接驳将于12月5日在起点举行。',
            '2026舟山群岛马拉松报名将于10月28日开启，仪式在当日举行。',
            '2026舟山群岛马拉松定于10月28日开放报名，比赛随后举行。',
            '2026舟山群岛马拉松将于10月28日报名，12月6日开赛。',
            '2026其他马拉松定于12月5日举行。',
            '2025舟山群岛马拉松定于11月16日开赛。',
            '比赛时间：2025年11月16日。',
            '2026舟山群岛马拉松暂定于12月6日开赛。',
            '2026舟山群岛马拉松原定于11月15日开赛。',
            '2026舟山群岛马拉松定于2月30日开赛。',
        ]
        for statement in statements:
            with self.subTest(statement=statement):
                item = self.zhoushan('中国田径协会A类认证赛事。' + statement)
                self.assertIsNone(item['date'])

    def test_two_concrete_dates_are_held_for_review_instead_of_choosing_silently(self):
        item = self.zhoushan('中国田径协会A类认证赛事。', reg_date='2026年12月6日', meta_date='2026.12.13')
        self.assertFalse(item['verified'])
        self.assertEqual(item['date_status'], 'conflict')
        self.assertIn('赛事日期互相冲突', item['reason'])

    def test_a_non_pending_old_month_conflicts_with_a_different_body_month(self):
        item = self.zhoushan('中国田径协会A类认证赛事。2026舟山群岛马拉松定于12月6日开赛。', meta_date='2026.11')
        self.assertFalse(item['verified'])
        self.assertEqual(item['date_status'], 'conflict')

    def test_explicit_race_date_labels_can_supply_a_full_body_date(self):
        item = self.zhoushan('中国田径协会A类认证赛事。比赛时间：2026年12月6日07:00。')
        self.assertEqual(item['date'], '2026-12-06')
        self.assertTrue(item['verified'])

    def test_pending_catalog_dates_remain_unknown(self):
        cards, _ = catalog_page(catalog_html('2026.11 待定'), A_CATALOG)
        self.assertIsNone(cards[0]['date'])
        self.assertIsNone(cards[0]['date_end'])

    def test_correcting_the_same_edition_date_updates_known_without_duplicate_discovery(self):
        fixed = self.zhoushan('中国田径协会A类认证赛事。2026舟山群岛马拉松定于12月6日开赛。')
        old = dict(fixed, date='2026-11', date_note='', date_status='month_only')
        state = {'known': {old['id']: old}}
        self.assertEqual(merge_discoveries(state, [fixed], NOW), [])
        self.assertEqual(state['known'][old['id']]['date'], '2026-12-06')


if __name__ == "__main__":
    unittest.main()
