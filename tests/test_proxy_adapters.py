"""The two model-shelf proxy routes.

``/pixlstash/model_icon`` interpolates ``icon_sha256`` into an upstream path,
so the same concern applies as to ``proxy_thumbnail``'s ``picture_id.isdigit()``
guard: anything that is not a 64-character lowercase hex digest is refused
before a client is built.

Every refusal here asserts on the *message*, not just the 400. The unconfigured
-server path also returns a 400, so a status-only assertion would pass with the
digest guard deleted — which is no test at all.

``/pixlstash/adapters`` is a plain forward; what is pinned is that it goes to
the right upstream path and that ``url`` / ``verify_ssl`` never survive the hop
(the SSRF rule ``_build_client`` enforces, from the query side).
"""

import asyncio
import json
import unittest
from unittest import mock

import _bootstrap as boot

proxy = boot.load_proxy()

GOOD = "a" * 64


class _Query:
    def __init__(self, params):
        self._params = params

    def get(self, key, default=None):
        return self._params.get(key, default)

    def items(self):
        return self._params.items()


class _Url:
    def __init__(self, params):
        self.query = _Query(params)


class _Request:
    """Enough of aiohttp's Request for these two handlers."""

    def __init__(self, **params):
        self.headers = {"Authorization": "Bearer caller-token"}
        self.rel_url = _Url(params)


class _Response:
    def __init__(self, payload=None, content=b"", headers=None):
        self._payload = payload
        self.content = content
        self.headers = headers or {}

    def json(self):
        return self._payload


class _FakeClient:
    """Records the upstream call instead of making it."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return self.response


def body_of(resp):
    return resp.kwargs.get("body", b"").decode()


class ModelIconGuardTests(unittest.TestCase):
    def _refused(self, value):
        resp = asyncio.run(proxy.proxy_model_icon(_Request(icon_sha256=value)))
        self.assertEqual(resp.kwargs.get("status"), 400, f"accepted {value!r}")
        # Naming the digest is what distinguishes this from the unconfigured-
        # server 400 the handler would otherwise fall through to.
        self.assertIn("hex digest", body_of(resp), f"wrong 400 for {value!r}")

    def test_rejects_missing(self):
        self._refused("")

    def test_rejects_uppercase_hex(self):
        self._refused("A" * 64)

    def test_rejects_wrong_length(self):
        self._refused("a" * 63)
        self._refused("a" * 65)

    def test_rejects_traversal(self):
        self._refused("../../../etc/passwd")
        self._refused("a" * 63 + "/")
        self._refused("../" + "a" * 61)

    def test_rejects_shell_and_query_punctuation(self):
        self._refused("a" * 63 + ";")
        self._refused(f"{GOOD}?x=1")
        self._refused(f"{GOOD}#frag")

    def test_rejects_trailing_newline(self):
        # `$` would let this through; the guard anchors with \Z.
        self._refused(GOOD + "\n")

    def test_forwards_a_real_digest_verbatim(self):
        """The positive control: a digest reaches the icon path unchanged."""
        client = _FakeClient(
            _Response(content=b"PNGDATA", headers={"Content-Type": "image/png"})
        )
        with mock.patch.object(proxy, "_build_client", lambda request: client):
            resp = asyncio.run(proxy.proxy_model_icon(_Request(icon_sha256=GOOD)))

        self.assertEqual(client.calls, [(f"/api/v1/model-icons/{GOOD}", {})])
        self.assertEqual(resp.kwargs["body"], b"PNGDATA")
        self.assertEqual(resp.kwargs["content_type"], "image/png")


class AdapterListProxyTests(unittest.TestCase):
    def _call(self, **params):
        client = _FakeClient(_Response(payload={"adapters": [{"sha256": GOOD}]}))
        with mock.patch.object(proxy, "_build_client", lambda request: client):
            resp = asyncio.run(proxy.proxy_adapters(_Request(**params)))
        return client, resp

    def test_hits_the_adapter_list_route(self):
        client, resp = self._call(file_kind="adapter")
        path, kwargs = client.calls[0]
        self.assertEqual(path, "/api/v1/adapters")
        self.assertEqual(json.loads(resp.kwargs["body"])["adapters"][0]["sha256"], GOOD)

    def test_forwards_the_picker_filters(self):
        client, _ = self._call(
            file_kind="adapter",
            kind="lokr",
            base_model="SDXL",
            character_id="7",
            q="hair",
        )
        self.assertEqual(
            client.calls[0][1]["params"],
            {
                "file_kind": "adapter",
                "kind": "lokr",
                "base_model": "SDXL",
                "character_id": "7",
                "q": "hair",
            },
        )

    def test_never_forwards_the_ssrf_params(self):
        # A client that still sends url/verify_ssl (an older picker, or an
        # attacker) must not have them reach the upstream query.
        client, _ = self._call(
            file_kind="adapter",
            url="http://evil.example",
            verify_ssl="false",
        )
        self.assertEqual(client.calls[0][1]["params"], {"file_kind": "adapter"})


if __name__ == "__main__":
    unittest.main()
