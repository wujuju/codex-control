from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    QObject,
    Property,
    QPersistentModelIndex,
    QTimer,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QWindow
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from .app import BridgeApp
from .config import AppConfig, load_config
from .ilink_auth import load_credentials
from .instance_lock import AlreadyRunningError, InstanceLock, acquire_instance_lock
from .onboarding import ensure_logins


log = logging.getLogger(__name__)


class ConversationModel(QAbstractListModel):
    NameRole = Qt.ItemDataRole.UserRole + 1
    TypeRole = NameRole + 1
    PreviewRole = NameRole + 2
    ActiveRole = NameRole + 3

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._item = {
            "name": "微信 iLink Bot",
            "type": "Bot 私聊",
            "preview": "等待微信消息",
            "active": True,
        }

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()
    ) -> int:
        return 0 if parent.isValid() else 1

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or index.row() != 0:
            return None
        names = {
            self.NameRole: "name",
            self.TypeRole: "type",
            self.PreviewRole: "preview",
            self.ActiveRole: "active",
        }
        key = names.get(role)
        return self._item.get(key) if key else None

    def roleNames(self) -> dict[int, QByteArray]:
        return {
            self.NameRole: QByteArray(b"name"),
            self.TypeRole: QByteArray(b"chatType"),
            self.PreviewRole: QByteArray(b"preview"),
            self.ActiveRole: QByteArray(b"active"),
        }

    def set_preview(self, text: str) -> None:
        preview = " ".join(text.split())
        if len(preview) > 30:
            preview = preview[:29] + "…"
        if preview == self._item["preview"]:
            return
        self._item["preview"] = preview
        index = self.index(0, 0)
        self.dataChanged.emit(index, index, [self.PreviewRole])


class MessageModel(QAbstractListModel):
    KindRole = Qt.ItemDataRole.UserRole + 1
    SenderRole = KindRole + 1
    TextRole = KindRole + 2
    TimeRole = KindRole + 3
    OutgoingRole = KindRole + 4

    def __init__(self) -> None:
        super().__init__()
        self._items: list[dict[str, Any]] = []

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()
    ) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        names = {
            self.KindRole: "kind",
            self.SenderRole: "sender",
            self.TextRole: "text",
            self.TimeRole: "time",
            self.OutgoingRole: "outgoing",
        }
        key = names.get(role)
        return self._items[index.row()].get(key) if key else None

    def roleNames(self) -> dict[int, QByteArray]:
        return {
            self.KindRole: QByteArray(b"kind"),
            self.SenderRole: QByteArray(b"sender"),
            self.TextRole: QByteArray(b"messageText"),
            self.TimeRole: QByteArray(b"messageTime"),
            self.OutgoingRole: QByteArray(b"outgoing"),
        }

    def add_message(self, kind: str, sender: str, text: str) -> None:
        from datetime import datetime

        if len(self._items) >= 500:
            self.beginRemoveRows(QModelIndex(), 0, 0)
            self._items.pop(0)
            self.endRemoveRows()
        row = len(self._items)
        self.beginInsertRows(QModelIndex(), row, row)
        self._items.append(
            {
                "kind": kind,
                "sender": sender,
                "text": text,
                "time": datetime.now().strftime("%H:%M"),
                "outgoing": kind == "outgoing",
            }
        )
        self.endInsertRows()

    @Slot()
    def clear(self) -> None:
        if not self._items:
            return
        self.beginResetModel()
        self._items.clear()
        self.endResetModel()


