from __future__ import annotations

import contextlib
import io
from pathlib import Path
import sys
import unittest

import httpx


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def tweet(tweet_id="10", owner_id="1", **legacy):
    return {
        "__typename": "Tweet",
        "rest_id": tweet_id,
        "legacy": {"user_id_str": owner_id, **legacy},
    }


def timeline_cell(result):
    return {
        "itemContent": {
            "itemType": "TimelineTweet",
            "tweet_results": {"result": result},
        }
    }


def timeline_payload(*results, cursor=None):
    entries = [
        {"entryId": f"tweet-{index}", "content": timeline_cell(result)}
        for index, result in enumerate(results)
    ]
    if cursor is not None:
        entries.append(
            {
                "entryId": "cursor-bottom",
                "content": {"cursorType": "Bottom", "value": cursor},
            }
        )
    return {
        "data": {
            "user": {
                "result": {
                    "timeline": {
                        "timeline": {
                            "instructions": [
                                {"type": "TimelineAddEntries", "entries": entries}
                            ]
                        }
                    }
                }
            }
        }
    }


class CookieCleanerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Importing here lets test discovery produce a direct, useful failure if
        # the implementation module has not yet been placed beside main.py.
        global cleaner
        import cookie_cleaner as cleaner

    def test_bare_auth_token_and_cookie_header_are_parsed(self):
        auth = "a" * 40
        csrf = "b" * 64

        bare = cleaner.parse_cookies(auth)
        self.assertEqual(bare.auth_token, auth)
        self.assertIsNone(bare.ct0)

        pair = cleaner.parse_cookies(f"Cookie: auth_token={auth}; ct0={csrf}; lang=zh")
        self.assertEqual(pair.auth_token, auth)
        self.assertEqual(pair.ct0, csrf)

    def test_explicit_ct0_can_complete_bare_auth_token(self):
        auth = "a" * 40
        csrf = "b" * 64
        parsed = cleaner.parse_cookies(auth, csrf)
        self.assertEqual((parsed.auth_token, parsed.ct0), (auth, csrf))

    def test_cookie_parser_rejects_injection_missing_auth_and_conflicts(self):
        auth_a = "a" * 40
        auth_b = "b" * 40
        csrf = "c" * 64
        invalid = (
            f"auth_token={auth_a}\nct0={csrf}",
            f"ct0={csrf}",
            f"auth_token={auth_a}; auth_token={auth_b}; ct0={csrf}",
            "auth_token=",
        )
        for raw in invalid:
            with self.subTest(raw_length=len(raw)):
                with self.assertRaises(cleaner.CleanerError):
                    cleaner.parse_cookies(raw)

    def test_credentials_and_parser_errors_never_reveal_secrets(self):
        auth = "deadbeef" * 5
        csrf = "facefeed" * 8
        credentials = cleaner.parse_cookies(f"auth_token={auth}; ct0={csrf}")
        self.assertNotIn(auth, repr(credentials))
        self.assertNotIn(csrf, repr(credentials))

        conflicting = f"auth_token={auth}; auth_token={'1' * 40}; ct0={csrf}"
        try:
            cleaner.parse_cookies(conflicting)
        except cleaner.CleanerError as error:
            rendered = str(error)
        else:
            self.fail("conflicting credentials were accepted")
        self.assertNotIn(auth, rendered)
        self.assertNotIn(csrf, rendered)

    def test_posts_include_quotes_but_never_quoted_originals(self):
        outer = tweet("10", "1")
        outer["quoted_status_result"] = {"result": tweet("99", "2")}
        page = cleaner.parse_page(timeline_payload(outer, cursor="next"), "posts", "1")
        self.assertEqual([(item.kind, item.id, item.owner_id) for item in page.items],
                         [("posts", "10", "1")])
        self.assertEqual(page.cursor, "next")

    def test_current_core_author_shape_is_recognized(self):
        current = tweet("12", owner_id=None)
        current["legacy"].pop("user_id_str")
        current["core"] = {"user_results": {"result": {"rest_id": "1"}}}
        page = cleaner.parse_page(timeline_payload(current), "posts", "1")
        self.assertEqual([(item.kind, item.id) for item in page.items], [("posts", "12")])

    def test_anonymous_visibility_wrapper_is_recognized(self):
        wrapped = {"tweet": tweet("14", "1")}
        page = cleaner.parse_page(timeline_payload(wrapped), "posts", "1")
        self.assertEqual([(item.kind, item.id) for item in page.items], [("posts", "14")])

    def test_direct_tweet_results_entry_shape_is_recognized(self):
        payload = {
            "data": {"timeline": {"instructions": [{"entries": [{
                "content": {"tweet_results": {"result": tweet("13", "1")}}
            }]}]}}
        }
        page = cleaner.parse_page(payload, "posts", "1")
        self.assertEqual([(item.kind, item.id) for item in page.items], [("posts", "13")])

    def test_unknown_author_shape_is_not_misreported_as_zero(self):
        unknown = tweet("12", owner_id=None)
        unknown["legacy"].pop("user_id_str")
        with self.assertRaises(cleaner.EndpointChanged):
            cleaner.parse_page(timeline_payload(unknown), "posts", "1")

    def test_replies_reposts_and_foreign_context_are_classified_safely(self):
        reply = tweet("10", "1", in_reply_to_status_id_str="9")
        foreign = tweet("9", "2")
        replies = cleaner.parse_page(timeline_payload(foreign, reply), "replies", "1")
        self.assertEqual([(item.kind, item.id) for item in replies.items], [("replies", "10")])

        repost = tweet(
            "11",
            "1",
            retweeted_status_result={"result": tweet("90", "2")},
        )
        posts = cleaner.parse_page(timeline_payload(repost), "posts", "1")
        self.assertEqual([(item.kind, item.id) for item in posts.items], [("reposts", "90")])

    def test_likes_and_following_require_an_explicit_positive_state(self):
        likes = cleaner.parse_page(
            timeline_payload(
                tweet("10", "2", favorited=True),
                tweet("11", "2", favorited=False),
            ),
            "likes",
            "1",
        )
        self.assertEqual([(item.kind, item.id) for item in likes.items], [("likes", "10")])

        payload = timeline_payload()
        entries = payload["data"]["user"]["result"]["timeline"]["timeline"]["instructions"][0]["entries"]
        entries.extend(
            [
                {
                    "content": {
                        "itemContent": {
                            "user_results": {
                                "result": {
                                    "rest_id": "2",
                                    "legacy": {"following": True, "screen_name": "Alice_2"},
                                }
                            }
                        }
                    }
                },
                {
                    "content": {
                        "itemContent": {
                            "user_results": {
                                "result": {"rest_id": "3", "legacy": {"following": False}}
                            }
                        }
                    }
                },
            ]
        )
        following = cleaner.parse_page(payload, "following", "1")
        self.assertEqual([(item.kind, item.id) for item in following.items], [("following", "2")])
        self.assertEqual(following.items[0].username, "Alice_2")
        self.assertEqual(cleaner.account_label(following.items[0]), "@Alice_2")

    def test_account_label_falls_back_to_a_short_non_secret_id(self):
        item = cleaner.Item("following", "1847811426720636928", "1")
        self.assertEqual(cleaner.account_label(item), "账号 …636928")

    def test_profile_totals_read_stable_profile_counters(self):
        payload = {"data": {"user": {"result": {"legacy": {
            "statuses_count": 384,
            "favourites_count": 1260,
            "friends_count": 692,
        }}}}}
        self.assertEqual(cleaner.profile_totals(payload), {
            "posts": 384, "likes": 1260, "following": 692,
        })
        self.assertEqual(cleaner.profile_totals({}), {
            "posts": None, "likes": None, "following": None,
        })

    def test_unknown_timeline_shape_is_not_reported_as_an_empty_account(self):
        for payload in ({}, {"data": None}, {"errors": [{"code": 32}]}):
            with self.subTest(payload=payload):
                with self.assertRaises(cleaner.CleanerError):
                    cleaner.parse_page(payload, "posts", "1")

    def test_write_receipts_require_exact_semantic_confirmation(self):
        good = {
            "posts": {"data": {"delete_tweet": {"tweet_results": {}}}},
            "replies": {"data": {"delete_tweet": {"tweet_results": {}}}},
            "reposts": {"data": {"unretweet": {"source_tweet_results": {}}}},
            "likes": {"data": {"unfavorite_tweet": "Done"}},
            "following": {"id_str": "7", "following": False},
        }
        for kind, payload in good.items():
            item_id = "7" if kind == "following" else "10"
            with self.subTest(kind=kind):
                self.assertTrue(cleaner.validate_receipt(kind, item_id, payload))

        bad = (
            ("posts", "10", {}),
            ("posts", "10", {"data": {"delete_tweet": None}}),
            ("likes", "10", {"data": {"unfavorite_tweet": "Pending"}}),
            ("reposts", "10", {"data": {"unretweet": {}}}),
            ("following", "7", {"id_str": "8", "following": False}),
            ("following", "7", {"id_str": "7", "following": True}),
        )
        for kind, item_id, payload in bad:
            with self.subTest(kind=kind, payload=payload):
                self.assertFalse(cleaner.validate_receipt(kind, item_id, payload))

    def test_cleanup_confirmation_requires_the_exact_account_phrase(self):
        calls = []

        def answer(value):
            def inner(prompt):
                calls.append(prompt)
                return value
            return inner

        self.assertTrue(cleaner.confirm_cleanup("Example", 3, "帖子", answer("清理 @Example")))
        for value in ("", "y", "清理@Example", "清理 @example", " 清理 @Example "):
            with self.subTest(value=value):
                self.assertFalse(cleaner.confirm_cleanup("Example", 3, "帖子", answer(value)))
        self.assertTrue(any("3" in prompt and "Example" in prompt for prompt in calls))

    def test_http_errors_are_classified_without_echoing_response_secrets(self):
        request = httpx.Request("GET", "https://x.com/i/api/example")
        secret = "feedface" * 5
        cases = (
            (401, cleaner.AuthenticationError),
            (403, cleaner.PermissionDenied),
            (429, cleaner.RateLimited),
            (404, cleaner.EndpointChanged),
            (500, cleaner.NetworkFailure),
        )
        for status, error_type in cases:
            response = httpx.Response(
                status,
                request=request,
                json={"detail": f"server echoed auth_token={secret}"},
            )
            with self.subTest(status=status):
                with self.assertRaises(error_type) as caught:
                    cleaner.classify_http_error(response, "测试")
                self.assertNotIn(secret, str(caught.exception))

    def test_allowed_probe_404_does_not_raise(self):
        response = httpx.Response(
            404,
            request=httpx.Request("GET", "https://api.x.com/1.1/account/settings.json"),
            json={},
        )
        self.assertIsNone(
            cleaner.classify_http_error(response, "核对账号", allow_not_found=True)
        )

    def test_import_and_pure_helpers_do_not_access_the_network(self):
        # All tests in this class construct response objects directly.  This
        # guard catches accidental eager clients or network work during import.
        source = Path(cleaner.__file__).read_text(encoding="utf-8")
        self.assertNotIn("requests.get(", source)
        self.assertNotIn("requests.post(", source)

    def test_current_webpack_transaction_metadata_is_parsed(self):
        html = '<script>...,123:"ondemand.s"...,123:\'7a3c9e1b\'...</script>'
        self.assertEqual(
            cleaner.transaction_bundle_url(html),
            'https://abs.twimg.com/responsive-web/client-web/ondemand.s.7a3c9e1ba.js',
        )
        self.assertEqual(
            cleaner.transaction_key_indices('x[2],16 y[42], 16 z[45],16'),
            [2, 42, 45],
        )
        self.assertIsNone(cleaner.transaction_bundle_url('<html></html>'))


if __name__ == "__main__":
    unittest.main()
