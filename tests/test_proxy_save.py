"""``/pixlstash/workflows/save``: a save of a workflow opened from PixlStash.

What is pinned: the two documents reach PixlStash's whole-graph save of the
workflow the canvas names, the runnable graph as the graph and the editor
document as its layout; an id that is not a workflow id never leaves ComfyUI;
``read_version`` goes only when the canvas knows it, and nothing the route
does not take is forwarded; and PixlStash's own
status comes back unchanged, since a 404 is what makes the toast offer
*Export to PixlStash*.
"""

import asyncio
import json
import unittest
from unittest import mock

import _bootstrap as boot

proxy = boot.load_proxy()

KEY = "auto:" + "a" * 64
EDITOR = {"nodes": [{"id": 1, "type": "x"}], "links": []}
OUTPUT = {"1": {"class_type": "x", "inputs": {}}}
BODY = {"workflow_key": KEY, "workflow": EDITOR, "output": OUTPUT}
PATH = f"/api/v1/workflows/{KEY}/graph"


class _Request:
    def __init__(self, body=None, query=None):
        self._body = body
        self.headers = {"Authorization": "Bearer caller-token"}
        self.rel_url = mock.Mock(query=query or {})

    async def json(self):
        return self._body


class _FakeClient:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def _send(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if self.error:
            raise self.error
        return mock.Mock(json=lambda: {"version": 4})

    def put(self, path, **kwargs):
        return self._send("PUT", path, **kwargs)

    def post(self, path, **kwargs):
        return self._send("POST", path, **kwargs)

    def get(self, path, **kwargs):
        return self._send("GET", path, **kwargs)


def body_of(resp):
    return json.loads(resp.kwargs["body"])


class SaveProxyTests(unittest.TestCase):
    def _call(self, handler, request, client=None):
        client = client or _FakeClient()

        def build(request, min_server_version=None):
            client.floor = min_server_version
            return client

        with mock.patch.object(proxy, "_build_client", build):
            resp = asyncio.run(handler(request))
        return client, resp

    def test_a_save_is_the_whole_graph_write_of_the_named_workflow(self):
        client, resp = self._call(proxy.proxy_workflow_save, _Request(BODY))
        [(method, path, kwargs)] = client.calls
        self.assertEqual((method, path, kwargs["is_write"]), ("PUT", PATH, True))
        self.assertEqual(
            kwargs["json"],
            {
                "workflow": OUTPUT,
                "editor": EDITOR,
                "source": "comfyui",
                "waiting_applied": False,
            },
        )
        self.assertEqual(body_of(resp), {"version": 4})

    def test_what_the_canvas_was_opened_with_goes_along(self):
        opening = "c" * 32
        opened = {
            **BODY,
            "read_version": 3,
            "waiting_applied": True,
            "opening": opening,
        }
        client, _ = self._call(proxy.proxy_workflow_save, _Request(opened))
        sent = client.calls[0][2]["json"]
        self.assertEqual(
            (sent["read_version"], sent["waiting_applied"], sent["opening"]),
            (3, True, opening),
        )

    def test_only_what_the_route_takes_is_forwarded(self):
        """A name for a new workflow is PixlStash's to give, who saved it is
        fixed here, and an opening that is not an opening's id is dropped."""
        body = {**BODY, "name": "Mine", "source": "mcp", "opening": "../x", "url": "x"}
        client, _ = self._call(proxy.proxy_workflow_save, _Request(body))
        sent = client.calls[0][2]["json"]
        self.assertEqual(
            sorted(sent), ["editor", "source", "waiting_applied", "workflow"]
        )
        self.assertEqual(sent["source"], "comfyui")

    def test_a_run_opened_from_pixlstash_is_sent_as_a_new_workflow(self):
        """The route says so, never the body: a flag in the body is what a
        ComfyUI still running the older routes would drop."""
        client, resp = self._call(proxy.proxy_workflow_save_new, _Request(BODY))
        [(method, path, kwargs)] = client.calls
        self.assertEqual((method, path, kwargs["is_write"]), ("PUT", PATH, True))
        self.assertIs(kwargs["json"]["as_new"], True)
        self.assertEqual(kwargs["json"]["workflow"], OUTPUT)
        self.assertEqual(body_of(resp), {"version": 4})
        client, _ = self._call(
            proxy.proxy_workflow_save, _Request({**BODY, "as_new": True})
        )
        self.assertNotIn("as_new", client.calls[0][2]["json"])

    def test_a_version_that_is_not_known_is_not_sent(self):
        """Its absence is what tells PixlStash the graph was read at none."""
        for unknown in (None, "3", True, 2.5):
            with self.subTest(unknown=unknown):
                body = {**BODY, "read_version": unknown, "waiting_applied": "yes"}
                client, _ = self._call(proxy.proxy_workflow_save, _Request(body))
                sent = client.calls[0][2]["json"]
                self.assertNotIn("read_version", sent)
                self.assertIs(sent["waiting_applied"], False)

    def test_anything_but_a_workflow_id_never_leaves_comfyui(self):
        for bad in (None, "", "manual:abc", KEY + "/../x", f"{KEY}\n", 7):
            with self.subTest(bad=bad):
                client, resp = self._call(
                    proxy.proxy_workflow_save, _Request({**BODY, "workflow_key": bad})
                )
                self.assertEqual(resp.kwargs.get("status"), 400)
                self.assertEqual(client.calls, [])

    def test_a_body_without_both_documents_is_refused(self):
        for bad in ({"workflow_key": KEY}, {**BODY, "output": []}, []):
            with self.subTest(bad=bad):
                client, resp = self._call(proxy.proxy_workflow_save, _Request(bad))
                self.assertEqual(resp.kwargs.get("status"), 400)
                self.assertEqual(client.calls, [])

    def test_pixlstash_status_comes_back_as_it_is(self):
        err = RuntimeError("PixlStash: flattened")
        err.status, err.detail = 404, "Unknown workflow."
        _, resp = self._call(
            proxy.proxy_workflow_save, _Request(BODY), _FakeClient(err)
        )
        self.assertEqual(resp.kwargs.get("status"), 404)
        self.assertEqual(body_of(resp)["error"], "Unknown workflow.")

    def test_the_people_read_is_of_the_named_workflow_only(self):
        client, _ = self._call(
            proxy.proxy_workflow_people, _Request(query={"workflow_key": KEY})
        )
        self.assertEqual(
            client.calls[0][:2], ("GET", f"/api/v1/workflows/{KEY}/people")
        )
        client, resp = self._call(
            proxy.proxy_workflow_people, _Request(query={"workflow_key": "../users"})
        )
        self.assertEqual((resp.kwargs.get("status"), client.calls), (400, []))

    def test_every_workflow_route_asks_for_the_pixlstash_that_has_it(self):
        """1.11 has none of these paths: it is told to update, not handed
        the 404 the save's toast reads as a workflow PixlStash does not have."""
        key = {"workflow_key": KEY}
        for handler, request in (
            (proxy.proxy_workflow_graph, _Request(query=key)),
            (proxy.proxy_workflow_people, _Request(query=key)),
            (proxy.proxy_workflow_convert, _Request(BODY)),
            (proxy.proxy_workflow_save, _Request(BODY)),
            (proxy.proxy_workflow_save_new, _Request(BODY)),
        ):
            with self.subTest(handler=handler.__name__):
                client, _ = self._call(handler, request)
                self.assertEqual((client.floor, len(client.calls)), ("1.12.0", 1))

    def test_the_floor_reaches_the_client_and_only_where_it_is_asked(self):
        request = _Request()
        with mock.patch.object(
            proxy, "read_credentials", lambda: ("https://safe.example", "", True)
        ):
            asked = proxy._build_client(request, "1.12.0")
            plain = proxy._build_client(request)
        self.assertEqual(asked.min_server_version, "1.12.0")
        self.assertEqual(plain.min_server_version, proxy.MIN_SERVER_VERSION)
        # A route older than the workflow routes keeps the package's own floor.
        client, _ = self._call(proxy.proxy_characters, _Request())
        self.assertEqual(client.floor, proxy.MIN_SERVER_VERSION)


if __name__ == "__main__":
    unittest.main()
