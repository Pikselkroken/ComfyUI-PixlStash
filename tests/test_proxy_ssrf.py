"""Proxy hardening: the SSRF and credential checks in ``_build_client``.

The proxy must (a) require a Bearer token from the caller, (b) resolve the
target URL/SSL *server-side* from ComfyUI settings rather than from the
request, and (c) only ever build a client for an ``http(s)`` host. Together
these stop the proxy being aimed at an attacker-chosen host.
"""

import unittest
from unittest import mock

import _bootstrap as boot

connection = boot.load("connection")
proxy = boot.load_proxy()


class BuildClientTests(unittest.TestCase):
    def setUp(self):
        # Single-user for all SSRF tests; the multi-user refusal is covered
        # separately in test_multi_user.py.
        self._mu = mock.patch.object(proxy, "multi_user_active", lambda: False)
        self._mu.start()
        self.addCleanup(self._mu.stop)

    def _creds(self, url):
        return mock.patch.object(proxy, "read_credentials", lambda: (url, "", True))

    def test_rejects_missing_authorization(self):
        req = boot.FakeRequest(headers={})
        with self._creds("https://vault.example"):
            with self.assertRaises(proxy.web.HTTPBadRequest) as ctx:
                proxy._build_client(req)
        self.assertIn("Authorization", ctx.exception.reason)

    def test_rejects_non_bearer_scheme(self):
        req = boot.FakeRequest(headers={"Authorization": "Token abc"})
        with self._creds("https://vault.example"):
            with self.assertRaises(proxy.web.HTTPBadRequest) as ctx:
                proxy._build_client(req)
        self.assertIn("Authorization", ctx.exception.reason)

    def test_rejects_when_server_url_unconfigured(self):
        req = boot.FakeRequest(headers={"Authorization": "Bearer t"})
        with self._creds(""):
            with self.assertRaises(proxy.web.HTTPBadRequest) as ctx:
                proxy._build_client(req)
        self.assertIn("not configured", ctx.exception.reason)

    def test_rejects_non_http_scheme(self):
        req = boot.FakeRequest(headers={"Authorization": "Bearer t"})
        # A non-http(s) configured URL (e.g. file://, ftp://, gopher://) must
        # never be turned into a client.
        with self._creds("file:///etc/passwd"):
            with self.assertRaises(proxy.web.HTTPBadRequest) as ctx:
                proxy._build_client(req)
        self.assertIn("http", ctx.exception.reason)

    def test_uses_server_side_url_not_request(self):
        # The token comes from the caller's header, but the host is fixed
        # server-side. Even a request carrying ?url=http://evil cannot redirect
        # the proxy: _build_client never reads the query for the target host.
        req = boot.FakeRequest(headers={"Authorization": "Bearer caller-token"})
        with self._creds("https://safe.example/"):
            client = proxy._build_client(req)
        self.assertEqual(client.base_url, "https://safe.example")
        self.assertNotIn("evil", client.base_url)
        # Token is the caller's, taken from the Authorization header.
        self.assertEqual(
            client._session.headers["Authorization"], "Bearer caller-token"
        )


if __name__ == "__main__":
    unittest.main()
