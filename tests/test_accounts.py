from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from wechat_codex.accounts import AccountRegistry, AccountRegistryError
from wechat_codex.config import AppConfig, load_config
from wechat_codex.ilink_api import ILinkCredentials
from wechat_codex.ilink_auth import load_credentials, save_credentials


class AccountRegistryTests(unittest.TestCase):
    def _config(self, root: Path) -> AppConfig:
        source = root / "config.yaml"
        source.write_text("projects:\n  demo: .\n", encoding="utf-8")
        return load_config(source)

    @staticmethod
    def _credentials(account_id: str, user_id: str = "owner") -> ILinkCredentials:
        return ILinkCredentials(
            token=f"token-{account_id}",
            account_id=account_id,
            user_id=user_id,
            base_url="https://ilinkai.weixin.qq.com",
        )

    def test_imports_legacy_credentials_on_first_load(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            credentials = self._credentials("bot-legacy")
            save_credentials(config.ilink_credentials_file, credentials)

            registry = AccountRegistry(config)

            self.assertEqual(len(registry.list_accounts()), 1)
            legacy = registry.list_accounts()[0]
            self.assertTrue(legacy.legacy)
            self.assertEqual(legacy.key, "legacy")
            self.assertEqual(legacy.account_id, "bot-legacy")
            self.assertEqual(legacy.runtime_dir, config.runtime_dir.resolve())
            self.assertEqual(legacy.credentials_file, config.ilink_credentials_file)
            saved = json.loads(registry.registry_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["accounts"][0]["runtime_dir"], ".")

            reloaded = AccountRegistry(config)
            self.assertEqual(reloaded.list_accounts(), [legacy])

    def test_reserve_register_and_deduplicate_account_id(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            registry = AccountRegistry(config)

            reserved = registry.reserve_new()

            self.assertTrue(reserved.runtime_dir.is_dir())
            self.assertEqual(
                reserved.runtime_dir.parent,
                config.runtime_dir.resolve() / "accounts",
            )
            self.assertFalse(registry.registry_file.exists())
            credentials = self._credentials("bot-one")
            save_credentials(reserved.credentials_file, credentials)
            registered = registry.register(reserved, credentials)

            self.assertEqual(registered.account_id, "bot-one")
            self.assertEqual(registered.user_id, "owner")
            self.assertEqual(registry.list_accounts(), [registered])
            self.assertTrue(registry.registry_file.is_file())

            duplicate_reservation = registry.reserve_new()
            duplicate_credentials = ILinkCredentials(
                token="fresh-token",
                account_id="bot-one",
                user_id="owner",
                base_url="https://ilinkai.weixin.qq.com",
            )
            save_credentials(
                duplicate_reservation.credentials_file, duplicate_credentials
            )
            duplicate = registry.register(
                duplicate_reservation, duplicate_credentials
            )

            self.assertEqual(duplicate, registered)
            self.assertEqual(registry.list_accounts(), [registered])
            persisted = json.loads(
                registry.registry_file.read_text(encoding="utf-8")
            )
            self.assertEqual(len(persisted["accounts"]), 1)
            self.assertEqual(
                load_credentials(registered.credentials_file).token,
                "fresh-token",
            )

    def test_reload_recovers_completed_login_missing_from_registry(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            registry = AccountRegistry(config)
            reserved = registry.reserve_new()
            credentials = self._credentials("bot-recovered")
            save_credentials(reserved.credentials_file, credentials)

            recovered_registry = AccountRegistry(config)

            recovered = recovered_registry.list_accounts()
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0].key, reserved.key)
            self.assertEqual(recovered[0].account_id, "bot-recovered")
            self.assertTrue(recovered_registry.registry_file.is_file())

    def test_broken_registered_credentials_do_not_hide_other_accounts(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            registry = AccountRegistry(config)
            first = registry.reserve_new()
            save_credentials(first.credentials_file, self._credentials("bot-one"))
            registered_first = registry.register(
                first, self._credentials("bot-one")
            )
            second = registry.reserve_new()
            save_credentials(second.credentials_file, self._credentials("bot-two"))
            registry.register(second, self._credentials("bot-two"))
            registered_first.credentials_file.unlink()

            reloaded = AccountRegistry(config)

            self.assertEqual(
                [record.account_id for record in reloaded.list_accounts()],
                ["bot-one", "bot-two"],
            )

    def test_register_requires_credentials_written_by_login_flow(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            registry = AccountRegistry(config)
            reserved = registry.reserve_new()

            with self.assertRaisesRegex(AccountRegistryError, "does not exist"):
                registry.register(reserved, self._credentials("bot-one"))

            self.assertEqual(registry.list_accounts(), [])
            self.assertFalse(registry.registry_file.exists())

    def test_rejects_registry_path_traversal(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            config.runtime_dir.mkdir(parents=True)
            key = "12345678123456781234567812345678"
            registry_file = config.runtime_dir / "wechat-accounts.json"
            registry_file.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "accounts": [
                            {
                                "key": key,
                                "account_id": "bot-one",
                                "user_id": "owner",
                                "runtime_dir": "../outside",
                                "credentials_file": "../outside/account.json",
                                "state_file": "../outside/state.json",
                                "legacy": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(AccountRegistryError, "Unsafe|escapes"):
                AccountRegistry(config)

    def test_rejects_malformed_registry_json(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            config.runtime_dir.mkdir(parents=True)
            (config.runtime_dir / "wechat-accounts.json").write_text(
                '{"version": 1, "accounts": "wrong"}', encoding="utf-8"
            )

            with self.assertRaisesRegex(AccountRegistryError, "must be a list"):
                AccountRegistry(config)

    def test_failed_atomic_registry_write_does_not_publish_record_in_memory(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            registry = AccountRegistry(config)
            reserved = registry.reserve_new()
            credentials = self._credentials("bot-one")
            save_credentials(reserved.credentials_file, credentials)

            with patch(
                "wechat_codex.accounts.atomic_write_text",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    registry.register(reserved, credentials)

            self.assertEqual(registry.list_accounts(), [])
            self.assertFalse(registry.registry_file.exists())

    def test_account_config_scopes_account_runtime_and_ilink_files(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            registry = AccountRegistry(config)
            reserved = registry.reserve_new()
            credentials = self._credentials("bot-one")
            save_credentials(reserved.credentials_file, credentials)
            registered = registry.register(reserved, credentials)

            scoped = registry.account_config(config, registered)

            self.assertIsInstance(scoped, AppConfig)
            self.assertEqual(scoped.runtime_dir, registered.runtime_dir)
            self.assertEqual(
                scoped.ilink_credentials_file, registered.credentials_file
            )
            self.assertEqual(scoped.ilink_state_file, registered.state_file)
            self.assertEqual(scoped.account_runtime_dir, registered.runtime_dir)
            self.assertEqual(scoped.projects, config.projects)
            self.assertEqual(scoped.chatgpt_profile_dir, config.chatgpt_profile_dir)


if __name__ == "__main__":
    unittest.main()
