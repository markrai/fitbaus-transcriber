import io
from pathlib import Path
import unittest
from unittest.mock import patch

from transcriber.__main__ import source_credential
from transcriber.audiobookshelf import SourceConfig
from transcriber.config import Config


class CredentialTests(unittest.TestCase):
    def config(self, scheme="https"):
        return Config("a" * 64, "127.0.0.1", 8765, Path("/unused"),
                      SourceConfig("home", scheme + "://localhost:13378"))

    def test_login_is_default(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=["", "reader"]), patch("getpass.getpass", return_value="password"), patch("transcriber.__main__.Audiobookshelf") as adapter:
            adapter.return_value.login.return_value = "issued-token"
            self.assertEqual(source_credential(self.config(), False), "issued-token")
            adapter.return_value.login.assert_called_once_with("reader", "password")

    def test_renewable_login_is_opt_in(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=["", "reader"]), patch("getpass.getpass", return_value="password"), patch("transcriber.__main__.Audiobookshelf") as adapter:
            adapter.return_value.login_session.return_value = ("access", "refresh")
            self.assertEqual(source_credential(self.config(), False, renewable=True), ("access", "refresh"))
            adapter.return_value.login_session.assert_called_once_with("reader", "password")

    def test_api_key_alternative(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="2"), patch("getpass.getpass", return_value="api-key"), patch("transcriber.__main__.Audiobookshelf") as adapter:
            self.assertEqual(source_credential(self.config(), False), "api-key")
            adapter.assert_not_called()

    def test_http_login_requires_explicit_confirmation(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=["1", ""]), patch("getpass.getpass") as password:
            with self.assertRaisesRegex(ValueError, "Login cancelled"):
                source_credential(self.config("http"), False)
            password.assert_not_called()

    def test_noninteractive_token_unchanged(self):
        with patch("sys.stdin", io.StringIO("script-token\n")):
            self.assertEqual(source_credential(self.config(), True), "script-token")
