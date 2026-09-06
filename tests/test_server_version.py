"""The version handshake: a node refuses a server older than it needs.

The browser-side check in ``web/js/combo_widgets.js`` only warns; a headless
run, or a user who ignores the badge, still reaches the server. So the client
asks ``GET /version`` once per server and refuses in plain words, naming both
versions, before the first request that would otherwise fail on a route the
server does not have yet.
"""

import pathlib
import re
import unittest

import _bootstrap as boot

connection = boot.load("connection")


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 300
        self.text = str(payload)

    def json(self):
        return self._payload


class _Session:
    """Answers ``/version`` with a fixed body and records every other call."""

    headers = {}

    def __init__(self, version):
        self.version = version
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append(url)
        if url.endswith("/version"):
            return _Response(self.version)
        return _Response({"ok": True})


def client(server_version, min_version="1.2.0", url="https://vault.example/api/v1"):
    c = connection.PixlStashClient(url, "example-token", min_server_version=min_version)
    c._session = _Session(server_version)
    return c


class ServerVersionTests(unittest.TestCase):
    def setUp(self):
        connection._server_versions.clear()

    def test_package_version_matches_pyproject(self):
        text = (pathlib.Path(boot.ROOT) / "pyproject.toml").read_text()
        declared = re.search(r'^version = "(.+)"$', text, re.M)[1]
        self.assertEqual(connection.VERSION, declared)

    def test_prerelease_tags_count_as_the_base_release(self):
        self.assertEqual(connection._base_version("1.11.0rc6"), (1, 11, 0))
        self.assertEqual(connection._base_version("1.4.0.dev2"), (1, 4, 0))
        self.assertIsNone(connection._base_version("<html>"))
        self.assertIsNone(connection._base_version(None))

    def test_an_old_server_is_refused_by_name(self):
        c = client({"version": "1.9.2"}, min_version="1.10.0")
        with self.assertRaises(RuntimeError) as ctx:
            c.get("/adapters")
        message = str(ctx.exception)
        self.assertIn("1.10.0", message)
        self.assertIn("1.9.2", message)
        self.assertIn(connection.VERSION, message)
        self.assertEqual(len(c._session.calls), 1, "no request past the refusal")
        # Forgotten, so an upgrade is seen on the next run instead of a cached refusal.
        self.assertNotIn(c.base_url, connection._server_versions)

    def test_a_new_enough_server_is_checked_once_per_url(self):
        c = client({"version": "1.11.0rc6"}, min_version="1.10.0")
        c.get("/adapters")
        c.get("/pictures")
        other = client({"version": "0.0.1"})  # same URL: never asked again
        other.get("/projects")
        self.assertEqual(
            [u.rsplit("/", 1)[1] for u in c._session.calls],
            ["version", "adapters", "pictures"],
        )
        self.assertEqual(
            other._session.calls, ["https://vault.example/api/v1/projects"]
        )

    def test_a_server_without_a_version_number_is_refused(self):
        c = client("<html>not pixlstash</html>")
        with self.assertRaises(RuntimeError) as ctx:
            c.get("/projects")
        self.assertIn("did not report a version", str(ctx.exception))

    def test_asking_for_the_version_itself_is_never_gated(self):
        c = client({"version": "0.0.1"})
        self.assertEqual(c.get("/version").json(), {"version": "0.0.1"})


if __name__ == "__main__":
    unittest.main()
