"""PixlStash's own certificate: Verify SSL stays on against a self-signed one.

When PixlStash links this ComfyUI over the network it writes its certificate
into ``PixlStash.CACertificate``. ``read_credentials`` turns that into a file
for ``requests``' ``verify``, and only while Verify SSL is on.
"""

import os
import tempfile
import threading
import unittest
from unittest import mock

import _bootstrap as boot

connection = boot.load("connection")

PEM = "-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----"


def _settings(**extra):
    base = {
        connection._SETTING_URL: "https://vault.example:9537",
        connection._SETTING_TOKEN: "example-token",
        connection._SETTING_SSL: True,
    }
    base.update(extra)
    return base


class CACertificateTests(unittest.TestCase):
    def read(self, settings):
        with boot.patched_modules(boot.cli_args_modules(multi_user=False)):
            with mock.patch.object(
                connection, "_comfy_settings", return_value=settings
            ):
                return connection.read_credentials()

    def test_a_set_certificate_becomes_the_file_requests_verifies_against(self):
        _, _, verify = self.read(_settings(**{connection._SETTING_CA: PEM}))
        self.assertIsInstance(verify, str)
        with open(verify, encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), PEM)

    def test_verify_ssl_off_still_wins(self):
        _, _, verify = self.read(
            _settings(**{connection._SETTING_SSL: False, connection._SETTING_CA: PEM})
        )
        self.assertIs(verify, False)

    def test_no_certificate_keeps_the_system_trust_store(self):
        _, _, verify = self.read(_settings(**{connection._SETTING_CA: ""}))
        self.assertIs(verify, True)

    def test_a_new_certificate_gets_a_new_file(self):
        _, _, first = self.read(_settings(**{connection._SETTING_CA: PEM}))
        _, _, second = self.read(
            _settings(**{connection._SETTING_CA: PEM.replace("ZmFrZQ", "b3RoZXI")})
        )
        self.assertNotEqual(first, second)
        self.assertTrue(os.path.isfile(second))

    def test_the_client_hands_the_file_to_every_request(self):
        _, _, verify = self.read(_settings(**{connection._SETTING_CA: PEM}))
        client = connection.PixlStashClient(
            "https://vault.example:9537", "example-token", verify_ssl=verify
        )
        seen = []

        def request(method, url, **kwargs):
            seen.append(kwargs.get("verify"))
            return mock.Mock(
                status_code=200, ok=True, json=lambda: {"version": "9.9.9"}
            )

        with mock.patch.object(client._session, "request", side_effect=request):
            client.server_version()
        self.assertEqual(seen, [verify])

    def test_a_planted_file_under_the_same_name_is_rewritten(self):
        _, _, path = self.read(_settings(**{connection._SETTING_CA: PEM}))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                "-----BEGIN CERTIFICATE-----\nplanted\n-----END CERTIFICATE-----\n"
            )
        _, _, again = self.read(_settings(**{connection._SETTING_CA: PEM}))
        self.assertEqual(again, path)
        with open(again, encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), PEM)

    def test_a_folder_another_user_owns_is_not_used(self):
        if not hasattr(os, "getuid"):
            self.skipTest("POSIX ownership only")
        with mock.patch.object(os, "getuid", return_value=os.getuid() + 1):
            _, _, verify = self.read(_settings(**{connection._SETTING_CA: PEM}))
        self.assertIs(verify, True)

    def _in_temp(self, prepare):
        """read_credentials with the temp dir swapped; *prepare* gets the folder path."""
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, f"comfyui-pixlstash-{os.getuid()}")
            prepare(tmp, folder)
            with mock.patch.object(tempfile, "gettempdir", return_value=tmp):
                return self.read(_settings(**{connection._SETTING_CA: PEM}))[2]

    def test_a_symlink_planted_as_the_folder_is_not_used(self):
        if not hasattr(os, "getuid"):
            self.skipTest("POSIX ownership only")

        def plant(tmp, folder):
            target = os.path.join(tmp, "elsewhere")
            os.mkdir(target, 0o700)
            os.symlink(target, folder)

        self.assertIs(self._in_temp(plant), True)

    def test_a_folder_others_can_write_is_not_used(self):
        if not hasattr(os, "getuid"):
            self.skipTest("POSIX ownership only")

        def loosen(tmp, folder):
            os.mkdir(folder)
            os.chmod(folder, 0o777)

        self.assertIs(self._in_temp(loosen), True)

    def test_concurrent_writes_of_one_new_certificate_all_succeed(self):
        pem = PEM.replace("ZmFrZQ", "Y29uY3VycmVudA")
        start = threading.Barrier(8)
        results, errors = [], []

        def worker():
            start.wait()
            try:
                results.append(connection._ca_bundle(pem))
            except Exception as exc:  # collected and asserted below
                errors.append(exc)

        with tempfile.TemporaryDirectory() as tmp:
            # No early return on an existing file: every thread takes the write path.
            with (
                mock.patch.object(tempfile, "gettempdir", return_value=tmp),
                mock.patch("builtins.open", side_effect=OSError("skip read")),
            ):
                threads = [threading.Thread(target=worker) for _ in range(8)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(set(results)), 1)
            with open(results[0], encoding="utf-8") as fh:
                self.assertEqual(fh.read().strip(), pem)
            self.assertEqual(
                [
                    n
                    for n in os.listdir(os.path.dirname(results[0]))
                    if n.endswith(".tmp")
                ],
                [],
            )


if __name__ == "__main__":
    unittest.main()