class BridgeController(QObject):
    statusChanged = Signal()
    runningChanged = Signal()
    bridgeEvent = Signal(str, str, str)
    bridgeState = Signal(str, str)

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self.conversations = ConversationModel(config)
        self.messages = MessageModel()
        self._status = "准备启动"
        self._state = "stopped"
        self._bridge: BridgeApp | None = None
        self._thread: threading.Thread | None = None
        self._stop_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.bridgeEvent.connect(
            self._apply_event, Qt.ConnectionType.QueuedConnection
        )
        self.bridgeState.connect(
            self._apply_state, Qt.ConnectionType.QueuedConnection
        )
        self.messages.add_message("system", "系统", "界面已启动，正在连接微信 iLink")

    @Property(str, notify=statusChanged)
    def statusText(self) -> str:
        return self._status

    @Property(str, notify=statusChanged)
    def statusState(self) -> str:
        return self._state

    @Property(bool, notify=runningChanged)
    def running(self) -> bool:
        return self._state in {"connecting", "running", "reconnecting", "stopping"}

    @Property(str, constant=True)
    def conversationTitle(self) -> str:
        return "微信 iLink Bot"

    @Property(str, constant=True)
    def conversationType(self) -> str:
        return "Bot 私聊"

    @Property(str, constant=True)
    def policyText(self) -> str:
        return "仅响应已授权的 iLink 用户 ID"

    @Slot()
    def startBridge(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._stop_thread is not None and self._stop_thread.is_alive():
                return
            try:
                bridge = BridgeApp(
                    self.config,
                    event_sink=self.bridgeEvent.emit,
                    state_sink=self.bridgeState.emit,
                )
            except Exception as exc:
                log.exception("桥接器初始化失败")
                self._set_state("error", f"启动失败：{exc}")
                self.messages.add_message("system", "系统", f"启动失败：{exc}")
                return
            self._bridge = bridge
            self._thread = threading.Thread(
                target=bridge.run,
                name="wechat-bridge",
                daemon=True,
            )
            self._thread.start()

    @Slot()
    def stopBridge(self) -> None:
        with self._lock:
            bridge = self._bridge
            if bridge is None:
                return
            if self._stop_thread is not None and self._stop_thread.is_alive():
                return
            stop_thread = threading.Thread(
                target=self._stop_bridge_worker,
                args=(bridge,),
                name="wechat-bridge-stop",
                daemon=True,
            )
            self._stop_thread = stop_thread
        self._set_state("stopping", "正在停止")
        stop_thread.start()

    def _stop_bridge_worker(self, bridge: BridgeApp) -> None:
        try:
            bridge.stop()
        except Exception:
            log.exception("停止桥接器失败")
        finally:
            with self._lock:
                if self._stop_thread is threading.current_thread():
                    self._stop_thread = None

    @Slot(str)
    def sendMessage(self, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        bridge = self._bridge
        if bridge is None or self._state != "running":
            self.messages.add_message("system", "系统", "桥接尚未运行，消息未发送")
            return
        bridge.enqueue_message(cleaned)

    @Slot()
    def clearMessages(self) -> None:
        self.messages.clear()

    @Slot(str, str, str)
    def _apply_event(self, kind: str, sender: str, text: str) -> None:
        self.messages.add_message(kind, sender, text)
        self.conversations.set_preview(text)

    @Slot(str, str)
    def _apply_state(self, state: str, text: str) -> None:
        self._set_state(state, text)
        if state in {"running", "reconnecting", "stopped"}:
            self.messages.add_message("system", "系统", text)

    def _set_state(self, state: str, text: str) -> None:
        was_running = self.running
        changed = state != self._state or text != self._status
        self._state = state
        self._status = text
        if changed:
            self.statusChanged.emit()
        if was_running != self.running:
            self.runningChanged.emit()

    def shutdown(self) -> None:
        # aboutToQuit runs on Qt's UI thread. Network and browser cleanup can
        # block, so only dispatch the stop request here.
        self.stopBridge()

    def wait_for_shutdown(self, timeout_seconds: float = 30.0) -> None:
        """Finish cleanup after Qt's event loop has stopped rendering the UI."""
        self.stopBridge()
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._lock:
            stop_thread = self._stop_thread
            bridge_thread = self._thread
        for worker in (stop_thread, bridge_thread):
            if worker is None or worker is threading.current_thread():
                continue
            remaining = max(0.0, deadline - time.monotonic())
            worker.join(timeout=remaining)
        if bridge_thread is not None and bridge_thread.is_alive():
            log.warning("等待桥接线程退出超时；进程退出时将强制回收")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="微信 Codex QML 桌面端")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument(
        "--skip-login-check",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def _has_interactive_console() -> bool:
    stdin = getattr(sys, "stdin", None)
    stdout = getattr(sys, "stdout", None)
    try:
        return bool(stdin and stdout and stdin.isatty() and stdout.isatty())
    except (AttributeError, OSError):
        return False


def _prepare_gui_logins(
    config: AppConfig,
    *,
    skip_login_check: bool,
    interactive_console: bool,
) -> None:
    if skip_login_check:
        log.info("登录检查已由控制台启动器完成")
        return
    if interactive_console:
        ensure_logins(config)
        return

    missing: list[str] = []
    if load_credentials(config.ilink_credentials_file) is None:
        missing.append("微信 iLink")
    profile = config.chatgpt_profile_dir
    if not profile.is_dir() or not any(profile.iterdir()):
        missing.append("ChatGPT Plus")
    if missing:
        names = "、".join(missing)
        raise RuntimeError(
            f"未找到 {names} 登录信息。请从项目目录运行 start.ps1（或 start.cmd），"
            "在可见控制台中完成首次登录。"
        )
    log.info("无交互控制台；使用已保存的登录信息启动 GUI")


def _prepare_gui_runtime(
    config: AppConfig,
    *,
    skip_login_check: bool,
    interactive_console: bool,
) -> InstanceLock:
    # Acquire before onboarding: an interactive check may open the same
    # persistent browser profile used by an already-running instance.
    instance_lock = acquire_instance_lock(config.runtime_dir)
    try:
        _prepare_gui_logins(
            config,
            skip_login_check=skip_login_check,
            interactive_console=interactive_console,
        )
    except BaseException:
        instance_lock.release()
        raise
    return instance_lock


def _install_system_tray(
    app: QApplication, window: QWindow
) -> QSystemTrayIcon | None:
    """Hide minimized windows from the taskbar and provide a restore path."""
    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.warning("系统托盘不可用；最小化后仍会显示在任务栏")
        return None

    icon = _create_app_icon()
    app.setWindowIcon(icon)
    window.setIcon(icon)
    app.setQuitOnLastWindowClosed(False)

    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip("微信 Codex 控制台")
    menu = QMenu()
    show_action = menu.addAction("显示窗口")
    quit_action = menu.addAction("退出")
    tray.setContextMenu(menu)

    notification_shown = False

    def show_window() -> None:
        tray.show()
        window.showNormal()
        window.raise_()
        window.requestActivate()

    def handle_activation(reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            show_window()

    def handle_visibility(visibility: QWindow.Visibility) -> None:
        nonlocal notification_shown
        if visibility == QWindow.Visibility.Minimized:
            tray.show()
            if not notification_shown:
                tray.showMessage(
                    "微信 Codex 控制台",
                    "程序仍在后台运行，双击托盘图标可重新打开界面。",
                    QSystemTrayIcon.MessageIcon.Information,
                    3000,
                )
                notification_shown = True
            QTimer.singleShot(0, window.hide)

    show_action.triggered.connect(show_window)
    quit_action.triggered.connect(app.quit)
    tray.activated.connect(handle_activation)
    window.visibilityChanged.connect(handle_visibility)
    tray.show()
    app.processEvents()
    log.info("系统托盘图标已显示；最小化后可双击恢复窗口")

    # Keep Python callbacks and the menu alive for as long as the tray icon.
    tray._menu = menu  # type: ignore[attr-defined]
    tray._show_window = show_window  # type: ignore[attr-defined]
    tray._handle_activation = handle_activation  # type: ignore[attr-defined]
    tray._handle_visibility = handle_visibility  # type: ignore[attr-defined]
    return tray


def _create_app_icon() -> QIcon:
    """Load icon.png, with a generated icon as a safe fallback."""
    candidates = (
        Path.cwd() / "icon.png",
        Path(__file__).resolve().parents[2] / "icon.png",
        Path(sys.executable).resolve().parent / "icon.png",
    )
    for path in dict.fromkeys(candidates):
        if not path.is_file():
            continue
        icon = QIcon(str(path))
        if not icon.isNull() and not icon.pixmap(32, 32).isNull():
            log.info("使用应用图标：%s", path)
            return icon
        log.warning("无法读取应用图标：%s", path)

    log.warning("找不到可用的 icon.png，改用内置托盘图标")
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#2f7df6"))
    painter.drawRoundedRect(4, 6, 56, 46, 14, 14)
    painter.setBrush(QColor("#ffffff"))
    painter.drawEllipse(17, 26, 7, 7)
    painter.drawEllipse(29, 26, 7, 7)
    painter.drawEllipse(41, 26, 7, 7)
    painter.setBrush(QColor("#22c55e"))
    painter.drawEllipse(43, 43, 17, 17)
    painter.end()
    return QIcon(pixmap)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    QQuickStyle.setStyle("Basic")
    app = QApplication(sys.argv[:1])
    app.setApplicationName("WeChat Codex Control")
    app.setOrganizationName("Sam")

    instance_lock: InstanceLock | None = None
    try:
        config = load_config(args.config)
        config.runtime_dir.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] [%(levelname)s] %(message)s",
            handlers=[
                RotatingFileHandler(
                    config.runtime_dir / "gui.log",
                    maxBytes=5 * 1024 * 1024,
                    backupCount=3,
                    encoding="utf-8",
                )
            ],
        )
        instance_lock = _prepare_gui_runtime(
            config,
            skip_login_check=args.skip_login_check,
            interactive_console=_has_interactive_console(),
        )
    except AlreadyRunningError as exc:
        log.error("GUI 拒绝启动第二个实例：%s", exc)
        QMessageBox.warning(None, "微信 Codex 已在运行", str(exc))
        return 1
    except Exception as exc:
        log.exception("GUI 启动前检查失败")
        QMessageBox.critical(
            None,
            "微信 Codex 无法启动",
            f"{exc}\n\n请在项目目录的可见终端中运行 start.ps1 或 start.cmd。",
        )
        return 1

    tray: QSystemTrayIcon | None = None
    try:
        controller = BridgeController(config)
        engine = QQmlApplicationEngine()
        engine.setInitialProperties(
            {
                "bridge": controller,
                "conversationModel": controller.conversations,
                "messageModel": controller.messages,
            }
        )

        qml_path = Path(__file__).resolve().parent / "qml" / "Main.qml"
        engine.load(qml_path.as_uri())
        if not engine.rootObjects():
            return 1
        window = engine.rootObjects()[0]
        if not isinstance(window, QWindow):
            log.error("QML 根对象不是窗口")
            return 1
        tray = _install_system_tray(app, window)

        app.aboutToQuit.connect(controller.shutdown)
        QTimer.singleShot(0, controller.startBridge)
        exit_code = app.exec()
        # The window is already gone, so waiting here cannot freeze visible UI;
        # retain the instance lock until browser/process cleanup is complete.
        controller.wait_for_shutdown()
        return exit_code
    finally:
        if tray is not None:
            tray.hide()
        instance_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
