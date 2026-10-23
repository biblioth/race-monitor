import unittest
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from race_monitor.brands import BASE, KEYWORDS, discover, inspect
from race_monitor.core import CST, merge_discoveries, item_text
from race_monitor.sources import SourceError, zui_cards

NOW = datetime(2026, 10, 23, 9, tzinfo=CST)


def card(name='2026FUGA训练赛·杭州西山越野赛', url=BASE+'94612', **fields):
    return dict({'name': name, 'url': url, 'date': '2026-11-15', 'location': '浙江・杭州', 'status': 'open', 'text': name}, **fields)


def listing(name='2026FUGA训练赛·杭州西山越野赛', url=BASE+'94612'):
    return f'''<div class="reg-race-block"><a class="race-name-a" href="{url}">{name}<span class="label">报名中</span></a>
    <p>登杭城之巅，逐西山征途！从2026年10月1日延期</p><div><a class="race-register-a">一键报名</a>
    <p>2026年11月15日星期日<br>浙江・杭州</p></div></div>'''


def search(keyword, content='', next_url=''):
    return f'''<div id="angReg_frontpage"><form><input name="q" value="{keyword.replace(chr(39), '&#39;')}"></form>
    {content or '抱歉，暂时没有相关赛事可以报名'}{('<a href="'+next_url+'">下一页</a>') if next_url else ''}</div>'''


def detail(name='2026FUGA训练赛·杭州西山越野赛', text='', date='2026年11月15日'):
    return f'''<nav>赞助商：始祖鸟；赞助商：凯乐石</nav><div id="angReg_viewRace">
    <h4>{name}<span>报名中</span></h4><p>赛事日期：{date}</p>
    <p>赛事地点：浙江・杭州</p><p>报名截止时间：2026年11月1日 23:59:59</p>
    <h5>龙坞35K</h5><p>{text}</p></div>'''


class Client:
    def __init__(self, pages):
        self.pages, self.calls = pages, []
    def get(self, url):
        self.calls.append(url)
        return self.pages(url) if callable(self.pages) else self.pages[url]


def reader(reports):
    def read(name, fn):
        try:
            value = fn()
            reports[name] = {'ok': True}
            return value
        except Exception as error:
            reports[name] = {'ok': False, 'error': str(error)}
    return read


class ZuicoolBrandTests(unittest.TestCase):
    def test_promotional_copy_and_postponement_do_not_replace_event_date(self):
        found = zui_cards(listing())[0]
        self.assertEqual(found['date'], '2026-11-15')
        self.assertEqual(found['location'], '浙江・杭州')

    def test_real_empty_search_is_success_but_login_or_missing_structure_is_failure(self):
        self.assertEqual(zui_cards(search('始祖鸟'), allow_empty=True), [])
        for html in ['请登录', '<html>抱歉，暂时没有相关赛事可以报名</html>']:
            with self.assertRaises(SourceError):
                zui_cards(html, allow_empty=True)

    def test_keywords_and_homepage_merge_before_reading_details(self):
        client = Client(lambda url: search(parse_qs(urlsplit(url).query)['q'][0], listing() if 'FUGA' in url else ''))
        reports = {}
        found, stats = discover(client, [card(url=BASE+'94612?campaign=home')], NOW, reader(reports), zui_cards)
        self.assertEqual([x['url'] for x in found], [BASE+'94612'])
        self.assertEqual(len(client.calls), len(KEYWORDS))
        self.assertEqual(stats['search_counts']['始祖鸟'], 0)
        self.assertTrue(all(v['ok'] for v in reports.values()))

    def test_related_sponsor_in_details_is_checked_without_a_brand_in_the_title(self):
        source = card(name='2026杭州西山越野赛')
        client = Client(lambda url: search(parse_qs(urlsplit(url).query)['q'][0]))
        candidates, _ = discover(client, [source], NOW, reader({}), zui_cards)
        self.assertEqual(len(candidates), 1)
        item = inspect(Client({source['url']: detail(source['name'], '合作伙伴：始祖鸟')}), source)[0]
        self.assertTrue(item['verified'])
        self.assertEqual(item['brand_evidence_kind'], 'platform_relationship')

    def test_search_failure_preserves_homepage_candidates_and_reports_gap(self):
        reports = {}
        found, stats = discover(Client(lambda url: '<html>请登录</html>'), [card()], NOW, reader(reports), zui_cards)
        self.assertEqual(len(found), 1)
        self.assertFalse(stats['search_complete'])
        self.assertFalse(all(x['ok'] for x in reports.values()))

    def test_lowercase_echo_and_pagination_work_and_duplicate_pages_do_not_duplicate_details(self):
        def page(url):
            q = parse_qs(urlsplit(url).query)['q'][0]
            if q.casefold() == 'fuga':
                return search('fuga', listing(), 'https://reg.zuicool.com/?q=fuga&amp;page=2' if 'page=2' not in url else '')
            return search(q.casefold())
        reports = {}
        found, stats = discover(Client(page), [], NOW, reader(reports), zui_cards)
        self.assertTrue(stats['search_complete'])
        self.assertEqual(stats['search_counts']['FUGA'], 2)
        self.assertEqual(len(found), 1)

    def test_search_pagination_must_keep_origin_and_keyword(self):
        reports = {}
        def page(url):
            q = parse_qs(urlsplit(url).query)['q'][0]
            return search(q, listing(), 'https://untrusted.example/?q=FUGA') if q == 'FUGA' else search(q)
        _, stats = discover(Client(page), [], NOW, reader(reports), zui_cards)
        self.assertFalse(stats['search_complete'])
        self.assertFalse(reports['brand:zuicool:search:FUGA:1']['ok'])

    def test_fuga_training_and_explicit_brand_titles_are_labeled_as_platform_evidence(self):
        for name in ['2026FUGA训练赛·杭州西山越野赛', '2026凯乐石FUGA训练赛-南京站（11月）', '2026始祖鸟攀岩公开课']:
            source = card(name=name)
            item = inspect(Client({source['url']: detail(name)}), source)[0]
            self.assertTrue(item['verified'])
            self.assertIn('平台信息', item['brand'])
            self.assertIn('未独立核实', item['brand'])
            self.assertIn('来源：最酷', item_text(item))

    def test_navigation_and_recommendations_do_not_supply_brand_proof(self):
        source = card(name='2026杭州西山越野赛')
        html = detail(source['name']).replace('</div>', '<div class="recommendations">赞助商：始祖鸟</div></div>')
        self.assertEqual(inspect(Client({source['url']: html}), source), [])

    def test_unrelated_event_without_year_in_title_is_not_a_brand_failure(self):
        source = card(name='首届金华山越野赛')
        self.assertEqual(inspect(Client({source['url']: detail(source['name'])}), source), [])

    def test_gear_mentions_are_held_for_review_not_claimed_as_a_cooperation(self):
        source = card(name='2026杭州西山越野赛')
        item = inspect(Client({source['url']: detail(source['name'], '建议穿着凯乐石鞋；越野路线35公里')}), source)[0]
        self.assertFalse(item['verified'])
        self.assertIn('待核验', item['reason'])

    def test_stale_card_cannot_supply_registration_or_current_event_identity(self):
        source = card()
        with self.assertRaises(ValueError):
            inspect(Client({source['url']: detail('2027FUGA训练赛·杭州西山越野赛', date='2027年11月15日')}), source)

    def test_outside_region_and_expired_lists_do_not_fetch_details(self):
        client = Client(lambda url: search(parse_qs(urlsplit(url).query)['q'][0]))
        found, _ = discover(client, [card(location='广东・东莞'), card(date='2026-01-01'), card(status='closed')], NOW, reader({}), zui_cards)
        self.assertEqual(found, [])


