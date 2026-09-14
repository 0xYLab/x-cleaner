"""Local, request-only X cookie cleanup. Credentials never persist."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import re
import subprocess
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from twikit import Client


LABELS = {'posts': '帖子 / 引用帖', 'replies': '自己的回复', 'reposts': '转发', 'likes': '点赞', 'following': '关注'}

SEARCH_TIMELINE_FEATURES = {
    'rweb_video_screen_enabled': False, 'rweb_cashtags_enabled': True,
    'profile_label_improvements_pcf_label_in_post_enabled': True,
    'responsive_web_profile_redirect_enabled': False, 'rweb_tipjar_consumption_enabled': False,
    'verified_phone_label_enabled': False, 'creator_subscriptions_tweet_preview_api_enabled': True,
    'responsive_web_graphql_timeline_navigation_enabled': True,
    'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False,
    'premium_content_api_read_enabled': False,
    'communities_web_enable_tweet_community_results_fetch': True,
    'c9s_tweet_anatomy_moderator_badge_enabled': True,
    'responsive_web_grok_analyze_button_fetch_trends_enabled': False,
    'responsive_web_grok_analyze_post_followups_enabled': True,
    'responsive_web_jetfuel_frame': True, 'responsive_web_grok_share_attachment_enabled': True,
    'responsive_web_grok_annotations_enabled': True, 'articles_preview_enabled': True,
    'responsive_web_edit_tweet_api_enabled': True,
    'graphql_is_translatable_rweb_tweet_is_translatable_enabled': True,
    'view_counts_everywhere_api_enabled': True, 'longform_notetweets_consumption_enabled': True,
    'responsive_web_twitter_article_tweet_consumption_enabled': True,
    'content_disclosure_indicator_enabled': True,
    'content_disclosure_ai_generated_indicator_enabled': True,
    'responsive_web_grok_show_grok_translated_post': True,
    'responsive_web_grok_analysis_button_from_backend': True, 'post_ctas_fetch_enabled': True,
    'freedom_of_speech_not_reach_fetch_enabled': True, 'standardized_nudges_misinfo': True,
    'tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled': True,
    'longform_notetweets_rich_text_read_enabled': True,
    'longform_notetweets_inline_media_enabled': False,
    'responsive_web_grok_image_annotation_enabled': True,
    'responsive_web_grok_imagine_annotation_enabled': True,
    'responsive_web_grok_community_note_auto_translation_is_enabled': True,
    'responsive_web_enhance_cards_enabled': False,
}


class CleanerError(Exception): pass
class AuthenticationError(CleanerError): pass
class PermissionDenied(CleanerError): pass
class RateLimited(CleanerError): pass
class EndpointChanged(CleanerError): pass
class NetworkFailure(CleanerError): pass
class SkipTarget(CleanerError): pass
class PendingAction(CleanerError): pass


def home_identity(html):
    """Only accept the user bound to the page's session, never feed authors."""
    match = re.search(r'(?:window\.)?__INITIAL_STATE__\s*=\s*', html)
    if not match: return None
    try:
        state, _ = json.JSONDecoder().raw_decode(html[match.end():])
        session = state.get('session', {})
        user_id = session.get('user_id')
        if not user_id or not str(user_id).isdigit(): return None
        users = state.get('entities', {}).get('users', {}).get('entities', {})
        user = users.get(str(user_id), {})
        handle = user.get('screen_name') or user.get('core', {}).get('screen_name')
        if not isinstance(handle, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,15}', handle): return None
        return {'id_str': str(user_id), 'screen_name': handle}
    except (ValueError, AttributeError, TypeError):
        return None


@dataclass(repr=False)
class Credentials:
    auth_token: str
    ct0: str | None = None

    def __repr__(self):
        return 'Credentials(<hidden>)'


def parse_cookies(raw, ct0=None):
    if len(raw) > 16000 or any(ord(c) < 32 for c in raw):
        raise CleanerError('Cookie 格式不正确，请粘贴单行值。')
    raw = raw.strip()
    values = {}
    if re.fullmatch(r'[a-fA-F0-9]{40}', raw):
        values['auth_token'] = raw
    else:
        raw = re.sub(r'^Cookie:\s*', '', raw, flags=re.I)
        for part in raw.split(';'):
            key, sep, value = part.strip().partition('=')
            if sep and key in {'auth_token', 'ct0'}:
                if key in values and values[key] != value:
                    raise CleanerError('Cookie 中有冲突的重复值。')
                values[key] = value
    if ct0:
        values['ct0'] = ct0.strip()
    if not re.fullmatch(r'[a-fA-F0-9]{40}', values.get('auth_token', '')):
        raise CleanerError('需要有效的 auth_token 值。')
    if 'ct0' in values and not re.fullmatch(r'[a-zA-Z0-9_-]{16,512}', values['ct0']):
        raise CleanerError('ct0 格式不正确。')
    return Credentials(values['auth_token'], values.get('ct0'))


def classify_http_error(response, operation, allow_not_found=False):
    code = response.status_code
    if code < 300 or (allow_not_found and code == 404): return
    kind, message = {
        401: (AuthenticationError, '登录会话失效，请重新获取 Cookie'),
        403: (PermissionDenied, 'X 拒绝请求，请检查同一会话的 ct0 或在 X 完成账号验证'),
        404: (EndpointChanged, '网页接口已变化'),
        429: (RateLimited, 'X 已限流，请稍后重试'),
    }.get(code, (NetworkFailure, '请求失败或发生重定向'))
    raise kind(f'{operation}：{message}（HTTP {code}）。')


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values(): yield from walk(child)
    elif isinstance(value, list):
        for child in value: yield from walk(child)


def transaction_bundle_url(html):
    """Resolve X's current two-stage ondemand.s webpack filename."""
    match = re.search(r',(\d+):["\']ondemand\.s["\']', html)
    if not match: return None
    file_hash = re.search(rf',{re.escape(match.group(1))}:["\']([0-9a-f]+)["\']', html)
    if not file_hash: return None
    return f'https://abs.twimg.com/responsive-web/client-web/ondemand.s.{file_hash.group(1)}a.js'


def transaction_key_indices(script):
    return [int(value) for value in re.findall(r'\[(\d+)\],\s*16', script)]


