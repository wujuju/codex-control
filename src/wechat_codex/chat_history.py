from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


HISTORY_FILENAME = "gui-chat-history.sqlite3"
HISTORY_SCHEMA_VERSION = "1"
PERSISTED_KINDS = frozenset({"incoming", "outgoing"})


class ChatHistoryError(RuntimeError):
    """Raised when account-owned GUI history cannot be used safely."""


@dataclass(frozen=True, slots=True)
class ChatMessage:
    message_id: str
    kind: str
    sender: str
    peer_id: str
    text: str
    occurred_at_ms: int
    content_type: str = "text"

    @classmethod
    def create(
        cls,
        *,
        message_id: str,
        kind: str,
        sender: str,
        peer_id: str,
        text: str,
        content_type: str = "text",
    ) -> ChatMessage:
        return cls(
            message_id=message_id,
            kind=kind,
            sender=sender,
            peer_id=peer_id,
            text=text,
            occurred_at_ms=int(time.time() * 1000),
            content_type=content_type,
        )


class ChatHistoryStore:
    """SQLite-backed message history owned by exactly one WeChat account."""

    def __init__(self, runtime_dir: Path, account_id: str) -> None:
        cleaned_account_id = account_id.strip()
        if not cleaned_account_id:
            raise ValueError("微信账号 ID 不能为空")
        self.account_id = cleaned_account_id
        self.path = Path(runtime_dir).resolve() / HISTORY_FILENAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._connection = sqlite3.connect(
                self.path,
                timeout=10,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA secure_delete = ON")
            self._initialize()
            os.chmod(self.path, 0o600)
        except (OSError, sqlite3.Error, ValueError) as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise ChatHistoryError(
                f"微信账号历史记录无法打开 {self.path}: {exc}"
            ) from exc

    def _initialize(self) -> None:
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('incoming', 'outgoing')),
                    sender TEXT NOT NULL,
                    peer_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    occurred_at_ms INTEGER NOT NULL CHECK (occurred_at_ms >= 0),
                    content_type TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS messages_chronological
                ON messages(occurred_at_ms DESC, message_id DESC)
                """
            )
            metadata = dict(
                self._connection.execute(
                    "SELECT key, value FROM metadata"
                ).fetchall()
            )
            if not metadata:
                message_count = self._connection.execute(
                    "SELECT COUNT(*) FROM messages"
                ).fetchone()[0]
                if message_count:
                    raise ValueError("历史记录数据库缺少账号绑定信息")
                self._connection.executemany(
                    "INSERT INTO metadata(key, value) VALUES (?, ?)",
                    (
                        ("schema_version", HISTORY_SCHEMA_VERSION),
                        ("account_id", self.account_id),
                    ),
                )
                return
            if metadata.get("schema_version") != HISTORY_SCHEMA_VERSION:
                raise ValueError("历史记录数据库版本不受支持")
            if metadata.get("account_id") != self.account_id:
                raise ValueError("历史记录数据库属于另一个微信账号")

    def append(self, message: ChatMessage) -> bool:
        self._validate_message(message)
        with self._lock:
            try:
                with self._connection:
                    cursor = self._connection.execute(
                        """
                        INSERT OR IGNORE INTO messages(
                            message_id,
                            kind,
                            sender,
                            peer_id,
                            text,
                            occurred_at_ms,
                            content_type
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            message.message_id,
                            message.kind,
                            message.sender,
                            message.peer_id,
                            message.text,
                            message.occurred_at_ms,
                            message.content_type,
                        ),
                    )
                return cursor.rowcount == 1
            except sqlite3.Error as exc:
                raise ChatHistoryError(
                    f"微信账号历史记录写入失败 {self.path}: {exc}"
                ) from exc

    def latest(self, limit: int) -> list[ChatMessage]:
        if limit <= 0:
            return []
        return self._query_page(limit=limit)

    def before(self, cursor: ChatMessage, limit: int) -> list[ChatMessage]:
        if limit <= 0:
            return []
        return self._query_page(
            limit=limit,
            before=(cursor.occurred_at_ms, cursor.message_id),
        )

    def _query_page(
        self,
        *,
        limit: int,
        before: tuple[int, str] | None = None,
    ) -> list[ChatMessage]:
        with self._lock:
            try:
                if before is None:
                    rows = self._connection.execute(
                        """
                        SELECT message_id, kind, sender, peer_id, text,
                               occurred_at_ms, content_type
                        FROM messages
                        ORDER BY occurred_at_ms DESC, message_id DESC
                        LIMIT ?
                        """,
                        (limit,),
                    ).fetchall()
                else:
                    rows = self._connection.execute(
                        """
                        SELECT message_id, kind, sender, peer_id, text,
                               occurred_at_ms, content_type
                        FROM messages
                        WHERE occurred_at_ms < ?
                           OR (occurred_at_ms = ? AND message_id < ?)
                        ORDER BY occurred_at_ms DESC, message_id DESC
                        LIMIT ?
                        """,
                        (before[0], before[0], before[1], limit),
                    ).fetchall()
            except sqlite3.Error as exc:
                raise ChatHistoryError(
                    f"微信账号历史记录读取失败 {self.path}: {exc}"
                ) from exc
        return [self._message_from_row(row) for row in reversed(rows)]

    def clear(self) -> None:
        with self._lock:
            try:
                with self._connection:
                    self._connection.execute("DELETE FROM messages")
                self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error as exc:
                raise ChatHistoryError(
                    f"微信账号历史记录清除失败 {self.path}: {exc}"
                ) from exc

    def count(self) -> int:
        with self._lock:
            try:
                row = self._connection.execute(
                    "SELECT COUNT(*) AS message_count FROM messages"
                ).fetchone()
            except sqlite3.Error as exc:
                raise ChatHistoryError(
                    f"微信账号历史记录统计失败 {self.path}: {exc}"
                ) from exc
        return int(row["message_count"])

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _validate_message(message: ChatMessage) -> None:
        if message.kind not in PERSISTED_KINDS:
            raise ValueError(f"不能持久化界面消息类型：{message.kind!r}")
        if not message.message_id or not message.peer_id or not message.content_type:
            raise ValueError("持久化界面消息缺少标识、对端用户或内容类型")
        if message.occurred_at_ms < 0:
            raise ValueError("持久化界面消息时间无效")

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> ChatMessage:
        return ChatMessage(
            message_id=str(row["message_id"]),
            kind=str(row["kind"]),
            sender=str(row["sender"]),
            peer_id=str(row["peer_id"]),
            text=str(row["text"]),
            occurred_at_ms=int(row["occurred_at_ms"]),
            content_type=str(row["content_type"]),
        )
