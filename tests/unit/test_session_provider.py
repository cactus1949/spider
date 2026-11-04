"""测试浏览器会话提供者（注入假 page）。"""

from __future__ import annotations

import pytest

from spider_gd.core.crawler.session_provider import BrowserSessionProvider, parse_hdkgccbl


class _FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies

    async def cookies(self):
        return self._cookies


class _FakePage:
    def __init__(self, url, token_value, cookies):
        self.url = url
        self._token_value = token_value
        self.context = _FakeContext(cookies)

    async def evaluate(self, expr):
        if "token" in expr.lower():
            return self._token_value
        return None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://pm.gd.csg.cn/p?HdKGccbL=abc123&x=1", "abc123"),
        ("https://pm.gd.csg.cn/#/daily?HdKGccbL=xyz789", "xyz789"),
        ("https://pm.gd.csg.cn/p?x=1", ""),
    ],
)
def test_parse_hdkgccbl(url, expected):
    assert parse_hdkgccbl(url) == expected


@pytest.mark.unit
async def test_get_session_reads_all_fields():
    page = _FakePage(
        url="https://pm.gd.csg.cn/#/daily?HdKGccbL=abc123",
        token_value="tok-123",
        cookies=[{"name": "SESSION_ID", "value": "s1"}, {"name": "XSRF", "value": "x1"}],
    )
    provider = BrowserSessionProvider(page)
    session = await provider.get_session()
    assert session.bearer_token == "tok-123"
    assert session.hdkgccbl == "abc123"
    assert session.cookies == {"SESSION_ID": "s1", "XSRF": "x1"}


@pytest.mark.unit
def test_is_expired_only_for_412():
    provider = BrowserSessionProvider(page=None)
    assert provider.is_expired(412) is True
    assert provider.is_expired(200) is False
    assert provider.is_expired(401) is False


@pytest.mark.unit
async def test_refresh_rereads_session():
    page = _FakePage("https://pm.gd.csg.cn/?HdKGccbL=k", "t", [])
    provider = BrowserSessionProvider(page)
    session = await provider.refresh()
    assert session.hdkgccbl == "k"
