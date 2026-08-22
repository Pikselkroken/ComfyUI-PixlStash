"""The model-shelf proxy routes.

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


class EntityThumbnailGuardTests(unittest.TestCase):
    """``/pixlstash/entity_thumbnail`` picks its upstream path from a table.

    Two caller-supplied values reach that decision: ``entity_type`` selects the
    template and ``entity_id`` is interpolated into it. The type is a lookup
    rather than a substitution, so the only thing that can carry a path is the
    id, and ``_positive_id`` is what stops it — the same guard
    ``proxy_thumbnail`` applies to ``picture_id``.
    """

    def _call(self, client=None, **params):
        client = client or _FakeClient(
            _Response(content=b"PNG", headers={"Content-Type": "image/png"})
        )
        with mock.patch.object(proxy, "_build_client", lambda request: client):
            resp = asyncio.run(proxy.proxy_entity_thumbnail(_Request(**params)))
        return client, resp

    def test_rejects_an_unknown_entity_type(self):
        for bad in ("", "picture", "character/../..", "CHARACTER"):
            with self.subTest(bad=bad):
                _, resp = self._call(entity_type=bad, entity_id="1")
                self.assertEqual(resp.kwargs.get("status"), 400)
                self.assertIn("entity_type", body_of(resp))

    def test_rejects_a_non_numeric_id(self):
        for bad in ("", "1/../../etc/passwd", "-1", "1.0", "1e3", "abc", "1 "):
            with self.subTest(bad=bad):
                _, resp = self._call(entity_type="character", entity_id=bad)
                self.assertEqual(resp.kwargs.get("status"), 400, f"accepted {bad!r}")
                self.assertIn("entity_id", body_of(resp))

    def test_rejects_an_id_no_row_can_have(self):
        # The message says "positive integer" and now the guard agrees: `0` and
        # a zero-padded id are digits, so `isdigit` alone forwarded them and
        # spent an upstream request being told there is no such row.
        for bad in ("0", "00", "007", "０"):
            with self.subTest(bad=bad):
                client, resp = self._call(entity_type="character", entity_id=bad)
                self.assertEqual(resp.kwargs.get("status"), 400, f"accepted {bad!r}")
                self.assertEqual(client.calls, [], f"asked upstream about {bad!r}")

    def test_a_padded_id_is_not_quietly_renumbered(self):
        # `int("007")` is 7 — the old guard would have fetched character 7 for
        # a caller who asked for "007". Refusing beats guessing which they meant.
        client, resp = self._call(entity_type="character", entity_id="007")
        self.assertEqual(resp.kwargs.get("status"), 400)
        self.assertEqual(client.calls, [])

    def test_a_character_reaches_the_character_thumbnail(self):
        client, resp = self._call(entity_type="character", entity_id="18")
        self.assertEqual(client.calls, [("/api/v1/characters/18/thumbnail", {})])
        self.assertEqual(resp.kwargs["body"], b"PNG")
        self.assertEqual(resp.kwargs["content_type"], "image/png")

    def test_a_set_reaches_the_picture_set_thumbnail(self):
        # A different upstream noun entirely — `set` is the record's word and
        # `picture_sets` is the API's, and getting that mapping wrong would 404
        # every set-attached model into the initials mark.
        client, _ = self._call(entity_type="set", entity_id="7")
        self.assertEqual(client.calls, [("/api/v1/picture_sets/7/thumbnail", {})])

    def test_an_entity_without_a_picture_is_an_error_not_a_crash(self):
        # A character with no reference face 404s. The picker reads that as
        # "draw the initials", so it has to come back as a response.
        class _Missing:
            def get(self, path, **kwargs):
                raise RuntimeError(
                    "PixlStash: not found — /api/v1/characters/9/thumbnail"
                )

        _, resp = self._call(client=_Missing(), entity_type="character", entity_id="9")
        self.assertEqual(resp.kwargs.get("status"), 502)
        self.assertIn("not found", body_of(resp))


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