@dataclass
class Item:
    kind: str
    id: str
    owner_id: str
    follows_me: bool | None = None
    username: str | None = None
    is_quote: bool = False


@dataclass
class Page:
    items: list[Item]
    cursor: str | None


def unwrap(result):
    # X has used both a named visibility wrapper and an otherwise identical
    # anonymous {tweet: ...} wrapper. Only unwrap when the outer object is not
    # itself a tweet, so quoted/retweeted children are never selected by error.
    for _ in range(4):
        if not isinstance(result, dict) or result.get('rest_id'): break
        child = result.get('tweet')
        if not isinstance(child, dict): break
        result = child
    return result if isinstance(result, dict) else {}


def tweet_owner(result):
    """Support both legacy and current GraphQL author locations."""
    legacy = result.get('legacy') or {}
    core = result.get('core') or {}
    candidates = [
        legacy.get('user_id_str'),
        (core.get('user_results') or {}).get('result', {}).get('rest_id'),
        (core.get('user_result') or {}).get('result', {}).get('rest_id'),
        core.get('user_id_str'),
    ]
    for value in candidates:
        if value is not None and str(value).isdigit(): return str(value)
    return None


def user_name(result):
    """Read an X handle without mistaking a display name for a username."""
    candidates = [
        (result.get('legacy') or {}).get('screen_name'),
        (result.get('core') or {}).get('screen_name'),
    ]
    for value in candidates:
        if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_]{1,15}', value):
            return value
    return None


def account_label(item):
    return f'@{item.username}' if item.username else f'账号 …{item.id[-6:]}'


def profile_totals(payload):
    """Return the three stable counters exposed by an X profile."""
    result = payload.get('data', {}).get('user', {}).get('result', {}) if isinstance(payload, dict) else {}
    legacy = result.get('legacy') or {}
    def count(name):
        value = legacy.get(name)
        return value if isinstance(value, int) and value >= 0 else None
    return {'posts': count('statuses_count'), 'likes': count('favourites_count'), 'following': count('friends_count')}


def parse_page(payload, category, owner_id):
    nodes = list(walk(payload))
    if not any('instructions' in n or 'entries' in n for n in nodes) or payload.get('errors'):
        raise EndpointChanged('无法识别扫描结果，数量未知。')
    items, seen, cursor, tweet_cells, unknown_authors = [], set(), None, 0, 0
    for node in nodes:
        if node.get('cursorType') == 'Bottom': cursor = node.get('value')
        cell = node.get('itemContent')
        # Current timelines sometimes place tweet_results/user_results directly
        # on an entry item instead of wrapping them in itemContent.
        if not isinstance(cell, dict) and ('tweet_results' in node or 'user_results' in node):
            cell = node
        if not isinstance(cell, dict) or cell.get('promotedMetadata'): continue
        if category == 'following':
            result = cell.get('user_results', {}).get('result', {})
            legacy = result.get('legacy') or {}
            if legacy.get('following') is not True and result.get('relationship_perspectives', {}).get('following') is not True:
                continue
            item_id, kind = result.get('rest_id'), 'following'
        else:
            result = unwrap(cell.get('tweet_results', {}).get('result', {}))
            legacy = result.get('legacy') or {}
            item_id, kind = result.get('rest_id'), category
            if item_id: tweet_cells += 1
            if category == 'likes':
                if legacy.get('favorited') is not True: continue
            else:
                author = tweet_owner(result)
                if author is None:
                    unknown_authors += 1
                    continue
                if author != owner_id: continue
                repost = unwrap(legacy.get('retweeted_status_result', {}).get('result', {}))
                reply = bool(legacy.get('in_reply_to_status_id_str'))
                if repost:
                    if category != 'posts': continue
                    item_id, kind = repost.get('rest_id'), 'reposts'
                elif reply != (category == 'replies'): continue
        if item_id and str(item_id).isdigit() and (kind, item_id) not in seen:
            seen.add((kind, item_id))
            followed_by = None
            if kind == 'following':
                states = [legacy.get('followed_by'), result.get('relationship_perspectives', {}).get('followed_by')]
                explicit = [v for v in states if isinstance(v, bool)]
                if explicit and all(v == explicit[0] for v in explicit): followed_by = explicit[0]
            username = user_name(result) if kind == 'following' else None
            items.append(Item(kind, str(item_id), owner_id, followed_by, username,
                              legacy.get('is_quote_status') is True))
    if category in {'posts', 'replies'} and tweet_cells and unknown_authors == tweet_cells:
        raise EndpointChanged('页面返回了帖子，但作者字段无法识别；未把它错误报告为0。')
    return Page(items, cursor)


def validate_receipt(kind, item_id, payload):
    if not isinstance(payload, dict) or payload.get('errors'): return False
    data = payload.get('data') or {}
    if kind in {'posts', 'replies'}:
        return isinstance(data.get('delete_tweet'), dict) and 'tweet_results' in data['delete_tweet']
    if kind == 'reposts':
        return isinstance(data.get('unretweet'), dict) and 'source_tweet_results' in data['unretweet']
    if kind == 'likes': return data.get('unfavorite_tweet') == 'Done'
    if kind == 'following':
        # Support both classic user receipts and nested user results. A matching
        # id and an explicit false relationship are required in the same object.
        for user in walk(payload):
            identity = user.get('id_str', user.get('rest_id', user.get('id')))
            if str(identity) != item_id: continue
            states = [user.get('following'), (user.get('legacy') or {}).get('following'),
                      (user.get('relationship_perspectives') or {}).get('following')]
            explicit = [s for s in states if isinstance(s, bool)]
            if explicit and all(s is False for s in explicit): return True
    return False


def confirm_cleanup(handle, total, description, reader=input):
    return reader(f'将清理 @{handle} 的 {description}，共 {total} 项。删除不能恢复。\n输入「清理 @{handle}」确认：') == f'清理 @{handle}'


