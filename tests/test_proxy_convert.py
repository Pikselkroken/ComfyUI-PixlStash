"""``/pixlstash/workflows/convert``: *Export to PixlStash*'s pass-through.

What is pinned: the body reaches PixlStash's convert route as a write, a body
that isn't two JSON objects never leaves ComfyUI, and PixlStash's own status
and ``detail`` come back unchanged rather than flattened into a 502 — the
toast shows the reader what PixlStash said.
"""

import asyncio
import json
import unittest
from unittest import mock

import _bootstrap as boot

connection = boot.load("connection")
proxy = boot.load_proxy()

BODY = {"name": "wf", "workflow": {"nodes": []}, "output": {"1": {}}}


class _Request:
    def __init__(self, body, headers=None, query=None):
        self._body = body
        self.headers = (
            {"Authorization": "Bearer caller-token"} if headers is None else headers
        )
        self.rel_url = mock.Mock(query=query or {})

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, path, **kwargs):
        self.calls.append((path, kwargs))
        if self.error:
            raise self.error
        return self.response


def body_of(resp):
    return json.loads(resp.kwargs["body"])


class ConvertProxyTests(unittest.TestCase):
    def _call(self, request, client=None):
        client = client or _FakeClient(
            _Response({"name": "wf", "matched": True, "workflow_id": "manual:x"})
        )
        with mock.patch.object(proxy, "_build_client", lambda request: client):
            resp = asyncio.run(proxy.proxy_workflow_convert(request))
        return client, resp

    def test_forwards_the_body_as_a_write(self):
        client, resp = self._call(_Request(BODY))
        self.assertEqual(
            client.calls,
            [("/api/v1/comfyui/workflows/convert", {"is_write": True, "json": BODY})],
        )
        self.assertEqual(body_of(resp)["matched"], True)
        self.assertNotIn("status", resp.kwargs)

    def test_refuses_a_body_that_is_not_two_objects(self):
        for bad in (
            ValueError("not json"),
            [],
            {"workflow": {}},
            {"output": {}},
            {"workflow": [], "output": {}},
            {"workflow": {}, "output": "x"},
        ):
            with self.subTest(bad=bad):
                client, resp = self._call(_Request(bad))
                self.assertEqual(resp.kwargs.get("status"), 400)
                self.assertIn("JSON objects", body_of(resp)["error"])
                self.assertEqual(client.calls, [])

    def test_passes_pixlstash_status_and_detail_through(self):
        for status, detail in ((403, "Owner token required"), (413, "Too large")):
            with self.subTest(status=status):
                err = RuntimeError("PixlStash: flattened message")
                err.status, err.detail = status, detail
                _, resp = self._call(_Request(BODY), _FakeClient(error=err))
                self.assertEqual(resp.kwargs.get("status"), status)
                self.assertEqual(body_of(resp)["error"], detail)

    def test_an_unreachable_server_is_a_502(self):
        err = RuntimeError("PixlStash: connection error")
        _, resp = self._call(_Request(BODY), _FakeClient(error=err))
        self.assertEqual(resp.kwargs.get("status"), 502)
        self.assertEqual(body_of(resp)["error"], "PixlStash: connection error")


class ConvertProxyGuardTests(unittest.TestCase):
    """The refusals ``_build_client`` makes, through this route."""

    def _call(self, request, multi_user=False):
        creds = ("https://safe.example", "", True)
        with (
            mock.patch.object(proxy, "multi_user_active", lambda: multi_user),
            mock.patch.object(proxy, "read_credentials", lambda: creds),
            mock.patch.object(connection.PixlStashClient, "post") as post,
        ):
            resp = asyncio.run(proxy.proxy_workflow_convert(request))
        return post, resp

    def test_refuses_without_authorization(self):
        post, resp = self._call(_Request(BODY, headers={}))
        self.assertEqual(resp.kwargs.get("status"), 400)
        self.assertIn("Authorization", body_of(resp)["error"])
        post.assert_not_called()

    def test_refuses_under_multi_user(self):
        post, resp = self._call(_Request(BODY), multi_user=True)
        self.assertEqual(resp.kwargs.get("status"), 400)
        self.assertEqual(body_of(resp)["error"], connection.MULTI_USER_MESSAGE)
        post.assert_not_called()

    def test_a_url_in_the_query_or_body_is_ignored(self):
        body = {**BODY, "url": "http://evil.example"}
        request = _Request(body, query={"url": "http://evil.example"})
        seen = []

        def post(self, path, **kwargs):
            seen.append(self.base_url + path)
            return _Response({})

        with (
            mock.patch.object(proxy, "multi_user_active", lambda: False),
            mock.patch.object(
                proxy, "read_credentials", lambda: ("https://safe.example", "", True)
            ),
            mock.patch.object(connection.PixlStashClient, "post", post),
        ):
            asyncio.run(proxy.proxy_workflow_convert(request))
        self.assertEqual(
            seen, ["https://safe.example/api/v1/comfyui/workflows/convert"]
        )


class ClientErrorCarriesStatusTests(unittest.TestCase):
    """``PixlStashClient`` keeps what the server answered on its error."""

    def test_status_and_detail_ride_on_the_error(self):
        client = connection.PixlStashClient("https://safe.example", "t")
        reply = mock.Mock(status_code=413, ok=False, text='{"detail":"Too large"}')
        reply.json.return_value = {"detail": "Too large"}
        with (
            mock.patch.object(client, "_require_server_version"),
            mock.patch.object(client._session, "request", return_value=reply),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                client.post("/api/v1/comfyui/workflows/convert", is_write=True)
        self.assertEqual(ctx.exception.status, 413)
        self.assertEqual(ctx.exception.detail, "Too large")


if __name__ == "__main__":
    unittest.main()