class DedupTests(unittest.TestCase):
    def event(self, **fields):
        return dict({'id': 'brand:zui:94612:2026', 'name': '2026凯乐石莫干山跑山赛', 'category': 'brand', 'edition': '2026',
                     'date': '2026-11-15', 'location': '浙江・湖州', 'status': 'open', 'verified': True, 'in_region': True,
                     'official': BASE+'94612', 'registration': BASE+'94612', 'priority': 2}, **fields)

    def test_shared_entry_links_merge_tracking_queries_and_categories(self):
        branded = self.event()
        marathon = self.event(id='caa:123:2026', category='caa', province='浙江', official='https://zuicool.com/event/9', registration=BASE+'94612?campaign=event')
        state = {}
        fresh = merge_discoveries(state, [branded, marathon], NOW)
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0]['category'], 'caa')
        self.assertEqual(merge_discoveries(state, [self.event(id='alias', official=BASE+'en/94612')], NOW), [])

    def test_letour_and_platform_repost_notify_once_then_remain_known(self):
        official = self.event(id='brand:letour:mogan:2026', name='2026 KAILAS FUGA 莫干山跑山赛', official='https://www.letoursports.net/10th-mogan', registration='https://www.letoursports.net/10th-mogan', priority=1)
        state = {}
        self.assertEqual(len(merge_discoveries(state, [self.event(), official], NOW)), 1)
        self.assertEqual(len(state['known']), 1)
        self.assertEqual(merge_discoveries(state, [self.event(id='another', official=BASE+'94612?campaign=search')], NOW + timedelta(days=1)), [])
        self.assertIn(BASE+'94612', next(iter(state['known'].values()))['identity_links'])

    def test_distinct_dates_brands_and_provinces_stay_separate(self):
        first = self.event()
        for second in [self.event(id='second', date='2026-12-15', official='https://example.com/two', registration=''),
                       self.event(id='second', name='2026始祖鸟莫干山跑山赛', official='https://example.com/two', registration=''),
                       self.event(id='second', location='江苏・南京', official='https://example.com/two', registration='')]:
            self.assertEqual(len(merge_discoveries({}, [first, second], NOW)), 2)

    def test_unverified_or_closed_first_discovery_does_not_consume_a_later_notification(self):
        for fields in [{'verified': False}, {'status': 'closed'}, {'status': 'unknown'}]:
            state = {}
            self.assertEqual(merge_discoveries(state, [self.event(**fields)], NOW), [])
            self.assertEqual(len(merge_discoveries(state, [self.event()], NOW)), 1)

    def test_semantic_event_query_ids_are_preserved_and_generic_signup_portals_do_not_merge(self):
        first = self.event(official='https://organizer.example/event?id=1', registration='https://signup.example/register')
        second = self.event(id='second', name='2026凯乐石南京越野挑战赛', official='https://organizer.example/event?id=2', registration='https://signup.example/register')
        self.assertEqual(len(merge_discoveries({}, [first, second], NOW)), 2)
        alias = dict(first, id='alias', official='https://organizer.example/event?utm_source=zuicool&id=1')
        self.assertEqual(len(merge_discoveries({}, [first, alias], NOW)), 1)


if __name__ == '__main__':
    unittest.main()
