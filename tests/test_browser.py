import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import TimeoutError as BrowserTimeout

from race_monitor.browser import hk_browser_pages, BrowserReadError
from race_monitor.sources import hk_items, SourceError


HOME = '<h2>2027 Events</h2><p>21 January 2027</p>'
ENTRY = '<h2>HONG KONG 100 ENTRY PROCEDURES</h2><h3>Charity Places</h3>'


def page(url, html, navigation_error=None, content_error=None, status=200, title="Hong Kong 100"):
    value = Mock()
    value.url = url
    value.goto.side_effect = navigation_error
    value.goto.return_value.status = status
    value.locator.return_value.filter.return_value.wait_for.side_effect = content_error
    value.content.return_value = html
    value.title.return_value = title
    return value


class BrowserFallbackTests(unittest.TestCase):
    def run_pages(self, pages):
        playwright = Mock()
        browser = playwright.chromium.launch.return_value
        context = browser.new_context.return_value
        context.new_page.side_effect = pages
        manager = Mock()
        manager.__enter__ = Mock(return_value=playwright)
        manager.__exit__ = Mock(return_value=False)
        return browser, context, patch('playwright.sync_api.sync_playwright', return_value=manager)

    def test_navigation_timeout_retries_only_the_failed_page_and_uses_fresh_live_content(self):
        first = page('https://hk100ultra.com/', '', navigation_error=BrowserTimeout('private browser details'))
        home = page('https://hk100ultra.com/', HOME)
        entry = page('https://hk100ultra.com/entry/', ENTRY)
        browser, context, manager = self.run_pages([first, home, entry])
        with manager, patch('race_monitor.browser.time.sleep') as sleep:
            self.assertEqual(hk_browser_pages(), [HOME, ENTRY])
        self.assertEqual(context.new_page.call_count, 3)
        sleep.assert_called_once_with(2)
        self.assertEqual(home.goto.call_args.kwargs['wait_until'], 'commit')
        for value in [first, home, entry]: value.close.assert_called_once()
        context.close.assert_called_once()
        browser.close.assert_called_once()

    def test_entry_content_timeout_can_recover_without_refetching_the_successful_homepage(self):
        home = page('https://hk100ultra.com/', HOME)
        first = page('https://hk100ultra.com/entry/', '', content_error=BrowserTimeout('timeout'))
        entry = page('https://hk100ultra.com/entry/', ENTRY)
        browser, context, manager = self.run_pages([home, first, entry])
        with manager, patch('race_monitor.browser.time.sleep'):
            self.assertEqual(hk_browser_pages(), [HOME, ENTRY])
        home.goto.assert_called_once()
        self.assertEqual(context.new_page.call_count, 3)

    def test_persistent_access_check_is_an_error_with_page_and_http_status(self):
        home = page('https://hk100ultra.com/', HOME)
        blocked = [page('https://hk100ultra.com/entry/', '', content_error=BrowserTimeout('private details'),
                        status=403, title='Just a moment...') for _ in range(2)]
        browser, context, manager = self.run_pages([home, *blocked])
        with manager, patch('race_monitor.browser.time.sleep'):
            with self.assertRaisesRegex(BrowserReadError, r'报名页：HTTP 403，访问校验页未恢复（已尝试 2 次）') as caught:
                hk_browser_pages()
        self.assertNotIn('private details', str(caught.exception))
        self.assertEqual(context.new_page.call_count, 3)
        context.close.assert_called_once()
        browser.close.assert_called_once()

    def test_unverified_redirect_is_rejected_without_retry_or_reading_html(self):
        redirected = page('https://other.example/', HOME)
        browser, context, manager = self.run_pages([redirected])
        with manager, self.assertRaisesRegex(BrowserReadError, '首页：跳转到未核验域名'):
            hk_browser_pages()
        redirected.content.assert_not_called()
        self.assertEqual(context.new_page.call_count, 1)
        context.close.assert_called_once()
        browser.close.assert_called_once()

    def test_source_error_keeps_safe_browser_diagnostics_and_does_not_return_old_data(self):
        client = Mock()
        client.get.side_effect = SourceError('HTTP 403')
        with patch('race_monitor.sources.hk_browser_pages', side_effect=BrowserReadError('首页：正文加载超时或网络失败（已尝试 2 次）')):
            with self.assertRaisesRegex(SourceError, '首页：正文加载超时或网络失败'):
                hk_items(client)