def system_proxy():
    try:
        result = subprocess.run(['/usr/sbin/scutil', '--proxy'], capture_output=True, text=True, timeout=3, stdin=subprocess.DEVNULL)
        fields = dict(re.findall(r'(\w+)\s*:\s*([^\n]+)', result.stdout))
        for prefix in ('HTTPS', 'HTTP'):
            if fields.get(prefix + 'Enable', '').strip() == '1':
                host = fields.get(prefix + 'Proxy', '').strip()
                port = fields.get(prefix + 'Port', '').strip()
                if re.fullmatch(r'[\w.-]+', host) and port.isdigit() and 0 < int(port) < 65536:
                    return f'http://{host}:{port}'
    except (OSError, subprocess.TimeoutExpired): pass
    return None


async def guard(request):
    host = request.url.host
    if request.headers.get('cookie') or request.headers.get('x-csrf-token'):
        if request.url.scheme != 'https' or host not in {'x.com', 'api.x.com'}:
            raise CleanerError('已阻止向非 X 地址发送登录凭证。')


class ScopedClient(Client):
    transaction_home_html = None

    async def _init_graphql_transaction(self, headers):
        """Initialize transaction metadata atomically from verified /home HTML."""
        if not self.transaction_home_html:
            response = await self.http.get('https://x.com/home', headers=headers)
            if response.status_code != 200:
                raise EndpointChanged('无法初始化 X 请求标识：登录首页不可读。')
            self.transaction_home_html = response.text
        transaction = self.client_transaction
        soup = BeautifulSoup(self.transaction_home_html, 'lxml')
        # Twikit mutates its object partway through init(). If a later parsing
        # step fails, subsequent calls otherwise crash with AttributeError.
        transaction.home_page_response = None
        for name in ('key', 'key_bytes', 'animation_key'):
            if hasattr(transaction, name): delattr(transaction, name)
        try:
            bundle_url = transaction_bundle_url(str(soup))
            if not bundle_url: raise ValueError('ondemand.s bundle not found')
            bundle = await self.http.get(bundle_url, headers=headers)
            values = transaction_key_indices(bundle.text)
            if len(values) < 2: raise ValueError('KEY_BYTE indices not found')
            row, indices = values[0], values[1:]
            transaction.DEFAULT_ROW_INDEX = row
            transaction.DEFAULT_KEY_BYTES_INDICES = indices
            key = transaction.get_key(response=soup)
            key_bytes = transaction.get_key_bytes(key=key)
            animation_key = transaction.get_animation_key(key_bytes=key_bytes, response=soup)
        except Exception as exc:
            transaction.home_page_response = None
            for name in ('key', 'key_bytes', 'animation_key'):
                if hasattr(transaction, name): delattr(transaction, name)
            raise EndpointChanged(f'无法生成 X 请求标识（{type(exc).__name__}）。') from None
        transaction.home_page_response = soup
        transaction.key = key
        transaction.key_bytes = key_bytes
        transaction.animation_key = animation_key

    async def request(self, method, url, auto_unlock=False, raise_exception=True, **kwargs):
        """Send once with X's required GraphQL transaction id.

        Transaction metadata is initialized lazily only for GraphQL. Challenge
        solving, account unlocking and automatic write retries remain disabled.
        """
        headers = dict(kwargs.pop('headers', {}))
        path = urlparse(str(url)).path
        if path.startswith('/i/api/graphql/'):
            if not self.client_transaction.home_page_response:
                transaction_headers = {
                    'Accept-Language': f'{self.language},{self.language.split("-")[0]};q=0.9',
                    'Cache-Control': 'no-cache',
                    'Referer': 'https://x.com/',
                    'User-Agent': self._user_agent,
                }
                await self._init_graphql_transaction(transaction_headers)
            headers['X-Client-Transaction-Id'] = self.client_transaction.generate_transaction_id(
                method=method, path=path
            )
        response = await self.http.request(method, url, headers=headers, **kwargs)
        self._remove_duplicate_ct0_cookie()
        classify_http_error(response, 'X 请求')
        try:
            payload = response.json()
        except ValueError:
            raise EndpointChanged('X 返回的不是接口数据，可能需要网页验证。') from None
        if isinstance(payload, dict) and payload.get('errors'):
            errors = payload['errors']
            codes = {e.get('code') for e in errors if isinstance(e, dict)} if isinstance(errors, list) else set()
            if codes & {32, 89, 215}: raise AuthenticationError('X 未接受当前登录会话。')
            if 88 in codes: raise RateLimited('X 已限流，请稍后重试。')
            if codes & {64, 326}: raise PermissionDenied('账号需要在 X 网页完成验证或解除限制。')
            raise PermissionDenied('X 返回接口错误，已停止本次操作。')
        return payload, response

    def get_cookies(self):
        return {c.name: c.value for c in self.http.cookies.jar if c.domain in {'.x.com', 'x.com', 'api.x.com'} and c.name in {'auth_token', 'ct0'}}

    def set_cookies(self, cookies, clear_cookies=False):
        values = self.get_cookies() if not clear_cookies else {}
        values.update({k: v for k, v in cookies.items() if k in {'auth_token', 'ct0'}})
        self.http.cookies.clear()
        for key, value in values.items(): self.http.cookies.set(key, value, domain='.x.com', path='/')

    def _remove_duplicate_ct0_cookie(self):
        self.set_cookies(self.get_cookies(), clear_cookies=True)

    def _get_csrf_token(self): return self.get_cookies().get('ct0')


