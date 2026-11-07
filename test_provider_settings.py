"""Credential persistence checks using synthetic keys only."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from provider_settings import ProviderSettings, validate_url


class ProviderSettingsTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / "data" / "test-runs"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def save(self):
        store = ProviderSettings(self.directory)
        store.save({"nai": {"url": "https://mock.example", "key": "SYNTHETIC_SECRET"}})
        return store

    def test_reopen_preserves_encrypted_settings(self):
        store = self.save()
        self.assertNotIn("SYNTHETIC_SECRET", store.path.read_text())
        self.assertEqual(ProviderSettings(self.directory).read()["nai"]["key"], "SYNTHETIC_SECRET")
        if os.name != "nt":
            self.assertEqual(store.key_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    @unittest.skipIf(os.name == "nt", "Linux Fernet storage")
    def test_missing_key_does_not_replace_existing_credentials(self):
        store = self.save()
        before = store.path.read_bytes()
        store.key_path.unlink()
        with self.assertRaisesRegex(ValueError, "密钥文件缺失"):
            store.save({"nai": {"key": "replacement"}})
        self.assertEqual(store.path.read_bytes(), before)
        self.assertFalse(store.key_path.exists())

    @unittest.skipIf(os.name == "nt", "Linux Fernet storage")
    def test_corrupt_config_is_rejected(self):
        store = self.save()
        wrapper = json.loads(store.path.read_text())
        wrapper["encrypted"] = "invalid-token"
        store.path.write_text(json.dumps(wrapper))
        with self.assertRaisesRegex(ValueError, "解密失败"):
            store.read()

    @unittest.skipIf(os.name == "nt", "Linux import boundary")
    def test_windows_config_requires_explicit_import(self):
        (self.directory / "providers.dpapi.json").write_text('{}')
        with self.assertRaisesRegex(ValueError, "Windows 凭据"):
            ProviderSettings(self.directory).read()

    def test_host_gateway_and_remote_http_validation(self):
        self.assertEqual(validate_url("http://host.docker.internal:8000/v1"),
                         "http://host.docker.internal:8000/v1")
        with self.assertRaises(ValueError):
            validate_url("http://remote.example/v1")


if __name__ == "__main__":
    unittest.main()
