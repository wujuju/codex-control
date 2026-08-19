from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .config import AppConfig
from .ilink_api import ILinkCredentials, ILinkProtocolError
from .ilink_auth import load_credentials, save_credentials
from .state_io import atomic_write_text


REGISTRY_FILENAME = "wechat-accounts.json"
REGISTRY_VERSION = 1
_LEGACY_KEY = "legacy"
_ACCOUNT_FILENAME = "ilink-account.json"
_STATE_FILENAME = "ilink-state.json"


log = logging.getLogger(__name__)


class AccountRegistryError(ValueError):
    """Raised when account registry data is malformed or unsafe."""


@dataclass(frozen=True, slots=True)
class AccountRecord:
    key: str
    account_id: str
    user_id: str
    runtime_dir: Path
    credentials_file: Path
    state_file: Path
    legacy: bool = False


class AccountRegistry:
    """Persistent registry for independently stateful WeChat iLink accounts.

    ``reserve_new`` only allocates a safe directory.  The caller completes the
    QR login into ``record.credentials_file`` and then calls ``register``.
    This keeps cancelled and failed QR attempts out of the visible account
    list.
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.runtime_dir = Path(config.runtime_dir).expanduser().resolve()
        self.registry_file = self.runtime_dir / REGISTRY_FILENAME
        self._legacy_credentials_file = Path(
            config.ilink_credentials_file
        ).expanduser().resolve()
        self._legacy_state_file = Path(config.ilink_state_file).expanduser().resolve()
        self._records: list[AccountRecord] = []
        self.reload()

    def list_accounts(self) -> list[AccountRecord]:
        """Return a stable snapshot in registry display order."""

        return list(self._records)

    def reload(self) -> list[AccountRecord]:
        """Reload and validate the registry, importing legacy login once."""

        records = self._read_registry() if self.registry_file.is_file() else []
        changed = self._append_legacy_if_needed(records)
        changed = self._append_orphaned_accounts(records) or changed
        if changed:
            self._write_registry(records)
        self._records = records
        return self.list_accounts()

    def reserve_new(self) -> AccountRecord:
        """Create an unregistered per-account directory for a QR login."""

        accounts_dir = self.runtime_dir / "accounts"
        accounts_dir.mkdir(parents=True, exist_ok=True)
        resolved_accounts_dir = accounts_dir.resolve()
        self._require_within(
            resolved_accounts_dir,
            self.runtime_dir,
            "accounts directory",
        )

        while True:
            key = uuid.uuid4().hex
            account_runtime = resolved_accounts_dir / key
            try:
                account_runtime.mkdir(mode=0o700)
            except FileExistsError:
                continue
            break

        return AccountRecord(
            key=key,
            account_id="",
            user_id="",
            runtime_dir=account_runtime,
            credentials_file=account_runtime / _ACCOUNT_FILENAME,
            state_file=account_runtime / _STATE_FILENAME,
            legacy=False,
        )

    def register(
        self,
        record: AccountRecord,
        credentials: ILinkCredentials,
    ) -> AccountRecord:
        """Register a completed login and return the canonical account record.

        Account IDs are unique.  Logging in to an already registered WeChat
        account returns its existing record instead of adding a duplicate.
        """

        self._validate_record_paths(record, allow_empty_identity=True)
        account_id = self._required_text(credentials.account_id, "account_id")
        user_id = self._required_text(credentials.user_id, "user_id")

        saved_credentials = self._load_checked_credentials(record.credentials_file)
        if (
            saved_credentials.account_id != account_id
            or saved_credentials.user_id != user_id
            or saved_credentials.token != credentials.token
            or saved_credentials.base_url != credentials.base_url
        ):
            raise AccountRegistryError(
                "The saved credentials do not match the completed QR login"
            )

        # Merge against the latest on-disk snapshot so a second GUI action does
        # not unknowingly overwrite accounts added since this object was made.
        records = self._read_registry() if self.registry_file.is_file() else []
        registry_changed = self._append_legacy_if_needed(records)
        registry_changed = (
            self._append_orphaned_accounts(records, ignored_key=record.key)
            or registry_changed
        )
        for existing in records:
            if existing.account_id != account_id:
                continue
            if existing.user_id != user_id:
                raise AccountRegistryError(
                    "该微信账号的授权者与已有记录不一致，未覆盖原凭证"
                )
            # Scanning an existing account is the GUI re-authorization path.
            # Keep its state directory and atomically refresh only credentials.
            save_credentials(existing.credentials_file, credentials)
            if registry_changed:
                self._write_registry(records)
            self._records = records
            return existing

        for existing in records:
            if existing.key == record.key:
                raise AccountRegistryError(
                    f"Account key is already registered: {record.key}"
                )

        registered = AccountRecord(
            key=record.key,
            account_id=account_id,
            user_id=user_id,
            runtime_dir=record.runtime_dir.resolve(),
            credentials_file=record.credentials_file.resolve(),
            state_file=record.state_file.resolve(),
            legacy=record.legacy,
        )
        self._validate_record_paths(registered)
        records.append(registered)
        self._validate_unique(records)
        self._write_registry(records)
        self._records = records
        return registered

    def discard_reservation(self, record: AccountRecord) -> None:
        """Remove files created for an unregistered, known-safe reservation."""

        self._validate_record_paths(record, allow_empty_identity=True)
        if record.legacy or any(item.key == record.key for item in self._records):
            return
        for path in (record.credentials_file, record.state_file):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.warning("清理未登记微信账号文件失败：%s", path, exc_info=True)
        try:
            record.runtime_dir.rmdir()
        except OSError:
            # Preserve unexpected files rather than recursively deleting a
            # directory that may contain diagnostic or user-owned state.
            pass

    def account_config(
        self,
        config: AppConfig,
        record: AccountRecord,
    ) -> AppConfig:
        """Return an AppConfig scoped to one account's independent state.

        ChatGPT Plus is intentionally owned by the application-level shared
        runner; this config scopes only account-owned runtime and iLink files.
        """

        self._validate_record_paths(record)
        return replace(
            config,
            ilink_credentials_file=record.credentials_file,
            ilink_state_file=record.state_file,
            account_runtime_dir=record.runtime_dir,
        )

    def _read_registry(self) -> list[AccountRecord]:
        self._validate_registry_location()
        try:
            raw = json.loads(self.registry_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AccountRegistryError(
                f"Invalid account registry JSON: {self.registry_file}"
            ) from exc
        if not isinstance(raw, dict):
            raise AccountRegistryError("Account registry root must be a JSON object")
        version = raw.get("version")
        if isinstance(version, bool) or version != REGISTRY_VERSION:
            raise AccountRegistryError(f"Unsupported account registry version: {version!r}")
        items = raw.get("accounts")
        if not isinstance(items, list):
            raise AccountRegistryError("Account registry 'accounts' must be a list")

        records = [self._record_from_json(item, index) for index, item in enumerate(items)]
        self._validate_unique(records)
        return records

    def _record_from_json(self, raw: Any, index: int) -> AccountRecord:
        if not isinstance(raw, dict):
            raise AccountRegistryError(f"Account entry {index + 1} must be an object")
        required = {
            "key",
            "account_id",
            "user_id",
            "runtime_dir",
            "credentials_file",
            "state_file",
            "legacy",
        }
        missing = required - raw.keys()
        if missing:
            names = ", ".join(sorted(missing))
            raise AccountRegistryError(
                f"Account entry {index + 1} is missing fields: {names}"
            )
        legacy = raw["legacy"]
        if not isinstance(legacy, bool):
            raise AccountRegistryError(
                f"Account entry {index + 1} field 'legacy' must be a boolean"
            )
        record = AccountRecord(
            key=self._required_text(raw["key"], "key"),
            account_id=self._required_text(raw["account_id"], "account_id"),
            user_id=self._required_text(raw["user_id"], "user_id"),
            runtime_dir=self._path_from_json(raw["runtime_dir"], "runtime_dir"),
            credentials_file=self._path_from_json(
                raw["credentials_file"], "credentials_file"
            ),
            state_file=self._path_from_json(raw["state_file"], "state_file"),
            legacy=legacy,
        )
        self._validate_record_paths(record)
        return record

    def _append_legacy_if_needed(self, records: list[AccountRecord]) -> bool:
        if not self._legacy_credentials_file.is_file():
            return False
        legacy_index = next(
            (index for index, record in enumerate(records) if record.key == _LEGACY_KEY),
            None,
        )
        try:
            credentials = self._load_checked_credentials(self._legacy_credentials_file)
        except AccountRegistryError:
            # Once registered, keep the row visible so only this controller
            # reports its damaged credentials instead of hiding all accounts.
            if legacy_index is not None:
                return False
            raise
        if legacy_index is not None:
            current = records[legacy_index]
            if (
                current.account_id == credentials.account_id
                and current.user_id == credentials.user_id
            ):
                return False
            records[legacy_index] = AccountRecord(
                key=_LEGACY_KEY,
                account_id=credentials.account_id,
                user_id=credentials.user_id,
                runtime_dir=self.runtime_dir,
                credentials_file=self._legacy_credentials_file,
                state_file=self._legacy_state_file,
                legacy=True,
            )
            self._validate_unique(records)
            return True
        if any(record.account_id == credentials.account_id for record in records):
            return False
        legacy = AccountRecord(
            key=_LEGACY_KEY,
            account_id=credentials.account_id,
            user_id=credentials.user_id,
            runtime_dir=self.runtime_dir,
            credentials_file=self._legacy_credentials_file,
            state_file=self._legacy_state_file,
            legacy=True,
        )
        self._validate_record_paths(legacy)
        records.insert(0, legacy)
        self._validate_unique(records)
        return True

    def _append_orphaned_accounts(
        self,
        records: list[AccountRecord],
        *,
        ignored_key: str = "",
    ) -> bool:
        """Recover completed QR logins not yet published to the registry."""

        accounts_dir = (self.runtime_dir / "accounts").resolve()
        if not accounts_dir.is_dir():
            return False
        known_keys = {record.key for record in records}
        known_ids = {record.account_id for record in records}
        changed = False
        for directory in accounts_dir.iterdir():
            if not directory.is_dir() or directory.name == ignored_key:
                continue
            key = directory.name
            if key in known_keys:
                continue
            try:
                parsed_key = uuid.UUID(key)
            except (ValueError, AttributeError):
                continue
            if parsed_key.hex != key:
                continue
            credentials_file = directory / _ACCOUNT_FILENAME
            try:
                credentials = self._load_checked_credentials(credentials_file)
            except AccountRegistryError:
                continue
            if credentials.account_id in known_ids:
                continue
            recovered = AccountRecord(
                key=key,
                account_id=credentials.account_id,
                user_id=credentials.user_id,
                runtime_dir=directory.resolve(),
                credentials_file=credentials_file.resolve(),
                state_file=(directory / _STATE_FILENAME).resolve(),
                legacy=False,
            )
            self._validate_record_paths(recovered)
            records.append(recovered)
            known_keys.add(key)
            known_ids.add(credentials.account_id)
            changed = True
        if changed:
            self._validate_unique(records)
        return changed

    def _write_registry(self, records: list[AccountRecord]) -> None:
        self._validate_unique(records)
        payload = {
            "version": REGISTRY_VERSION,
            "accounts": [self._record_to_json(record) for record in records],
        }
        atomic_write_text(
            self.registry_file,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    def _record_to_json(self, record: AccountRecord) -> dict[str, Any]:
        self._validate_record_paths(record)
        return {
            "key": record.key,
            "account_id": record.account_id,
            "user_id": record.user_id,
            "runtime_dir": self._path_to_json(record.runtime_dir),
            "credentials_file": self._path_to_json(record.credentials_file),
            "state_file": self._path_to_json(record.state_file),
            "legacy": record.legacy,
        }

    def _validate_record_paths(
        self,
        record: AccountRecord,
        *,
        allow_empty_identity: bool = False,
    ) -> None:
        key = self._required_text(record.key, "key")
        if not allow_empty_identity:
            self._required_text(record.account_id, "account_id")
            self._required_text(record.user_id, "user_id")

        runtime = Path(record.runtime_dir).resolve()
        credentials = Path(record.credentials_file).resolve()
        state = Path(record.state_file).resolve()
        if record.legacy:
            if key != _LEGACY_KEY:
                raise AccountRegistryError("Legacy account key must be 'legacy'")
            expected = (
                self.runtime_dir,
                self._legacy_credentials_file,
                self._legacy_state_file,
            )
        else:
            try:
                parsed_key = uuid.UUID(key)
            except (ValueError, AttributeError) as exc:
                raise AccountRegistryError(f"Invalid account key: {key!r}") from exc
            if parsed_key.hex != key:
                raise AccountRegistryError(f"Account key must be a lowercase UUID: {key!r}")
            account_runtime = (self.runtime_dir / "accounts" / key).resolve()
            self._require_within(account_runtime, self.runtime_dir, "account runtime")
            expected = (
                account_runtime,
                account_runtime / _ACCOUNT_FILENAME,
                account_runtime / _STATE_FILENAME,
            )
        if (runtime, credentials, state) != expected:
            raise AccountRegistryError(
                f"Unsafe or unexpected paths for account {record.key!r}"
            )

    @staticmethod
    def _required_text(value: Any, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise AccountRegistryError(f"Account field {field_name!r} must be non-empty text")
        cleaned = value.strip()
        if len(cleaned) > 512 or any(ord(character) < 32 for character in cleaned):
            raise AccountRegistryError(f"Account field {field_name!r} contains invalid text")
        return cleaned

    def _path_from_json(self, value: Any, field_name: str) -> Path:
        text = self._required_text(value, field_name)
        if "\x00" in text or text.startswith("~"):
            raise AccountRegistryError(f"Invalid registry path in {field_name!r}")
        path = Path(text)
        if not path.is_absolute():
            path = self.runtime_dir / path
        return path.resolve()

    def _path_to_json(self, path: Path) -> str:
        resolved = Path(path).resolve()
        try:
            relative = resolved.relative_to(self.runtime_dir)
        except ValueError:
            return str(resolved)
        return "." if not relative.parts else relative.as_posix()

    @staticmethod
    def _require_within(path: Path, parent: Path, label: str) -> None:
        if path != parent and parent not in path.parents:
            raise AccountRegistryError(f"{label} escapes the global runtime directory")

    def _validate_registry_location(self) -> None:
        if self.registry_file.is_symlink():
            raise AccountRegistryError("Account registry file must not be a symbolic link")
        self._require_within(
            self.registry_file.parent.resolve(), self.runtime_dir, "account registry"
        )

    @staticmethod
    def _validate_unique(records: list[AccountRecord]) -> None:
        keys: set[str] = set()
        account_ids: set[str] = set()
        for record in records:
            if record.key in keys:
                raise AccountRegistryError(f"Duplicate account key: {record.key}")
            if record.account_id in account_ids:
                raise AccountRegistryError(
                    f"Duplicate account_id in registry: {record.account_id}"
                )
            keys.add(record.key)
            account_ids.add(record.account_id)

    @staticmethod
    def _load_checked_credentials(path: Path) -> ILinkCredentials:
        try:
            credentials = load_credentials(path)
        except ILinkProtocolError as exc:
            raise AccountRegistryError(str(exc)) from exc
        if credentials is None:
            raise AccountRegistryError(f"Account credentials file does not exist: {path}")
        return credentials
