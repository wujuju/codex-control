pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: window

    required property var accountManager
    required property var accountModel

    readonly property var currentBridge: accountManager ? accountManager.currentBridge : null
    readonly property var currentConversationModel: accountManager ? accountManager.currentConversationModel : null
    readonly property var currentMessageModel: accountManager ? accountManager.currentMessageModel : null

    property int currentPage: 0
    property color accent: "#2f7df6"
    property color sidebar: "#151922"
    property color sidebarMuted: "#8f98aa"

    width: 1080
    height: 720
    minimumWidth: 820
    minimumHeight: 520
    visible: true
    title: currentPage === 0 ? "微信 Codex 账号" : "微信 Codex 控制台"
    color: "#f3f5f8"

    function statusColor(state) {
        if (state === "running")
            return "#38d996"
        if (state === "connecting" || state === "reconnecting" || state === "stopping")
            return "#f0b44d"
        if (state === "error")
            return "#ef6262"
        return "#7c8799"
    }

    function openAccount(accountKey) {
        if (!accountManager)
            return
        accountManager.selectAccount(accountKey)
        currentPage = 1
    }

    function beginAddAccount() {
        if (!accountManager || accountManager.adding || accountManager.loginBusy)
            return
        verifyCodeField.clear()
        addAccountDialog.open()
        accountManager.beginAddAccount()
    }

    Connections {
        target: window.accountManager
        enabled: window.accountManager !== null
        ignoreUnknownSignals: true

        function onAddingChanged() {
            if (!window.accountManager.adding && addAccountDialog.visible)
                addAccountDialog.close()
        }

        function onLoginChanged() {
            if (!window.accountManager.adding && addAccountDialog.visible)
                addAccountDialog.close()
        }

        function onCurrentChanged() {
            composer.clear()
        }
    }

    StackLayout {
        anchors.fill: parent
        currentIndex: window.currentPage

        Rectangle {
            color: "#f3f5f8"

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 28
                spacing: 20

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 14

                    Rectangle {
                        Layout.preferredWidth: 48
                        Layout.preferredHeight: 48
                        radius: 15
                        color: window.accent

                        Text {
                            anchors.centerIn: parent
                            text: "C"
                            color: "white"
                            font.pixelSize: 23
                            font.bold: true
                        }
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 3

                        Text {
                            text: "微信账号"
                            color: "#151a23"
                            font.pixelSize: 24
                            font.bold: true
                        }
                        Text {
                            text: "管理已登录的微信；所有账号共用当前 ChatGPT Plus 登录"
                            color: "#707b8b"
                            font.pixelSize: 13
                        }
                    }

                    Button {
                        id: addAccountButton
                        text: window.accountManager && window.accountManager.loginBusy ? "正在处理…" : "添加微信"
                        enabled: window.accountManager &&
                                 !window.accountManager.adding &&
                                 !window.accountManager.loginBusy
                        onClicked: window.beginAddAccount()

                        background: Rectangle {
                            radius: 10
                            color: addAccountButton.enabled ? window.accent : "#b9c3d1"
                        }
                        contentItem: Text {
                            text: addAccountButton.text
                            color: "white"
                            font.pixelSize: 14
                            font.bold: true
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                    }

                    Button {
                        text: "最小化"
                        flat: true
                        onClicked: window.showMinimized()
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    radius: 16
                    color: "white"
                    border.width: 1
                    border.color: "#e2e7ee"

                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 1
                        spacing: 0

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 52
                            color: "#f8fafc"
                            radius: 15

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 22
                                anchors.rightMargin: 22
                                spacing: 18

                                Text {
                                    Layout.fillWidth: true
                                    Layout.preferredWidth: 2
                                    text: "微信 / Bot ID"
                                    color: "#768193"
                                    font.pixelSize: 12
                                    font.bold: true
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: "Owner ID"
                                    color: "#768193"
                                    font.pixelSize: 12
                                    font.bold: true
                                }
                                Text {
                                    Layout.preferredWidth: 180
                                    text: "状态"
                                    color: "#768193"
                                    font.pixelSize: 12
                                    font.bold: true
                                }
                            }
                        }

                        Item {
                            Layout.fillWidth: true
                            Layout.fillHeight: true

                            ListView {
                                id: accountList
                                anchors.fill: parent
                                anchors.margins: 10
                                clip: true
                                spacing: 6
                                model: window.accountModel
                                boundsBehavior: Flickable.StopAtBounds
                                ScrollBar.vertical: ScrollBar {}

                                delegate: ItemDelegate {
                                    id: accountDelegate

                                    required property string accountKey
                                    required property string accountName
                                    required property string ownerId
                                    required property string statusText
                                    required property string statusState

                                    width: accountList.width
                                    height: 82
                                    padding: 0
                                    hoverEnabled: true
                                    onDoubleClicked: window.openAccount(accountDelegate.accountKey)

                                    ToolTip.visible: hovered
                                    ToolTip.text: "双击打开控制台"

                                    background: Rectangle {
                                        radius: 12
                                        color: accountDelegate.hovered ? "#f2f6fc" : "white"
                                        border.width: accountDelegate.hovered ? 1 : 0
                                        border.color: "#dce6f5"
                                    }

                                    contentItem: RowLayout {
                                        anchors.fill: parent
                                        anchors.leftMargin: 12
                                        anchors.rightMargin: 12
                                        spacing: 18

                                        RowLayout {
                                            Layout.fillWidth: true
                                            Layout.preferredWidth: 2
                                            spacing: 12

                                            Rectangle {
                                                Layout.preferredWidth: 44
                                                Layout.preferredHeight: 44
                                                radius: 14
                                                color: "#2d9b73"

                                                Text {
                                                    anchors.centerIn: parent
                                                    text: "微信"
                                                    color: "white"
                                                    font.pixelSize: 12
                                                    font.bold: true
                                                }
                                            }

                                            ColumnLayout {
                                                Layout.fillWidth: true
                                                spacing: 3

                                                Text {
                                                    Layout.fillWidth: true
                                                    text: accountDelegate.accountName.length > 0
                                                          ? accountDelegate.accountName : "微信 iLink Bot"
                                                    color: "#202630"
                                                    font.pixelSize: 15
                                                    font.bold: true
                                                    elide: Text.ElideRight
                                                }
                                                Text {
                                                    Layout.fillWidth: true
                                                    text: "双击进入该账号的控制台"
                                                    color: "#7c8797"
                                                    font.pixelSize: 11
                                                    elide: Text.ElideMiddle
                                                }
                                            }
                                        }

                                        Text {
                                            Layout.fillWidth: true
                                            text: accountDelegate.ownerId.length > 0 ? accountDelegate.ownerId : "—"
                                            color: "#4d5868"
                                            font.pixelSize: 13
                                            elide: Text.ElideMiddle
                                        }

                                        RowLayout {
                                            Layout.preferredWidth: 180
                                            spacing: 9

                                            Rectangle {
                                                Layout.preferredWidth: 10
                                                Layout.preferredHeight: 10
                                                radius: 5
                                                color: window.statusColor(accountDelegate.statusState)
                                            }
                                            Text {
                                                Layout.fillWidth: true
                                                text: accountDelegate.statusText
                                                color: "#4d5868"
                                                font.pixelSize: 13
                                                elide: Text.ElideRight
                                            }
                                        }
                                    }
                                }
                            }

                            Column {
                                anchors.centerIn: parent
                                spacing: 8
                                visible: accountList.count === 0

                                Text {
                                    anchors.horizontalCenter: parent.horizontalCenter
                                    text: "还没有登录的微信"
                                    color: "#4b5565"
                                    font.pixelSize: 17
                                    font.bold: true
                                }
                                Text {
                                    anchors.horizontalCenter: parent.horizontalCenter
                                    text: "点击“添加微信”，扫码后账号会显示在这里"
                                    color: "#9099a7"
                                    font.pixelSize: 13
                                }
                            }
                        }
                    }
                }

                Text {
                    Layout.alignment: Qt.AlignHCenter
                    text: "双击任意账号进入控制台"
                    color: "#8a93a2"
                    font.pixelSize: 12
                }
            }
        }

        Item {
            RowLayout {
                anchors.fill: parent
                spacing: 0

                Rectangle {
                    Layout.preferredWidth: 300
                    Layout.fillHeight: true
                    color: window.sidebar

                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 18
                        spacing: 14

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 10

                            Rectangle {
                                Layout.preferredWidth: 38
                                Layout.preferredHeight: 38
                                radius: 12
                                color: window.accent

                                Text {
                                    anchors.centerIn: parent
                                    text: "C"
                                    color: "white"
                                    font.pixelSize: 19
                                    font.bold: true
                                }
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 1

                                Text {
                                    text: "Codex Control"
                                    color: "white"
                                    font.pixelSize: 17
                                    font.bold: true
                                }
                                Text {
                                    text: "微信消息桥接"
                                    color: window.sidebarMuted
                                    font.pixelSize: 12
                                }
                            }
                        }

                        Button {
                            id: backButton
                            Layout.fillWidth: true
                            text: "‹ 返回账号列表"
                            flat: true
                            onClicked: window.currentPage = 0

                            contentItem: Text {
                                text: backButton.text
                                color: "#d7deea"
                                font.pixelSize: 13
                                horizontalAlignment: Text.AlignLeft
                                verticalAlignment: Text.AlignVCenter
                            }
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 1
                            color: "#292f3c"
                        }

                        Text {
                            text: "会话"
                            color: window.sidebarMuted
                            font.pixelSize: 12
                            font.bold: true
                        }

                        ListView {
                            id: conversationList
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            spacing: 6
                            model: window.currentConversationModel

                            delegate: ItemDelegate {
                                id: conversationDelegate

                                required property string name
                                required property string chatType
                                required property string preview
                                required property bool active

                                width: conversationList.width
                                height: 76
                                padding: 0
                                hoverEnabled: true

                                background: Rectangle {
                                    radius: 12
                                    color: conversationDelegate.active ? "#263143" :
                                           (conversationDelegate.hovered ? "#202632" : "transparent")
                                    border.width: conversationDelegate.active ? 1 : 0
                                    border.color: "#344258"
                                }

                                contentItem: RowLayout {
                                    anchors.fill: parent
                                    anchors.margins: 11
                                    spacing: 11

                                    Rectangle {
                                        Layout.preferredWidth: 42
                                        Layout.preferredHeight: 42
                                        radius: 14
                                        color: "#2d9b73"

                                        Text {
                                            anchors.centerIn: parent
                                            text: "Bot"
                                            color: "white"
                                            font.pixelSize: 14
                                            font.bold: true
                                        }
                                    }

                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        spacing: 4

                                        RowLayout {
                                            Layout.fillWidth: true

                                            Text {
                                                Layout.fillWidth: true
                                                text: conversationDelegate.name
                                                color: "white"
                                                font.pixelSize: 15
                                                font.bold: true
                                                elide: Text.ElideRight
                                            }
                                            Rectangle {
                                                Layout.preferredWidth: typeText.implicitWidth + 12
                                                Layout.preferredHeight: 20
                                                radius: 10
                                                color: "#344155"

                                                Text {
                                                    id: typeText
                                                    anchors.centerIn: parent
                                                    text: conversationDelegate.chatType
                                                    color: "#b8c4d8"
                                                    font.pixelSize: 10
                                                }
                                            }
                                        }
                                        Text {
                                            Layout.fillWidth: true
                                            text: conversationDelegate.preview
                                            color: window.sidebarMuted
                                            font.pixelSize: 12
                                            elide: Text.ElideRight
                                        }
                                    }
                                }
                            }
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 64
                            radius: 12
                            color: "#202632"

                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: 12
                                spacing: 10

                                Rectangle {
                                    Layout.preferredWidth: 10
                                    Layout.preferredHeight: 10
                                    radius: 5
                                    color: window.currentBridge
                                           ? window.statusColor(window.currentBridge.statusState)
                                           : "#7c8799"
                                }
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 2

                                    Text {
                                        Layout.fillWidth: true
                                        text: window.currentBridge ? window.currentBridge.statusText : "未选择微信账号"
                                        color: "#e7ebf2"
                                        font.pixelSize: 12
                                        elide: Text.ElideRight
                                    }
                                    Text {
                                        text: "最小化后继续运行"
                                        color: window.sidebarMuted
                                        font.pixelSize: 10
                                    }
                                }
                            }
                        }
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    color: "#f3f5f8"

                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 0

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 74
                            color: "white"
                            border.color: "#e5e9f0"
                            border.width: 1

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 24
                                anchors.rightMargin: 18
                                spacing: 12

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 3

                                    RowLayout {
                                        spacing: 8

                                        Text {
                                            text: window.currentBridge ? window.currentBridge.conversationTitle : "微信 iLink Bot"
                                            color: "#151a23"
                                            font.pixelSize: 19
                                            font.bold: true
                                        }
                                        Rectangle {
                                            Layout.preferredWidth: headerType.implicitWidth + 14
                                            Layout.preferredHeight: 22
                                            radius: 11
                                            color: "#edf3ff"

                                            Text {
                                                id: headerType
                                                anchors.centerIn: parent
                                                text: window.currentBridge ? window.currentBridge.conversationType : "Bot 私聊"
                                                color: window.accent
                                                font.pixelSize: 11
                                            }
                                        }
                                    }
                                    Text {
                                        text: window.currentBridge ? window.currentBridge.policyText : "请选择一个已登录的微信账号"
                                        color: "#7a8494"
                                        font.pixelSize: 12
                                    }
                                }

                                Button {
                                    text: window.currentBridge && window.currentBridge.running ? "停止" : "启动"
                                    enabled: window.currentBridge !== null
                                    onClicked: {
                                        if (!window.currentBridge)
                                            return
                                        if (window.currentBridge.running)
                                            window.currentBridge.stopBridge()
                                        else
                                            window.currentBridge.startBridge()
                                    }
                                }
                                Button {
                                    text: "清空"
                                    flat: true
                                    enabled: window.currentBridge !== null
                                    onClicked: {
                                        if (window.currentBridge)
                                            window.currentBridge.clearMessages()
                                    }
                                }
                                Button {
                                    text: "最小化"
                                    flat: true
                                    onClicked: window.showMinimized()
                                }
                            }
                        }

                        ListView {
                            id: messageList
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            Layout.leftMargin: 14
                            Layout.rightMargin: 14
                            Layout.topMargin: 10
                            Layout.bottomMargin: 8
                            spacing: 6
                            clip: true
                            model: window.currentMessageModel
                            boundsBehavior: Flickable.StopAtBounds
                            ScrollBar.vertical: ScrollBar {}

                            onCountChanged: positionViewAtEnd()

                            delegate: Item {
                                id: messageDelegate

                                required property string kind
                                required property string sender
                                required property string messageText
                                required property string messageTime
                                required property bool outgoing

                                width: messageList.width
                                height: messageDelegate.kind === "system" ? systemLabel.height + 14 : bubble.height + 24

                                Text {
                                    id: systemLabel
                                    visible: messageDelegate.kind === "system"
                                    anchors.horizontalCenter: parent.horizontalCenter
                                    topPadding: 5
                                    bottomPadding: 5
                                    text: messageDelegate.messageText
                                    color: "#8a93a2"
                                    font.pixelSize: 11
                                }

                                Rectangle {
                                    id: bubble
                                    visible: messageDelegate.kind !== "system"
                                    width: Math.min(Math.max(messageBody.implicitWidth + 28, 100), parent.width * 0.72)
                                    height: bubbleColumn.implicitHeight + 20
                                    x: messageDelegate.outgoing ? parent.width - width - 10 : 10
                                    radius: 14
                                    color: messageDelegate.outgoing ? window.accent : "white"
                                    border.width: messageDelegate.outgoing ? 0 : 1
                                    border.color: "#e1e6ee"

                                    ColumnLayout {
                                        id: bubbleColumn
                                        anchors.left: parent.left
                                        anchors.right: parent.right
                                        anchors.top: parent.top
                                        anchors.margins: 10
                                        spacing: 5

                                        Text {
                                            Layout.fillWidth: true
                                            text: messageDelegate.sender
                                            color: messageDelegate.outgoing ? "#dce9ff" : "#6e7888"
                                            font.pixelSize: 11
                                            font.bold: true
                                        }
                                        Text {
                                            id: messageBody
                                            Layout.fillWidth: true
                                            text: messageDelegate.messageText
                                            color: messageDelegate.outgoing ? "white" : "#202630"
                                            font.pixelSize: 14
                                            wrapMode: Text.Wrap
                                            textFormat: Text.PlainText
                                        }
                                        Text {
                                            Layout.alignment: Qt.AlignRight
                                            text: messageDelegate.messageTime
                                            color: messageDelegate.outgoing ? "#d3e2ff" : "#a0a8b4"
                                            font.pixelSize: 10
                                        }
                                    }
                                }
                            }
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 104
                            color: "white"
                            border.color: "#e2e7ee"
                            border.width: 1

                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: 14
                                spacing: 12

                                ScrollView {
                                    Layout.fillWidth: true
                                    Layout.fillHeight: true

                                    TextArea {
                                        id: composer
                                        placeholderText: "输入消息，Enter 发送，Shift+Enter 换行"
                                        wrapMode: TextArea.Wrap
                                        font.pixelSize: 14

                                        background: Rectangle {
                                            radius: 10
                                            color: "#f6f8fb"
                                            border.color: composer.activeFocus ? window.accent : "#dce2ea"
                                        }

                                        Keys.onReturnPressed: function(event) {
                                            if (event.modifiers & Qt.ShiftModifier) {
                                                event.accepted = false
                                            } else if (window.currentBridge &&
                                                       window.currentBridge.statusState === "running" &&
                                                       text.trim().length > 0) {
                                                window.currentBridge.sendMessage(text)
                                                clear()
                                                event.accepted = true
                                            } else {
                                                event.accepted = true
                                            }
                                        }
                                    }
                                }

                                Button {
                                    id: sendButton
                                    Layout.preferredWidth: 92
                                    Layout.fillHeight: true
                                    text: "发送"
                                    enabled: window.currentBridge &&
                                             composer.text.trim().length > 0 &&
                                             window.currentBridge.statusState === "running"
                                    onClicked: {
                                        if (!window.currentBridge)
                                            return
                                        window.currentBridge.sendMessage(composer.text)
                                        composer.clear()
                                    }

                                    background: Rectangle {
                                        radius: 10
                                        color: sendButton.enabled ? window.accent : "#b9c3d1"
                                    }
                                    contentItem: Text {
                                        text: sendButton.text
                                        color: "white"
                                        font.pixelSize: 14
                                        font.bold: true
                                        horizontalAlignment: Text.AlignHCenter
                                        verticalAlignment: Text.AlignVCenter
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    Dialog {
        id: addAccountDialog

        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(480, window.width - 40)
        modal: true
        focus: true
        title: "添加微信账号"
        closePolicy: Popup.NoAutoClose

        onClosed: verifyCodeField.clear()

        contentItem: ColumnLayout {
            spacing: 14

            Text {
                Layout.fillWidth: true
                text: "请使用微信扫描二维码完成登录"
                color: "#252b35"
                font.pixelSize: 15
                font.bold: true
                horizontalAlignment: Text.AlignHCenter
            }

            Rectangle {
                Layout.alignment: Qt.AlignHCenter
                Layout.preferredWidth: 292
                Layout.preferredHeight: 292
                radius: 12
                color: "#f6f8fb"
                border.width: 1
                border.color: "#e0e5ed"

                BusyIndicator {
                    anchors.centerIn: parent
                    running: window.accountManager &&
                             window.accountManager.loginBusy &&
                             qrImage.status !== Image.Ready
                }

                Image {
                    id: qrImage
                    anchors.fill: parent
                    anchors.margins: 12
                    source: window.accountManager ? window.accountManager.qrImageUrl : ""
                    sourceSize.width: 536
                    sourceSize.height: 536
                    fillMode: Image.PreserveAspectFit
                    asynchronous: true
                    cache: false
                }
            }

            Text {
                Layout.fillWidth: true
                text: window.accountManager ? window.accountManager.loginStatus : "正在准备二维码…"
                color: "#687384"
                font.pixelSize: 13
                wrapMode: Text.Wrap
                horizontalAlignment: Text.AlignHCenter
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                visible: window.accountManager && window.accountManager.verifyCodeRequired

                TextField {
                    id: verifyCodeField
                    Layout.fillWidth: true
                    placeholderText: "输入微信验证码"
                    inputMethodHints: Qt.ImhDigitsOnly
                    enabled: window.accountManager && window.accountManager.verifyCodeRequired
                    onAccepted: submitVerifyButton.clicked()
                }

                Button {
                    id: submitVerifyButton
                    text: "提交验证码"
                    enabled: window.accountManager &&
                             window.accountManager.verifyCodeRequired &&
                             verifyCodeField.text.trim().length > 0
                    onClicked: {
                        if (!window.accountManager || !enabled)
                            return
                        window.accountManager.submitVerifyCode(verifyCodeField.text.trim())
                    }
                }
            }
        }

        footer: DialogButtonBox {
            Button {
                text: "取消"
                DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
            }

            onRejected: {
                if (window.accountManager && window.accountManager.adding)
                    window.accountManager.cancelAddAccount()
                addAccountDialog.close()
            }
        }
    }
}
