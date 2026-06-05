"""Multi-user guard: PixlStash must refuse to run under ``--multi-user``.

ComfyUI does not tell a node which user submitted the running prompt, so a
node cannot pick that user's stored token. Rather than risk authenticating as
the wrong user, both the nodes (via ``read_credentials``) and the proxy (via
``_build_client``) refuse outright.
"""

import unittest

import _bootstrap as boot

connection = boot.load("connection")


class MultiUserActiveTests(unittest.TestCase):
    def test_true_when_flag_set(self):
        with boot.patched_modules(boot.cli_args_modules(multi_user=True)):
            self.assertTrue(connection.multi_user_active())

    def test_false_when_flag_clear(self):
        with boot.patched_modules(boot.cli_args_modules(multi_user=False)):
            self.assertFalse(connection.multi_user_active())

    def test_false_when_comfy_unavailable(self):
        # Outside ComfyUI the import fails; the guard must fail open to False
        # so the lint/test env and non-ComfyUI callers are unaffected.
        with boot.patched_modules({"comfy": None, "comfy.cli_args": None}):
            self.assertFalse(connection.multi_user_active())


class ReadCredentialsGuardTests(unittest.TestCase):
    def test_raises_even_with_explicit_credentials(self):
        # Explicit url/token must NOT bypass the guard: the check is first.
        with boot.patched_modules(boot.cli_args_modules(multi_user=True)):
            with self.assertRaises(RuntimeError) as ctx:
                connection.read_credentials("https://vault.example", "secret-token")
        self.assertEqual(str(ctx.exception), connection.MULTI_USER_MESSAGE)
        self.assertIn("multi-user", str(ctx.exception))

    def test_passes_through_when_single_user(self):
        with boot.patched_modules(boot.cli_args_modules(multi_user=False)):
            url, token, ssl = connection.read_credentials(
                "https://vault.example", "secret-token", True
            )
        self.assertEqual(url, "https://vault.example")
        self.assertEqual(token, "secret-token")


class ProxyGuardTests(unittest.TestCase):
    def setUp(self):
        self.proxy = boot.load_proxy()

    def test_build_client_refuses_in_multi_user(self):
        req = boot.FakeRequest(headers={"Authorization": "Bearer secret-token"})
        with boot.patched_modules(boot.cli_args_modules(multi_user=True)):
            with self.assertRaises(self.proxy.web.HTTPBadRequest) as ctx:
                self.proxy._build_client(req)
        self.assertEqual(ctx.exception.reason, connection.MULTI_USER_MESSAGE)


if __name__ == "__main__":
    unittest.main()
