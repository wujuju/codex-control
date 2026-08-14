pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: window
    required property var bridge
    required property var conversationModel
    required property var messageModel
    width: 1080
    height: 720
    minimumWidth: 820
    minimumHeight: 520
    visible: true
    title: "微信 Codex 控制台"
    color: "#f3f5f8"

    property color accent: "#2f7df6"
    property color sidebar: "#151922"
    property color sidebarMuted: "#8f98aa"

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
                    model: window.conversationModel

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
                            color: window.bridge.statusState === "running" ? "#38d996" :
                                   window.bridge.statusState === "reconnecting" ? "#f0b44d" : "#7c8799"
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            Text {
                                text: window.bridge.statusText
                                color: "#e7ebf2"
                                font.pixelSize: 12
                                elide: Text.ElideRight
                                Layout.fillWidth: true
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
                                    text: window.bridge.conversationTitle
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
                                        text: window.bridge.conversationType
                                        color: window.accent
                                        font.pixelSize: 11
                                    }
                                }
                            }
                            Text {
                                text: window.bridge.policyText
                                color: "#7a8494"
                                font.pixelSize: 12
                            }
                        }

                        Button {
                            text: window.bridge.running ? "停止" : "启动"
                            onClicked: window.bridge.running ? window.bridge.stopBridge() : window.bridge.startBridge()
                        }
                        Button {
                            text: "清空"
                            flat: true
                            onClicked: window.bridge.clearMessages()
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
                    model: window.messageModel
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
                                    } else {
                                        window.bridge.sendMessage(text)
                                        clear()
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
                            enabled: composer.text.trim().length > 0 && window.bridge.running
                            onClicked: {
                                window.bridge.sendMessage(composer.text)
                                composer.clear()
                            }
                            background: Rectangle {
                                radius: 10
                                color: parent.enabled ? window.accent : "#b9c3d1"
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
