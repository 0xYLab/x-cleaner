import unittest
from unittest.mock import AsyncMock, patch
import cookie_cleaner as c


def page(cursor=None, users=()):
    entries = [{'content': {'itemContent': {'user_results': {'result': {'rest_id': str(uid), 'legacy': {'following': True, **fields}}}}}} for uid, fields in users]
    if cursor: entries.append({'content': {'cursorType': 'Bottom', 'value': cursor}})
    return {'data': {'instructions': [{'entries': entries}]}}


class RegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_replayed_page_restarts_before_oldest_id(self):
        api = c.CookieAPI(c.Credentials('a' * 40, 'b' * 64))
        api.owner_id, api.handle = '1', 'Example'
        def tweets(uid, cursor):
            data = page(cursor)
            data['data']['instructions'][0]['entries'].insert(0, {'content': {'itemContent': {
                'tweet_results': {'result': {'rest_id': uid, 'legacy': {'user_id_str': '1'}}}
            }}})
            return data
        api.call = AsyncMock(side_effect=[tweets('100', 'a'), tweets('100', 'b'), tweets('50', None)])
        try:
            with patch.object(c.asyncio, 'sleep', new=AsyncMock()):
                found = await api.search_own_posts(2)
            self.assertEqual([x.id for x in found['posts']], ['100', '50'])
            self.assertEqual(api.call.await_args_list[2].args[2], 'from:Example max_id:99')
            self.assertIsNone(api.call.await_args_list[2].args[-1])
        finally: await api.close()

    async def test_main_timeline_reply_survives_replies_endpoint_failure(self):
        api = c.CookieAPI(c.Credentials('a' * 40, 'b' * 64))
        api.owner_id, api.endpoints_checked = '1', True
        payload = page()
        payload['data']['instructions'][0]['entries'].append({'content': {'itemContent': {
            'tweet_results': {'result': {'rest_id': '30', 'legacy': {
                'user_id_str': '1', 'in_reply_to_status_id_str': '20'
            }}}
        }}})
        api.call = AsyncMock(side_effect=[payload, c.EndpointChanged('404'), page(), page()])
        api.search_own_posts = AsyncMock(return_value={'posts': [], 'replies': [], 'reposts': []})
        try:
            found = await api.scan()
            self.assertEqual([x.id for x in found['replies']], ['30'])
        finally: await api.close()

    async def test_fallback_preserves_main_reposts_and_failure(self):
        api = c.CookieAPI(c.Credentials('a' * 40, 'b' * 64))
        api.owner_id, api.handle, api.endpoints_checked = '1', 'Example', True
        original = {'data': {'instructions': [{'entries': [{'content': {'itemContent': {
            'tweet_results': {'result': {'rest_id': '20', 'legacy': {
                'user_id_str': '1', 'retweeted_status_result': {'result': {'rest_id': '10'}}
            }}}
        }}}]}]}}
        api.call = AsyncMock(side_effect=[original, c.EndpointChanged('404'), page(), page()])
        api.search_own_posts = AsyncMock(return_value={
            'posts': [], 'replies': [c.Item('replies', '30', '1')], 'reposts': []
        })
        try:
            records = await api.scan()
            self.assertEqual([x.id for x in records['reposts']], ['10'])
            self.assertEqual([x.id for x in records['replies']], ['30'])
            self.assertIn('replies', api.scan_failures)
        finally: await api.close()

    async def test_search_uses_current_features_and_post_fallback(self):
        api = c.CookieAPI(c.Credentials('a' * 40, 'b' * 64))
        api.client.gql.gql_get = AsyncMock(side_effect=c.EndpointChanged('404'))
        expected = ({'data': {}}, object())
        api.client.gql.gql_post = AsyncMock(return_value=expected)
        try:
            result = await api.search_timeline_current('from:Example', 'Latest', 50, None)
            self.assertIs(result, expected)
            variables = api.client.gql.gql_post.await_args.args[1]
            features = api.client.gql.gql_post.await_args.args[2]
            self.assertTrue(variables['withGrokTranslatedBio'])
            self.assertEqual(features, c.SEARCH_TIMELINE_FEATURES)
        finally: await api.close()

    async def test_empty_profile_timeline_uses_verified_search_fallback(self):
        api = c.CookieAPI(c.Credentials('a' * 40, 'b' * 64))
        api.owner_id, api.handle = '1', 'Example'
        api.endpoints_checked, api.batch_size = True, 50
        # Profile counters can themselves be temporarily unavailable. That
        # must not suppress the independent search fallback.
        api.profile_counts = {'posts': None, 'likes': None, 'following': None}
        api.call = AsyncMock(side_effect=[page(), page(), page(), page()])
        fallback_item = c.Item('posts', '10', '1')
        api.search_own_posts = AsyncMock(return_value={
            'posts': [fallback_item], 'replies': [], 'reposts': []
        })
        try:
            records = await api.scan()
            self.assertEqual(records['posts'], [fallback_item])
            api.search_own_posts.assert_awaited_once_with(50)
        finally: await api.close()

    def test_nested_receipt_requires_matching_id_and_false(self):
        receipt = {'data': {'user': {'rest_id': '2', 'relationship_perspectives': {'following': False}}}}
        self.assertTrue(c.validate_receipt('following', '2', receipt))
        self.assertFalse(c.validate_receipt('following', '3', receipt))
        receipt['data']['user']['following'] = True
        self.assertFalse(c.validate_receipt('following', '2', receipt))

    async def test_submitted_but_unverified_is_pending_not_failed_or_retried(self):
        api = c.CookieAPI(c.Credentials('a' * 40, 'b' * 64))
        api.owner_id = '1'
        api.call = AsyncMock(return_value={})
        api.following_relationship = AsyncMock(side_effect=c.SkipTarget('not found'))
        try:
            with self.assertRaises(c.PendingAction):
                await api.remove(c.Item('following', '2', '1', False))
            api.call.assert_awaited_once()
        finally: await api.close()

    async def test_recheck_uses_scan_batch_size_and_missing_is_unknown(self):
        api = c.CookieAPI(c.Credentials('a' * 40, 'b' * 64))
        api.owner_id, api.batch_size = '1', 50
        api.call = AsyncMock(return_value=page())
        try:
            with self.assertRaises(c.SkipTarget):
                await api.following_relationship('2')
            self.assertEqual(api.call.call_args.args[-2], 50)
        finally: await api.close()

    async def test_nonmutual_uses_following_relation(self):
        api = c.CookieAPI(c.Credentials('a' * 40, 'b' * 64))
        api.owner_id = '1'
        api.call = AsyncMock(side_effect=c.EndpointChanged('404'))
        api.following_relationship = AsyncMock(return_value={'following': True, 'followed_by': False})
        api.remove = AsyncMock()
        try:
            self.assertTrue(await api.unfollow_if_nonmutual(c.Item('following', '2', '1', False)))
            api.following_relationship.assert_not_awaited()
            api.remove.assert_awaited_once()
        finally: await api.close()

    async def test_ambiguous_unfollow_receipt_waits_for_batch_rescan(self):
        api = c.CookieAPI(c.Credentials('a' * 40, 'b' * 64))
        api.owner_id = '1'
        api.call = AsyncMock(return_value={})
        api.following_relationship = AsyncMock(return_value={'following': False})
        try:
            with self.assertRaises(c.PendingAction):
                await api.remove(c.Item('following', '2', '1', False))
            api.call.assert_awaited_once()
            api.following_relationship.assert_not_awaited()
        finally: await api.close()

    async def test_unfollow_404_uses_api_host_fallback(self):
        api = c.CookieAPI(c.Credentials('a' * 40, 'b' * 64))
        api.owner_id = '1'
        receipt = {'id_str': '2', 'following': False}
        api.call = AsyncMock(side_effect=[c.EndpointChanged('404'), receipt])
        try:
            await api.remove(c.Item('following', '2', '1', False))
            self.assertEqual(api.call.await_count, 2)
            self.assertIn('备用接口', api.call.await_args_list[1].args[0])
        finally: await api.close()

    async def test_batch_stops_at_fifty_even_if_server_returns_more(self):
        api = c.CookieAPI(c.Credentials('a' * 40, 'b' * 64))
        api.owner_id, api.endpoints_checked, api.batch_size = '1', True, 50
        api.profile_counts['posts'] = 0
        api.call = AsyncMock(side_effect=[page(), page(), page(), page('more', users=[(str(i + 2), {'followed_by': False}) for i in range(80)])])
        try:
            records = await api.scan()
            self.assertEqual(len(records['following']), 50)
            self.assertEqual(api.call.await_count, 4)
        finally: await api.close()

    async def test_repeat_cursor_retains_results_and_scans_other_categories(self):
        api = c.CookieAPI(c.Credentials('a' * 40, 'b' * 64))
        api.owner_id, api.endpoints_checked = '1', True
        api.profile_counts['posts'] = 0
        api.call = AsyncMock(side_effect=[page('repeat'), page('repeat'), page(), page(), page(users=[('2', {'followed_by': False}), ('3', {'followed_by': True})])])
        try:
            records = await api.scan()
            self.assertEqual(api.call.await_count, 5)
            self.assertEqual([x.follows_me for x in records['following']], [False, True])
            self.assertTrue(api.scan_notes)
        finally: await api.close()

    async def test_unknown_or_mutual_cannot_be_unfollowed(self):
        api = c.CookieAPI(c.Credentials('a' * 40, 'b' * 64))
        api.owner_id = '1'
        api.call = AsyncMock()
        try:
            for state in (None, True):
                with self.assertRaises(c.CleanerError):
                    await api.unfollow_if_nonmutual(c.Item('following', '2', '1', state))
            api.call.assert_not_awaited()
        finally: await api.close()

    async def test_scanned_nonmutual_does_not_repeat_full_list_read(self):
        api = c.CookieAPI(c.Credentials('a' * 40, 'b' * 64))
        api.owner_id = '1'
        api.call = AsyncMock(return_value=page(users=[('2', {'followed_by': True})]))
        api.remove = AsyncMock()
        try:
            self.assertTrue(await api.unfollow_if_nonmutual(c.Item('following', '2', '1', False)))
            api.remove.assert_awaited_once()
            api.call.assert_not_awaited()
        finally: await api.close()

    async def test_confirmed_nonmutual_is_removed(self):
        api = c.CookieAPI(c.Credentials('a' * 40, 'b' * 64))
        api.owner_id = '1'
        api.call = AsyncMock(return_value=page(users=[('2', {'followed_by': False})]))
        api.remove = AsyncMock()
        try:
            self.assertTrue(await api.unfollow_if_nonmutual(c.Item('following', '2', '1', False)))
            api.remove.assert_awaited_once()
        finally: await api.close()
