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

    def get(self, path, **kwargs):
        return self._send("GET", path, **kwargs)


def body_of(resp):
    return json.loads(resp.kwargs["body"])


class SaveProxyTests(unittest.TestCase):
    def _call(self, handler, request, client=None):
        client = client or _FakeClient()
        with mock.patch.object(proxy, "_build_client", lambda request: client):
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


if __name__ == "__main__":
    unittest.main()
