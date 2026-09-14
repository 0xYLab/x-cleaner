import unittest
from unittest.mock import AsyncMock, patch
import httpx
import json
import cookie_cleaner as c


class SafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_home_identity_uses_session_user_not_feed_author(self):
        state = {'session': {'user_id': '123'}, 'entities': {'users': {'entities': {'123': {'screen_name': 'Me'}, '456': {'screen_name': 'Other'}}}}}
        self.assertEqual(c.home_identity('window.__INITIAL_STATE__=' + json.dumps(state) + ';'), {'id_str': '123', 'screen_name': 'Me'})
        state['session'] = {}
        self.assertIsNone(c.home_identity('window.__INITIAL_STATE__=' + json.dumps(state)))
        self.assertIsNone(c.home_identity('<html>Home</html>'))

    async def test_home_session_identifies_without_old_endpoints(self):
        api = c.CookieAPI(c.Credentials('a' * 40, 'b' * 64))
        original = api.client.http
        state = {'session': {'user_id': '123'}, 'entities': {'users': {'entities': {'123': {'screen_name': 'Me'}}}}}
        def respond(request):
            self.assertEqual(str(request.url), 'https://x.com/home')
            return httpx.Response(200, text='window.__INITIAL_STATE__=' + json.dumps(state))
        api.client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        api.client.set_cookies({'auth_token': 'a' * 40, 'ct0': 'b' * 64})
        try:
            self.assertEqual(await api.identify(), 'Me')
        finally:
            await original.aclose()
            await api.close()

    async def test_identity_returns_server_handle_without_html_bootstrap(self):
        api = c.CookieAPI(c.Credentials('a' * 40, 'b' * 64))
        original = api.client.http
        async def respond(request):
            if request.url.path == '/home':
                return httpx.Response(200, text='<html></html>')
            self.assertEqual(request.url.path, '/i/api/1.1/account/verify_credentials.json')
            self.assertIn('auth_token=', request.headers['cookie'])
            self.assertEqual(request.headers['x-csrf-token'], 'b' * 64)
            return httpx.Response(200, json={'id_str': '123', 'screen_name': 'RealAccount'})
        api.client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        api.client.set_cookies({'auth_token': 'a' * 40, 'ct0': 'b' * 64})
        api.client.client_transaction.init = AsyncMock(side_effect=AssertionError('must not bootstrap'))
        try:
            self.assertEqual(await api.identify(), 'RealAccount')
            self.assertEqual(api.owner_id, '123')
            api.client.client_transaction.init.assert_not_awaited()
        finally:
            await original.aclose()
            await api.close()

    async def test_graphql_request_adds_required_transaction_id_once(self):
        api = c.CookieAPI(c.Credentials('a' * 40, 'b' * 64))
        original = api.client.http
        seen = []
        def respond(request):
            seen.append(request)
            return httpx.Response(200, json={'data': {}})
        api.client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        api.client.set_cookies({'auth_token': 'a' * 40, 'ct0': 'b' * 64})
        api.client.client_transaction.home_page_response = '<html></html>'
        api.client.client_transaction.generate_transaction_id = lambda **kwargs: 'transaction-123'
        try:
            await api.client.request('GET', 'https://x.com/i/api/graphql/query/Example')
            self.assertEqual(seen[0].headers['x-client-transaction-id'], 'transaction-123')
        finally:
            await original.aclose()
            await api.close()

    async def test_rejected_session_is_not_reported_as_login_success(self):
        api = c.CookieAPI(c.Credentials('a' * 40, 'b' * 64))
        original = api.client.http
        api.client.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(401, json={'errors': []})))
        api.client.set_cookies({'auth_token': 'a' * 40, 'ct0': 'b' * 64})
        try:
            with self.assertRaises(c.AuthenticationError): await api.identify()
            self.assertIsNone(api.owner_id)
        finally:
            await original.aclose()
            await api.close()

    async def test_credentials_are_scoped_and_cleared(self):
        api = c.CookieAPI(c.Credentials('a' * 40, 'b' * 64))
        try:
            api.client._remove_duplicate_ct0_cookie()
            self.assertTrue(all(x.domain == '.x.com' for x in api.client.http.cookies.jar))
            foreign = api.client.http.build_request('GET', 'https://example.com/')
            self.assertNotIn('cookie', foreign.headers)
            with self.assertRaises(c.CleanerError):
                await c.guard(httpx.Request('GET', 'https://example.com/', headers={'cookie': 'secret'}))
        finally:
            await api.close()
        self.assertEqual(len(api.client.http.cookies), 0)

    async def test_unverified_account_cannot_write(self):
        api = c.CookieAPI(c.Credentials('a' * 40, 'b' * 64))
        api.call = AsyncMock()
        try:
            with self.assertRaises(c.AuthenticationError):
                await api.remove(c.Item('posts', '1', '2'))
            api.call.assert_not_awaited()
        finally:
            await api.close()

    async def test_scan_select_clean_rescan_return(self):
        fake = AsyncMock()
        fake.identify.return_value = 'Example'
        before = {k: [] for k in c.LABELS}
        before['posts'] = [c.Item('posts', '10', '1')]
        fake.scan.side_effect = [before, {k: [] for k in c.LABELS}]
        fake.scan_notes = []
        fake.account_totals.return_value = {'posts': 1, 'likes': 0, 'following': 0}
        with patch.object(c, 'CookieAPI', return_value=fake), patch.object(c.getpass, 'getpass', return_value='a' * 40), patch.object(c, 'system_proxy', return_value=None), patch('builtins.input', side_effect=['yes', '50', '1', '1', '0']), patch.object(c, 'confirm_cleanup', return_value=True), patch.object(c.asyncio, 'sleep', new=AsyncMock()):
            await c.run_cookie_cleaner()
        fake.remove.assert_awaited_once()
        self.assertEqual(fake.scan.await_count, 2)
        fake.close.assert_awaited_once()

    async def test_one_changed_cleanup_endpoint_does_not_cancel_other_categories(self):
        fake = AsyncMock()
        fake.identify.return_value = 'Example'
        before = {k: [] for k in c.LABELS}
        before['posts'] = [c.Item('posts', '10', '1')]
        before['likes'] = [c.Item('likes', '11', '1')]
        fake.scan.side_effect = [before, {k: [] for k in c.LABELS}]
        fake.scan_notes = []
        fake.account_totals.return_value = {'posts': 1, 'likes': 1, 'following': 0}

        async def remove(item):
            if item.kind == 'posts':
                raise c.EndpointChanged('HTTP 404')

        fake.remove.side_effect = remove
        with patch.object(c, 'CookieAPI', return_value=fake), patch.object(c.getpass, 'getpass', return_value='a' * 40), patch.object(c, 'system_proxy', return_value=None), patch('builtins.input', side_effect=['yes', '50', 'all', '1', '1', '0']), patch.object(c, 'confirm_cleanup', return_value=True), patch.object(c.asyncio, 'sleep', new=AsyncMock()), patch('sys.stdout'):
            await c.run_cookie_cleaner()

        self.assertEqual(fake.remove.await_count, 2)
        self.assertEqual(fake.remove.await_args_list[-1].args[0].kind, 'likes')
        self.assertEqual(fake.scan.await_count, 2)

    async def test_wrong_account_stops_before_scan(self):
        fake = AsyncMock()
        fake.identify.return_value = 'Example'
        with patch.object(c, 'CookieAPI', return_value=fake), patch.object(c.getpass, 'getpass', side_effect=['a' * 40, 'q']), patch.object(c, 'system_proxy', return_value=None), patch('builtins.input', return_value='no'):
            await c.run_cookie_cleaner()
        fake.scan.assert_not_awaited()
        fake.remove.assert_not_awaited()
        fake.close.assert_awaited_once()