class CookieAPI:
    def __init__(self, credentials, proxy=None):
        self.proxy = proxy or None
        self.client = ScopedClient(proxy=self.proxy, timeout=30, follow_redirects=False, trust_env=False, event_hooks={'request': [guard]})
        self.client.set_cookies({'auth_token': credentials.auth_token, **({'ct0': credentials.ct0} if credentials.ct0 else {})})
        self.owner_id = None
        self.handle = None
        self.endpoints_checked = False
        self.profile_counts = {'posts': None, 'likes': None, 'following': None}

    async def refresh_endpoints(self):
        """Refresh operation ids from X's current web bundles.

        The logged-out landing page often serves only a challenge shell and no
        application bundles.  The authenticated /home HTML is therefore used
        only to discover public abs.twimg.com script URLs; credentials are
        never sent to that asset host.
        """
        from twikit.client.gql import Endpoint
        mapping = {'UserByScreenName': 'USER_BY_SCREEN_NAME', 'UserTweets': 'USER_TWEETS', 'UserTweetsAndReplies': 'USER_TWEETS_AND_REPLIES', 'Likes': 'USER_LIKES', 'Following': 'FOLLOWING', 'SearchTimeline': 'SEARCH_TIMELINE', 'DeleteTweet': 'DELETE_TWEET', 'DeleteRetweet': 'DELETE_RETWEET', 'UnfavoriteTweet': 'UNFAVORITE_TWEET'}
        mapping['UserByRestId'] = 'USER_BY_REST_ID'
        found = {}
        async with httpx.AsyncClient(proxy=self.proxy, timeout=12, follow_redirects=False, trust_env=False) as public:
            try:
                pages = []
                home = await public.get('https://x.com/')
                pages.append(home.text)
                signed_in = await self.client.http.get(
                    'https://x.com/home',
                    headers={'User-Agent': self.client._user_agent, 'Accept': 'text/html'},
                )
                if signed_in.status_code == 200: pages.append(signed_in.text)
                queue = []
                for html in pages:
                    queue.extend(re.findall(
                        r'(?:src|href)=[\"\']((?:https:)?//abs\.twimg\.com/[^\"\']+\.js)[\"\']',
                        html,
                    ))
                    queue.extend('https://abs.twimg.com/' + path.lstrip('/') for path in re.findall(
                        r'[\"\'](/?responsive-web/[^\"\']+\.js)[\"\']', html
                    ))
                queue = [('https:' + url) if url.startswith('//') else url for url in queue]
                visited = set()
                while queue and len(visited) < 96:
                    url = queue.pop(0)
                    if url in visited or urlparse(url).hostname != 'abs.twimg.com': continue
                    visited.add(url)
                    response = await public.get(url)
                    if response.status_code != 200: continue
                    text = response.text
                    for query, name in re.findall(r'queryId:[\"\']([\w-]+)[\"\'],operationName:[\"\'](\w+)[\"\']', text):
                        if name in mapping: found[name] = query
                    for relative in re.findall(r'[\"\'](\.{1,2}/[^\"\']+\.js)[\"\']', text):
                        queue.append(urljoin(url, relative))
                    for asset in re.findall(r'[\"\']((?:https:)?//abs\.twimg\.com/[^\"\']+\.js)[\"\']', text):
                        queue.append(('https:' + asset) if asset.startswith('//') else asset)
                    for path in re.findall(r'[\"\'](/?responsive-web/[^\"\']+\.js)[\"\']', text):
                        queue.append('https://abs.twimg.com/' + path.lstrip('/'))
                    if len(found) == len(mapping): break
            except httpx.HTTPError: pass
        for name, query in found.items():
            setattr(Endpoint, mapping[name], f'https://x.com/i/api/graphql/{query}/{name}')
        print(f'接口检查：已更新 {len(found)}/{len(mapping)} 项' if found else '接口检查：使用随程序附带的接口版本')
        self.endpoints_checked = True

    async def call(self, operation, method, *args):
        try:
            data, response = await method(*args)
            classify_http_error(response, operation)
            if not isinstance(data, dict) or data.get('errors'):
                raise PermissionDenied(f'{operation}：X 返回错误，已停止。')
            return data
        except CleanerError as exc:
            raise type(exc)(f'{operation}：{exc}') from None
        except httpx.HTTPError:
            raise NetworkFailure(f'{operation}：网络或代理连接失败。') from None
        except Exception as exc:
            name = type(exc).__name__
            cls = {'Unauthorized': AuthenticationError, 'Forbidden': PermissionDenied, 'NotFound': EndpointChanged, 'TooManyRequests': RateLimited}.get(name, EndpointChanged)
            raise cls(f'{operation}失败（{name}）；X 网页接口或会话需要检查。') from None

    async def identify(self):
        # /home establishes the cookie session but HTTP 200 alone proves nothing.
        url = 'https://x.com/home'
        for _ in range(4):
            home = await self.client.http.get(url, headers={'User-Agent': self.client._user_agent, 'Accept': 'text/html'})
            self.client._remove_duplicate_ct0_cookie()
            if home.status_code not in {301, 302, 303, 307, 308}: break
            target = urlparse(urljoin(url, home.headers.get('location', '')))
            if target.scheme != 'https' or target.hostname != 'x.com' or target.port not in {None, 443}:
                raise AuthenticationError('主页跳转到其他地址，未能确认登录。')
            if any(part in target.path for part in ('login', 'account/access', 'challenge')):
                raise AuthenticationError('X 要求重新登录或完成账号验证，尚未扫描。')
            url = target.geturl()
        else:
            raise NetworkFailure('X 主页重定向过多。')
        classify_http_error(home, '访问 X 主页')
        self.client.transaction_home_html = home.text
        data = home_identity(home.text)
        if not self.client._get_csrf_token():
            value = getpass.getpass('X 未自动返回 ct0，请粘贴同一会话的 ct0：')
            parsed = parse_cookies(self.client.get_cookies().get('auth_token', ''), value)
            self.client.set_cookies({'ct0': parsed.ct0})
        # Prefer a self endpoint that returns both the authenticated handle and
        # immutable id. Never derive identity from a user-supplied handle.
        if data is None:
            try:
                data = await self._verify_current_user()
            except EndpointChanged:
                try:
                    data = await self.call('核对账号', self.client.v11.settings)
                except EndpointChanged:
                    raise EndpointChanged('X 主页未提供可核验的登录用户名，两个账号接口也返回 404。当前纯请求登录尚不兼容；未开始扫描或清理。') from None
        handle = data.get('screen_name')
        if not isinstance(handle, str) or not re.fullmatch(r'\w{1,15}', handle):
            raise AuthenticationError('X 未返回当前登录账号，已停止。')
        owner = data.get('id_str') or data.get('id')
        if not owner:
            await self.refresh_endpoints()
            user = await self.call('读取账号 ID', self.client.gql.user_by_screen_name, handle)
            result = user.get('data', {}).get('user', {}).get('result', {})
            owner = result.get('rest_id')
            returned_handle = result.get('core', {}).get('screen_name') or result.get('legacy', {}).get('screen_name')
            if returned_handle and returned_handle.lower() != handle.lower(): raise AuthenticationError('账号核验结果不一致。')
        if not owner or not str(owner).isdigit(): raise EndpointChanged('账号数据结构已变化。')
        self.owner_id, self.handle = str(owner), handle
        return handle

    async def _verify_current_user(self):
        async def verify():
            return await self.client.get(
                'https://x.com/i/api/1.1/account/verify_credentials.json',
                headers=self.client._base_headers,
                params={'include_entities': 'false', 'skip_status': 'true'},
            )
        return await self.call('核对账号', verify)

    async def account_totals(self):
        if not self.endpoints_checked: await self.refresh_endpoints()
        data = await self.call('读取账号总量', self.client.gql.user_by_screen_name, self.handle)
        self.profile_counts = profile_totals(data)
        return dict(self.profile_counts)

    async def search_own_posts(self, limit):
        """Fallback when X's profile timeline returns cursors but no tweets."""
        found = {'posts': [], 'replies': [], 'reposts': []}
        seen, cursors, cursor = set(), set(), None
        query = f'from:{self.handle}'
        boundary = None
        full = getattr(self, 'full_scan', False)
        item_limit = float('inf') if full else limit
        for page_number in range(1, (1000 if full else 10) + 1):
            payload = await self.call(
                '备用扫描帖子', self.search_timeline_current,
                query, 'Latest', min(limit, 100), cursor,
            )
            pages = [parse_page(payload, category, self.owner_id) for category in ('posts', 'replies')]
            previous_count = len(seen)
            for page in pages:
                for item in page.items:
                    key = (item.kind, item.id)
                    if key not in seen and len(seen) < item_limit:
                        seen.add(key)
                        found[item.kind].append(item)
            print(f'\r备用扫描帖子：{len(seen)} 项 · 第 {page_number} 页', end='', flush=True)
            if len(seen) >= item_limit: break
            next_cursor = pages[0].cursor or pages[1].cursor
            if not next_cursor or next_cursor in cursors or len(seen) == previous_count:
                # Search cursors can keep changing while replaying the same
                # page. Restart below the oldest verified self-authored post;
                # never derive a boundary from a foreign repost's original ID.
                ids = [int(item.id) for kind in ('posts', 'replies') for item in found[kind]]
                older = min(ids) - 1 if ids else None
                if older is None or older < 1 or (boundary is not None and older >= boundary):
                    break
                boundary = older
                query = f'from:{self.handle} max_id:{boundary}'
                cursor, cursors = None, set()
                await asyncio.sleep(1)
                continue
            cursors.add(next_cursor)
            cursor = next_cursor
            await asyncio.sleep(1)
        print()
        return found

    async def search_timeline_current(self, query, product, count, cursor):
        """Use the synchronized variables/features required by current search."""
        from twikit.client.gql import Endpoint
        variables = {
            'rawQuery': query, 'count': count, 'querySource': 'typed_query',
            'product': product, 'withGrokTranslatedBio': True,
        }
        if cursor is not None: variables['cursor'] = cursor
        try:
            return await self.client.gql.gql_get(
                Endpoint.SEARCH_TIMELINE, variables, SEARCH_TIMELINE_FEATURES
            )
        except EndpointChanged:
            return await self.client.gql.gql_post(
                Endpoint.SEARCH_TIMELINE, variables, SEARCH_TIMELINE_FEATURES
            )

    async def scan(self):
        """扫描所有可读取分页，并保留每个分类的完成状态。

        full_scan 只取消常规批量条数限制；遇到重复游标、限流、接口变化
        或安全页数上限仍会停止，避免无限请求和误报“已扫描全部”。
        """
        if not self.owner_id: raise AuthenticationError('请先核对账号。')
        limit = getattr(self, 'batch_size', 50)
        if limit not in {50, 100}: raise CleanerError('批量大小必须是 50 或 100。')
        full = getattr(self, 'full_scan', False)
        page_limit = 1000 if full else 10
        item_limit = float('inf') if full else limit
        if not self.endpoints_checked: await self.refresh_endpoints()
        result = {key: [] for key in LABELS}
        self.scan_notes = []
        self.scan_failures = set()
        for category, method in [('posts', self.client.gql.user_tweets), ('replies', self.client.gql.user_tweets_and_replies), ('likes', self.client.gql.user_likes), ('following', self.client.gql.following)]:
            cursor, cursors, seen = None, set(), set()
            for page_number in range(1, page_limit + 1):
                try:
                    payload = await self.call('扫描' + LABELS[category], method, self.owner_id, limit, cursor)
                    page = parse_page(payload, category, self.owner_id)
                    if category == 'posts':
                        # A profile timeline may contain self replies as well
                        # as originals. Retain them even if the dedicated
                        # replies endpoint is unavailable.
                        replies = parse_page(payload, 'replies', self.owner_id)
                        page.items.extend(replies.items)
                except (AuthenticationError, PermissionDenied, RateLimited): raise
                except CleanerError as exc:
                    self.scan_failures.add(category)
                    self.scan_notes.append(f'{LABELS[category]}：已保留扫描结果，未扫完。{exc}')
                    break
                for item in page.items:
                    key = (item.kind, item.id)
                    if key not in seen:
                        seen.add(key)
                        if not any(old.id == item.id for old in result[item.kind]):
                            result[item.kind].append(item)
                    if len(seen) >= item_limit: break
                print(f'\r扫描 {LABELS[category]}：{len(seen)} 项 · 第 {page_number} 页', end='', flush=True)
                if len(seen) >= item_limit: break
                if not page.cursor: break
                if page.cursor in cursors:
                    self.scan_failures.add(category)
                    self.scan_notes.append(f'{LABELS[category]}：分页重复，扫描未完成；当前数量不是总量。')
                    break
                cursors.add(page.cursor)
                cursor = page.cursor
                await asyncio.sleep(1)
            else:
                self.scan_failures.add(category)
                self.scan_notes.append(f'{LABELS[category]}：达到 {page_limit} 页安全上限，扫描未完成。')
            print()
        # A failed profile-counter request produces None, not proof that the
        # account has zero posts. In that case the search fallback is required
        # just as much as when the counter is explicitly positive.
        if (self.profile_counts.get('posts') != 0 and
                (not result['posts'] and not result['replies'] or
                 bool({'posts', 'replies'} & self.scan_failures))):
            try:
                fallback = await self.search_own_posts(limit)
                added = 0
                for kind in ('posts', 'replies', 'reposts'):
                    existing = {item.id for item in result[kind]}
                    for item in fallback[kind]:
                        if item.id not in existing:
                            result[kind].append(item)
                            existing.add(item.id)
                            added += 1
                if any(fallback.values()):
                    self.scan_notes.append(f'备用搜索补充 {added} 项，已与主扫描去重合并；不代表历史帖子已扫完。')
                else:
                    self.scan_failures.update({'posts', 'replies'})
                    self.scan_notes.append('帖子 / 引用帖：资料页有帖子，但主时间线和备用检索都未返回可核验ID。')
            except (AuthenticationError, PermissionDenied, RateLimited):
                raise
            except CleanerError as exc:
                self.scan_failures.update({'posts', 'replies'})
                self.scan_notes.append(f'备用扫描帖子：{exc}')
        unknown = [item for item in result['following'] if item.follows_me is None]
        if unknown:
            followers, cursor, cursors, complete = set(), None, set(), False
            try:
                for number in range(1, 4):
                    payload = await self.call('核对粉丝列表', self.client.gql.followers, self.owner_id, limit, cursor)
                    nodes = list(walk(payload))
                    if not any('instructions' in n or 'entries' in n for n in nodes):
                        raise EndpointChanged('无法识别粉丝列表。')
                    next_cursor = None
                    for node in nodes:
                        if node.get('cursorType') == 'Bottom': next_cursor = node.get('value')
                        user = node.get('itemContent', {}).get('user_results', {}).get('result', {})
                        if user.get('rest_id'): followers.add(str(user['rest_id']))
                    print(f'\r核对回关：已读取 {len(followers)} 位粉丝', end='', flush=True)
                    if not next_cursor:
                        complete = True
                        break
                    if next_cursor in cursors: break
                    cursors.add(next_cursor)
                    cursor = next_cursor
                    await asyncio.sleep(1)
            except (AuthenticationError, PermissionDenied, RateLimited): raise
            except CleanerError:
                pass
            print()
            for item in unknown:
                if item.id in followers: item.follows_me = True
                elif complete: item.follows_me = False
            if not complete: self.scan_notes.append('粉丝列表尚未完整返回；无法确认回关关系的账号不会取关。')
        if full:
            # 粉丝列表只用于统计和核对回关关系，绝不会进入删除菜单。
            followers, cursor, visited = set(), None, set()
            self.follower_scan_complete = False
            try:
                for _ in range(page_limit):
                    payload = await self.call('扫描粉丝', self.client.gql.followers, self.owner_id, limit, cursor)
                    nodes = list(walk(payload))
                    if not any('instructions' in node for node in nodes):
                        raise EndpointChanged('粉丝列表格式无法识别。')
                    for node in nodes:
                        user = node.get('user_results', {}).get('result', {})
                        uid = user.get('rest_id')
                        if uid and str(uid).isdigit(): followers.add(str(uid))
                    next_cursor = next((node.get('value') for node in nodes if node.get('cursorType') == 'Bottom'), None)
                    if not next_cursor:
                        self.follower_scan_complete = True
                        break
                    if next_cursor in visited: break
                    visited.add(next_cursor)
                    cursor = next_cursor
                    await asyncio.sleep(1)
            except CleanerError as exc:
                self.scan_notes.append(f'粉丝扫描未完成：{exc}')
            self.follower_count = len(followers)
        return result

    async def unfollow_if_nonmutual(self, item):
        if item.kind != 'following' or item.owner_id != self.owner_id or item.follows_me is not False:
            raise CleanerError('该账号未被明确识别为未回关，已跳过取关。')
        # The immediately preceding scan already established this relationship.
        # Re-reading the full list for every user multiplied requests and caused
        # rate limits before large batches could finish.
        await self.remove(item)
        return True

    async def following_relationship(self, user_id):
        """Re-read the working following timeline, including explicit relation."""
        cursor, seen = None, set()
        for _ in range(10):
            data = await self.call('关注列表复核', self.client.gql.following, self.owner_id, getattr(self, 'batch_size', 50), cursor)
            page = parse_page(data, 'following', self.owner_id)
            for candidate in page.items:
                if candidate.id == user_id:
                    return {'following': True, 'followed_by': candidate.follows_me}
            if not page.cursor: break
            if page.cursor in seen: break
            seen.add(page.cursor)
            cursor = page.cursor
            await asyncio.sleep(1)
        raise SkipTarget('复核列表未返回此账号，不能确定关注状态；跳过此账号。')

    async def read_relationship(self, user_id):
        data = await self.call('读取目标账号关系', self.client.gql.user_by_rest_id, user_id)
        user = data.get('data', {}).get('user', {}).get('result', {})
        if str(user.get('rest_id')) != user_id:
            raise CleanerError('目标账号 ID 不匹配，无法核实关注关系。')
        legacy = user.get('legacy') or {}
        perspective = user.get('relationship_perspectives') or {}
        result = {}
        for key in ('following', 'followed_by'):
            values = [v for v in (legacy.get(key), perspective.get(key)) if isinstance(v, bool)]
            result[key] = values[0] if values and all(v == values[0] for v in values) else None
        return result

    async def destroy_friendship(self, user_id):
        """Use X's web endpoint, then its API host when the web route is gone."""
        form = {
            'include_profile_interstitial_type': 1, 'include_blocking': 1,
            'include_blocked_by': 1, 'include_followed_by': 1,
            'include_want_retweets': 1, 'include_mute_edge': 1,
            'include_can_dm': 1, 'include_can_media_tag': 1,
            'include_ext_is_blue_verified': 1, 'include_ext_verified_type': 1,
            'include_ext_profile_image_shape': 1, 'skip_status': 1,
            'user_id': user_id,
        }
        headers = self.client._base_headers | {'content-type': 'application/x-www-form-urlencoded'}

        async def send(url):
            return await self.client.post(url, data=form, headers=headers)

        try:
            return await self.call('清理关注', send, 'https://x.com/i/api/1.1/friendships/destroy.json')
        except EndpointChanged:
            return await self.call('清理关注（备用接口）', send, 'https://api.x.com/1.1/friendships/destroy.json')

    async def remove(self, item):
        if not self.owner_id or item.owner_id != self.owner_id: raise AuthenticationError('清理项目与登录账号不匹配。')
        if item.kind == 'following':
            data = await self.destroy_friendship(item.id)
        else:
            method = {'posts': self.client.gql.delete_tweet, 'replies': self.client.gql.delete_tweet, 'reposts': self.client.gql.delete_retweet, 'likes': self.client.gql.unfavorite_tweet}[item.kind]
            data = await self.call('清理' + LABELS[item.kind], method, item.id)
        if not validate_receipt(item.kind, item.id, data):
            if item.kind == 'following':
                raise PendingAction('取关已提交，留待整批结束后统一复扫确认。')
            raise CleanerError('操作回执不明确，已停止；该项可能已生效，请复扫核对。')

    async def close(self):
        self.client.http.cookies.clear()
        await self.client.http.aclose()
        self.owner_id = self.handle = None


