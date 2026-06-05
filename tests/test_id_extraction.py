"""ID extraction: loader selections must reduce to a digits-only id.

A combo selection is the string ``"<name> #<id>"``. The numeric id is later
interpolated into REST paths (e.g. ``/picture_sets/<id>/members``), so the
extractor must never let path/query/script characters through. Anything that
isn't a trailing ``#<digits>`` yields ``""``.
"""

import unittest

import _bootstrap as boot

# All three loaders share the same _extract_id; project_loader is the sample.
project_loader = boot.load("nodes.project_loader")
extract_id = project_loader._extract_id


class ExtractIdTests(unittest.TestCase):
    def test_extracts_trailing_id(self):
        self.assertEqual(extract_id("My Project #42"), "42")
        self.assertEqual(extract_id("#7"), "7")
        self.assertEqual(extract_id("Trailing space #99   "), "99")

    def test_empty_and_missing(self):
        self.assertEqual(extract_id(""), "")
        self.assertEqual(extract_id("no id here"), "")
        self.assertEqual(extract_id("#"), "")

    def test_injection_payloads_yield_only_digits_or_empty(self):
        payloads = [
            "../../../../etc/passwd #5",  # path traversal in the name part
            "#5/../../etc/passwd",  # traversal AFTER the id
            "#5; rm -rf /",  # shell metacharacters
            "' OR '1'='1 #1",  # SQL-ish
            "<img src=x onerror=alert(1)> #3",  # XSS-ish
            "#5\n#9",  # newline smuggling
            "..%2f..%2f #8",  # encoded traversal
            "#1 2 3",  # extra tokens after id
        ]
        for p in payloads:
            with self.subTest(payload=p):
                out = extract_id(p)
                # Result is always either empty or pure ASCII digits.
                self.assertTrue(
                    out == "" or out.isdigit(),
                    f"{p!r} -> {out!r} is not digits-only",
                )
                self.assertNotIn("/", out)
                self.assertNotIn("..", out)


if __name__ == "__main__":
    unittest.main()
