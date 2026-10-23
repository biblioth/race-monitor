import unittest
from unittest.mock import Mock, patch
from race_monitor.service import notify_failure


class FailureNotificationTests(unittest.TestCase):
    def environment(self):
        return {'FEISHU_WEBHOOK_URL': 'https://example.com/old', 'FEISHU_WEBHOOK_SECRET': 'old-secret',
                'FEISHU_ALERTS_WEBHOOK_URL': 'https://example.com/new', 'FEISHU_ALERTS_WEBHOOK_SECRET': 'new-secret'}

    def test_fallback_uses_both_robots_and_their_own_signatures(self):
        response = Mock();response.json.return_value={'code': 0}
        with patch.dict('os.environ', self.environment(), clear=True), patch('race_monitor.service.requests.post', return_value=response) as post:
            notify_failure()
        self.assertEqual([c.args[0] for c in post.call_args_list], ['https://example.com/old', 'https://example.com/new'])
        self.assertNotEqual(post.call_args_list[0].kwargs['json']['sign'], post.call_args_list[1].kwargs['json']['sign'])

    def test_one_failed_robot_does_not_prevent_delivery_to_the_other(self):
        response = Mock();response.json.return_value={'code': 0}
        def send(url, **kwargs):
            if url.endswith('/old'):raise RuntimeError('fake business error')
            return response
        with patch.dict('os.environ', self.environment(), clear=True), patch('race_monitor.service.requests.post', side_effect=send) as post, patch('time.sleep'):
            with self.assertRaisesRegex(RuntimeError, 'FEISHU'):
                notify_failure()
        self.assertEqual([c.args[0] for c in post.call_args_list], ['https://example.com/old']*3+['https://example.com/new'])

    def test_duplicate_webhook_is_only_sent_once(self):
        env=self.environment();env['FEISHU_ALERTS_WEBHOOK_URL']=env['FEISHU_WEBHOOK_URL']
        response=Mock();response.json.return_value={'code': 0}
        with patch.dict('os.environ', env, clear=True), patch('race_monitor.service.requests.post', return_value=response) as post:
            notify_failure()
        self.assertEqual(post.call_count, 1)