def fair_batch(groups, selected, limit):
    items = []
    for index in range(max((len(groups[key]) for key in selected), default=0)):
        for key in selected:
            if index < len(groups[key]): items.append(groups[key][index])
            if len(items) == limit: return items
    return items


async def run_cookie_cleaner(proxy=None):
    api = None
    try:
        proxy = proxy if proxy is not None else system_proxy()
        while True:
            raw = getpass.getpass('\nauth_token（输入 q 退出）：')
            if raw.strip().lower() == 'q': return
            try:
                credentials = parse_cookies(raw)
                raw = ''
                api = CookieAPI(credentials, proxy)
                credentials.auth_token, credentials.ct0 = '', None
                print('正在登录……')
                handle = await api.identify()
                while True:
                    answer = input(f'登录成功：@{handle}，是你的账号吗？yes / no：').strip().lower()
                    if answer in {'yes', 'no'}: break
                    print('请输入 yes 或 no。')
                if answer == 'yes': break
            except (CleanerError, httpx.HTTPError) as exc:
                print(str(exc) if isinstance(exc, CleanerError) else '网络连接失败，请检查代理。')
            if api: await api.close()
            api = None
        while True:
            size = input('每页扫描 50 / 100 条（回车默认 50，不限制总扫描数）：').strip() or '50'
            if size in {'50', '100'}: break
            print('请输入 50 或 100。')
        api.batch_size = int(size)
        api.full_scan = True
        print('开始全量扫描，完成后选择清理数量；分页异常时保留已读取结果。')
        try:
            account_counts = await api.account_totals()
        except (EndpointChanged, NetworkFailure):
            account_counts = {'posts': None, 'likes': None, 'following': None}
        starting_counts = dict(account_counts)
        records = await api.scan()
        totals = {key: 0 for key in LABELS}
        pending = set()
        while True:
            print(f'\n── @{handle} · 扫描清单（异常分类仅为已读取数量） ──')
            shown = lambda value: str(value) if value is not None else '暂不可读'
            print(f'  账号总量：帖子 {shown(account_counts["posts"])} · 点赞 {shown(account_counts["likes"])} · 关注 {shown(account_counts["following"])}')
            changes = []
            for key, title in [('posts', '帖子'), ('likes', '点赞'), ('following', '关注')]:
                start, current = starting_counts[key], account_counts[key]
                if isinstance(start, int) and isinstance(current, int):
                    changes.append(f'{title}减少 {max(0, start - current)}')
            if changes: print('  自本次启动以来资料计数：' + ' · '.join(changes))
            posts = list({(item.kind, item.id): item for kind in ('posts', 'replies', 'reposts') for item in records[kind]}.values())
            quotes = sum(item.is_quote for item in records['posts'])
            print(f'  已读取：原创 {len(records["posts"]) - quotes} · 引用 {quotes} · 回复 {len(records["replies"])} · 转帖 {len(records["reposts"])} · 点赞 {len(records["likes"])}')
            if isinstance(getattr(api, 'follower_count', None), int):
                print(f'  粉丝：{api.follower_count}（' + ('扫描完成' if api.follower_scan_complete else '未扫完') + '，仅统计）')
            nonmutual = [item for item in records['following'] if item.follows_me is False and item.id not in pending]
            mutual = sum(item.follows_me is True for item in records['following'])
            unknown = sum(item.follows_me is None for item in records['following'])
            groups = {'1': posts, '2': records['likes'], '3': nonmutual}
            descriptions = {'1': '帖子（含引用、回复、转发）', '2': '点赞', '3': '未回关账号'}
            scan_failures = getattr(api, 'scan_failures', set())
            if not isinstance(scan_failures, set): scan_failures = set()
            failure_groups = {
                '1': bool({'posts', 'replies'} & scan_failures),
                '2': 'likes' in scan_failures,
                '3': 'following' in scan_failures,
            }
            for key in groups:
                if failure_groups[key] and not groups[key]:
                    value = '无法读取'
                else:
                    value = f'本批可选 {len(groups[key])} 项' + ('（部分未读取）' if failure_groups[key] else '')
                print(f'  {key}  {descriptions[key]}：{value}')
            print(f'  本批关注 {len(records["following"])} 人 · 互关 {mutual} · 未回关 {len(nonmutual)} · 关系未知 {unknown}')
            notes = getattr(api, 'scan_notes', [])
            if notes:
                print('  扫描提示：')
                for note in notes[-4:]: print('   - ' + note)
            if pending: print(f'  待复扫确认：取关 {len(pending)} 项（本次不重复）')
            print('  all 全部三项   r 重新扫描   0 退出')
            choice = input('选择（可多选，如 1,3）：').strip().lower()
            if choice in {'0', 'q'}: return
            if choice == 'r':
                records = await api.scan()
                try:
                    account_counts = await api.account_totals()
                except (EndpointChanged, NetworkFailure):
                    pass
                continue
            parts = choice.replace('，', ',').split(',')
            if choice == 'all': selected = list(groups)
            elif parts and all(p.strip() in groups for p in parts):
                selected = list(dict.fromkeys(p.strip() for p in parts))
            else:
                print('请选择 1—3、all、r 或 0。')
                continue
            blocked = [key for key in selected if failure_groups[key] and not groups[key]]
            if blocked:
                print('以下分类扫描失败，尚无可安全处理的ID：' + '、'.join(descriptions[key] for key in blocked))
                relevant = []
                for note in notes:
                    if any(LABELS[name] in note for key in blocked for name in ({'posts', 'replies'} if key == '1' else ({'likes'} if key == '2' else {'following'}))):
                        relevant.append(note)
                for note in relevant[:2]: print('  原因：' + note)
                print('请稍后输入 r 重新扫描，或选择其他可用分类。')
                continue
            available = {key: len(groups[key]) for key in selected}
            if not any(available.values()):
                print('所选分类没有扫描到可处理项目。')
                continue
            quantities = {}
            for key in selected:
                maximum = available[key]
                if maximum == 0:
                    quantities[key] = 0
                    continue
                while True:
                    raw_limit = input(f'{descriptions[key]}：清理 0—{maximum} 项（回车默认 0）：').strip()
                    if not raw_limit:
                        quantities[key] = 0
                        break
                    if raw_limit.isdigit() and 0 <= int(raw_limit) <= maximum:
                        quantities[key] = int(raw_limit)
                        break
                    print(f'请输入 0—{maximum}。')
            items = [item for key in selected for item in groups[key][:quantities[key]]]
            if not items:
                print('本次各类别数量均为 0，未执行清理。')
                continue
            print('本次计划：' + '、'.join(f'{descriptions[key]} {quantities[key]} 项' for key in selected))
            if not confirm_cleanup(handle, len(items), '、'.join(descriptions[k] for k in selected)): continue
            try:
                before_counts = dict(account_counts)
                skipped = 0
                failed = 0
                unavailable = set()
                attempted = []
                batch_confirmed = 0
                batch_stats = {key: {'confirmed': 0, 'pending': 0, 'failed': 0, 'skipped': 0} for key in ('1', '2', '3')}
                for index, item in enumerate(items, 1):
                    bucket = '1' if item.kind in {'posts', 'replies', 'reposts'} else ('2' if item.kind == 'likes' else '3')
                    target = f' · {account_label(item)}' if item.kind == 'following' else ''
                    if item.kind in unavailable:
                        skipped += 1
                        batch_stats[bucket]['skipped'] += 1
                        print(f'\r清理进度：{index}/{len(items)} · 跳过不可用的{LABELS[item.kind]}{target}', end='', flush=True)
                        continue
                    try:
                        attempted.append(item)
                        if item.kind == 'following':
                            if await api.unfollow_if_nonmutual(item):
                                totals[item.kind] += 1
                                batch_confirmed += 1
                                batch_stats[bucket]['confirmed'] += 1
                            else: skipped += 1
                        else:
                            await api.remove(item)
                            totals[item.kind] += 1
                            batch_confirmed += 1
                            batch_stats[bucket]['confirmed'] += 1
                    except SkipTarget as exc:
                        skipped += 1
                        batch_stats[bucket]['skipped'] += 1
                    except PendingAction as exc:
                        pending.add(item.id)
                        batch_stats[bucket]['pending'] += 1
                    except (EndpointChanged, NetworkFailure) as exc:
                        # One obsolete mutation must not cancel unrelated work.
                        # Disable only that action for the remainder of this batch
                        # so the terminal is not flooded with identical 404s.
                        failed += 1
                        batch_stats[bucket]['failed'] += 1
                        unavailable.add(item.kind)
                        print(f'\n{LABELS[item.kind]}暂停：接口暂不可用；其余类别继续。')
                    if index % 10 == 0 or index == len(items):
                        print(f'\r\033[2K进度 {index}/{len(items)} · 当前：{LABELS[item.kind]}{target}', end='', flush=True)
                    await asyncio.sleep(1.2)
                status = f'回执成功 {batch_confirmed} · 待确认 {sum(item.id in pending for item in attempted)} · 失败 {failed} · 未执行/跳过 {skipped}'
                if unavailable:
                    status += ' · 暂停类别：' + '、'.join(LABELS[k] for k in unavailable)
                print(f'\n本批处理结束：{status}。正在复扫……')
                records = await api.scan()
                after = {(item.kind, item.id) for values in records.values() for item in values}
                disappeared = sum((item.kind, item.id) not in after for item in attempted)
                remaining = sum((item.kind, item.id) in after for item in attempted)
                print(f'列表复扫（辅助核对）：本批对象中 {disappeared} 项未再发现 · {remaining} 项仍然存在；扫描窗口有限，以资料计数变化为准。')
                try:
                    account_counts = await api.account_totals()
                except (EndpointChanged, NetworkFailure):
                    pass
                planned_counts = {
                    'posts': quantities.get('1', 0),
                    'likes': quantities.get('2', 0),
                    'following': quantities.get('3', 0),
                }
                print('\n本轮结果：')
                for key, title in [('posts', '帖子'), ('likes', '点赞'), ('following', '关注')]:
                    bucket = {'posts': '1', 'likes': '2', 'following': '3'}[key]
                    stat = batch_stats[bucket]
                    result = f'计划 {planned_counts[key]} · 回执成功 {stat["confirmed"]} · 待确认 {stat["pending"]} · 未执行/失败 {stat["skipped"] + stat["failed"]}'
                    before, current = before_counts[key], account_counts[key]
                    if isinstance(before, int) and isinstance(current, int):
                        reduced = max(0, before - current)
                        result += f' · 资料计数减少 {reduced}'
                    if planned_counts[key]: print(f'  {title}：{result}')
                print('  注：资料计数可能包含上一轮延迟生效，不单独作为本轮成功数。')
            except CleanerError as exc:
                print(f'\n{exc}\nAPI成功回执累计（不等于主页计数变化）：' + '、'.join(f'{LABELS[k]} {v}' for k, v in totals.items()))
                return
    except (EOFError, KeyboardInterrupt): print('\n已停止。')
    except CleanerError as exc: print(f'\n{exc}')
    except httpx.HTTPError: print('\n网络连接失败，请检查代理地址。')
    finally:
        if api: await api.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--proxy')
    parser.add_argument('--no-system-proxy', action='store_true')
    parser.add_argument('--embedded', action='store_true')
    args = parser.parse_args()
    asyncio.run(run_cookie_cleaner(args.proxy if not args.no_system_proxy else ''))


if __name__ == '__main__': main()
