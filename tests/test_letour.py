import unittest
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from race_monitor.core import CST, merge_discoveries
from race_monitor.letour import BASE, collect, own_url, parse_detail, parse_list

NOW = datetime(2026, 10, 23, 9, tzinfo=CST)


def list_html(name='2027凯乐石莫干山跑山赛', state='报名中', location='浙江 湖州', next_link=''):
    return f'''<h1 class="sas-name">朗途体育赛事官网</h1><div class="outline"><a class="title" href="/event?id=123&amp;mid=99">{name}</a>
    <div class="state">{state}</div><div class="data"><i class="icon-flag"></i><span>{location}</span></div></div>{next_link}'''


def detail(name='2027凯乐石莫干山跑山赛', action='立即报名', disabled='', year=2027, body=None):
    start=int(datetime(year, 11, 1, 8, tzinfo=CST).timestamp()*1000)
    end=int(datetime(year, 11, 2, 8, tzinfo=CST).timestamp()*1000)
    return f'''<nav>2027凯乐石广州100越野赛；赛事地点：浙江</nav><h2><span id="SocialShareTitle">{name}</span></h2>
    <div id="mainPane"><button class="join" {disabled}>{action}</button></div>
    <div id="contentBox">{body if body is not None else '赛事地点：浙江省湖州市德清县莫干山镇庾村'}</div>
    <script>var batches=[{{id:2,title:'30KM',valid:true,startTime:{start},endTime:{end},price:580}}];</script>'''


class Client:
    def __init__(self, pages):self.pages,self.calls=pages,[]
    def get(self, url):
        self.calls.append(url)
        return self.pages(url) if callable(self.pages) else self.pages[url]


def reader(reports):
    def read(name, fn):
        try:
            value=fn();reports[name]={'ok':True};return value
        except Exception as e:reports[name]={'ok':False,'error':str(e)}
    return read


class LetourChineseTests(unittest.TestCase):
    def test_only_chinese_official_event_and_category_urls_are_used(self):
        self.assertEqual(own_url('/m/event?id=123&mid=456', '/event', 'id'), BASE+'event?id=123')
        for url in ['https://untrusted.example/event?id=123','/event?id=123&id=456','/event?id={{o.id}}','/event?id=abc','/article?id=123']:
            self.assertEqual(own_url(url, '/event', 'id'), '')

    def test_list_reads_region_and_closed_status_from_own_card(self):
        found,_=parse_list(list_html(),BASE+'events?mid=1');self.assertEqual(len(found),1)
        for html in [list_html(state='活动进行中'),list_html(location='广东 广州'),list_html(name='莫干山纪念勋章'),list_html(name='【赛后参赛包邮寄申请】2027凯乐石莫干山跑山赛'),list_html(name='【品牌展位押金】2027凯乐石莫干山跑山赛')]:
            self.assertEqual(parse_list(html,BASE+'events?mid=1')[0],[])
        with self.assertRaises(ValueError):parse_list('<h1 class="sas-name">朗途</h1>请登录',BASE+'events?mid=1')

    def test_alternative_list_layout_and_direct_event_category(self):
        html=list_html().replace('class="outline"','class="event-data"').replace('<div class="data"><i class="icon-flag"></i><span>浙江 湖州</span></div>','<div><i class="icon-flag"></i> 浙江 湖州</div>')
        html=html.replace('<div class="event-data">','<ul class="events"><li><div class="event-data">').replace('</div><div class="data">','</div><div class="data">')+'</li></ul>'
        self.assertEqual(len(parse_list(html,BASE+'events?mid=1')[0]),1)
        direct='<h1 class="sas-name">朗途体育赛事官网</h1>'+detail()+'<script>var eventId=123;</script>'
        self.assertEqual(parse_list(direct,BASE+'events?mid=1')[0][0]['url'],BASE+'event?id=123')
        with self.assertRaises(ValueError):
            parse_list(direct.replace('var eventId=123;', ''),BASE+'events?mid=1')

    def test_chinese_date_groups_price_and_registration_state_are_not_templates(self):
        item=parse_detail(detail(),BASE+'event?id=123',NOW)[0]
        self.assertEqual(item['date'],'2027-11-01');self.assertEqual(item['date_end'],'2027-11-02')
        self.assertEqual(item['groups'],'30KM');self.assertEqual(item['fee'],'580元')
        self.assertTrue(item['verified']);self.assertEqual(item['status'],'open')
        for status in ['报名已截止','活动进行中','活动已结束']:
            item=parse_detail(detail(action=status,disabled='disabled'),BASE+'event?id=123',NOW)[0]
            self.assertEqual(item['status'],'closed')
            self.assertEqual(merge_discoveries({},[item],NOW),[])
        item=parse_detail(detail(disabled='disabled'),BASE+'event?id=123',NOW)[0]
        self.assertFalse(item['verified'])

    def test_navigation_cannot_supply_brand_or_location_and_javascript_is_not_executed(self):
        self.assertEqual(parse_detail(detail(name='2027莫干山越野赛'),BASE+'event?id=123',NOW),[])
        self.assertEqual(parse_detail(detail(body='赛事地点：广东广州'),BASE+'event?id=123',NOW),[])
        with self.assertRaises(ValueError):
            parse_detail(detail().replace('valid:true','valid:execute_untrusted()'),BASE+'event?id=123',NOW)
        with self.assertRaises(ValueError):
            parse_detail(detail(year=2026),BASE+'event?id=123',NOW)

    def test_categories_pagination_and_duplicate_entries_are_merged(self):
        pages={BASE:'<title>朗途体育赛事官网</title><a href="/events?mid=1">越野赛报名</a><a href="/events?mid=2">主题活动</a>',
               BASE+'events?mid=1':list_html(next_link='<div class="pagination"><a href="/events?mid=1&amp;page=2">下一页</a></div>'),
               BASE+'events?mid=1&page=2':list_html(), BASE+'events?mid=2':list_html(), BASE+'event?id=123':detail()}
        client=Client(pages);reports={};items=collect(client,NOW,reader(reports))
        self.assertEqual(len(items),1);self.assertTrue(all(s['ok'] for s in reports.values()))
        self.assertEqual(client.calls.count(BASE+'event?id=123'),1)
        pages[BASE+'events?mid=2']='<html>请登录</html>'
        reports={};self.assertEqual(len(collect(Client(pages),NOW,reader(reports))),1)
        self.assertTrue(any(not s['ok'] for s in reports.values()))
        with self.assertRaises(ValueError):
            parse_list(list_html(next_link='<div class="pagination"><a href="https://untrusted.example/events?mid=1">下一页</a></div>'),BASE+'events?mid=1')

    def test_zuicool_repost_deduplicates_with_chinese_site(self):
        official=parse_detail(detail(),BASE+'event?id=123',NOW)[0]
        repost=dict(official,id='brand:zui:99:2027',official='https://reg.zuicool.com/99',registration='https://reg.zuicool.com/99',priority=2)
        self.assertEqual(len(merge_discoveries({},[official,repost],NOW)),1)


if __name__=='__main__':unittest.main()
