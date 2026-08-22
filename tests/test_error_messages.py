"""What a failed request tells the user.

Every node in this package surfaces `_check`'s RuntimeError verbatim, in a
ComfyUI error dialog, to someone who cannot see the response. So the message
*is* the diagnostic — and a 404 that said only "not found — <url>" sent its
reader looking for a missing endpoint when the server had answered "Project 7
not found": a row, on a route that exists and is working.
"""

import unittest

import _bootstrap as boot

connection = boot.load("connection")

URL = "https://vault.example/api/v1/pictures/import"


class _Response:
    """The two bits of `requests.Response` that `_check` reads."""

    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.ok = 200 <= status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


def check(status_code, payload=None, text="", is_write=False):
    """`_check`'s message for one response, or None if it accepted it."""
    client = connection.PixlStashClient.__new__(connection.PixlStashClient)
    try:
        client._check(_Response(status_code, payload, text), URL, is_write=is_write)
    except RuntimeError as exc:
        return str(exc)
    return None


class NotFoundTests(unittest.TestCase):
    def test_a_missing_row_is_named(self):
        # The bug this file exists for: the Saver's import 404s with a project
        # that is not in the token's library, and the reader was told only that
        # a URL was not found.
        message = check(404, {"detail": "Project 7 not found"})
        self.assertIn("Project 7 not found", message)
        # The URL stays, because a genuinely missing route has no detail and
        # the path is then the only clue there is.
        self.assertIn(URL, message)

    def test_a_route_that_really_is_missing_still_says_which(self):
        for payload, text in ((None, "<html>404</html>"), ({}, ""), (None, "")):
            with self.subTest(payload=payload):
                message = check(404, payload, text)
                self.assertIn("not found", message)
                self.assertIn(URL, message)

    def test_an_html_error_page_does_not_become_the_whole_message(self):
        message = check(404, None, "<!doctype html>" + "x" * 5000)
        self.assertLess(len(message), 700)


class DetailShapeTests(unittest.TestCase):
    """`detail` is a string, a list of validation errors, or absent."""

    def test_a_validation_error_list_is_flattened(self):
        message = check(
            400,
            {
                "detail": [
                    {"loc": ["body", "file"], "msg": "field required"},
                    {"loc": ["body", "project_id"], "msg": "value is not a valid int"},
                ]
            },
        )
        self.assertIn("field required", message)
        self.assertIn("value is not a valid int", message)
        # Not the raw dicts — `loc` and `type` are noise to the person reading
        # a ComfyUI error dialog.
        self.assertNotIn("'loc'", message)

    def test_a_body_that_is_not_json_is_still_reported(self):
        message = check(400, None, "upstream refused the request")
        self.assertIn("upstream refused the request", message)

    def test_a_json_body_with_no_detail_is_not_dropped(self):
        message = check(400, {"error": "no space left"})
        self.assertIn("no space left", message)

    def test_a_structured_detail_is_json_not_a_python_repr(self):
        # The reader is looking at an HTTP response; `{'code': 5, 'ok': True}`
        # is not what any server sent.
        message = check(400, {"detail": {"code": 5, "retryable": True}})
        self.assertIn('"code": 5', message)
        self.assertIn("true", message)
        self.assertNotIn("'code'", message)
        self.assertNotIn("True", message)

    def test_a_detail_that_is_merely_falsy_is_still_reported(self):
        # `0` and `false` are things a server said. Testing truthiness dropped
        # them and left the caller with a bare URL and no reason.
        for detail, expected in ((0, "0"), (False, "false")):
            with self.subTest(detail=detail):
                message = check(404, {"detail": detail})
                self.assertIn(expected, message)

    def test_an_empty_detail_leaves_only_the_url(self):
        for detail in (None, "", [], {}):
            with self.subTest(detail=detail):
                message = check(404, {"detail": detail})
                self.assertEqual(message, f"PixlStash: not found — {URL}")


class OtherStatusTests(unittest.TestCase):
    """The messages that already worked, pinned so the refactor kept them."""

    def test_401_names_the_token(self):
        self.assertIn("invalid or expired", check(401))

    def test_403_on_a_write_names_the_write_scope(self):
        self.assertIn("write access", check(403, is_write=True))

    def test_403_on_a_read_does_not(self):
        message = check(403)
        self.assertIn("does not have access", message)
        self.assertNotIn("write access", message)

    def test_a_2xx_raises_nothing(self):
        for status in (200, 201, 204):
            with self.subTest(status=status):
                self.assertIsNone(check(status))


if __name__ == "__main__":
    unittest.main()
