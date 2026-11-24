import QtQuick
import QtQuick.Window

Window {
    id: window
    visible: false
    color: "transparent"
    title: "LIA"
    // Do not force "always on top": the KWin script (kde.py) keeps the
    // panel on top when it appears and releases it when you click another
    // window. The x/y values below apply only to X11.
    flags: Qt.FramelessWindowHint
           | (Qt.platform.pluginName === "xcb" ? Qt.Tool : 0)

    property bool expanded: false
    property bool closedManual: false
    width: expanded ? 780 : 380
    height: panel.height + 24
    x: geoX + (geoW - width) / 2
    y: geoY + Math.max(48, geoH * 0.06)

    property string state: "sleeping"
    property real levelTarget: 0
    property real levelSmooth: 0
    property real lastLevel: 0
    property real t: 0
    property string textUser: ""
    property string textResponse: ""
    property string descriptionCommand: ""

    readonly property var rotulos: ({
        sleeping: "Sleeping",
        awakened: "Yes?",
        listening: "Listening",
        transcribing: "Transcribing",
        thinking: "Thinking",
        speaking: "Speaking",
        awaiting_confirmation: "Awaiting confirmation",
        executing: "Executing",
        error: "Error"
    })

    readonly property var cores: ({
        sleeping: [90, 105, 130],
        awakened: [60, 200, 255],
        listening: [60, 200, 255],
        transcribing: [80, 170, 255],
        thinking: [90, 150, 255],
        speaking: [120, 230, 255],
        awaiting_confirmation: [255, 176, 42],
        executing: [90, 255, 190],
        error: [255, 90, 90]
    })

    function colorCurrent() { return cores[state] || cores.sleeping }

    // rgba(): for Canvas only (accepts CSS text). cor(): for the
    // QML properties, which do NOT understand "rgba(...)" and turn black.
    function rgba(c, a) { return "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + a + ")" }
    function color(c, a) { return Qt.rgba(c[0] / 255, c[1] / 255, c[2] / 255, a) }

    function show() {
        // When a monitor is clear, the ENVIRONMENT (environment.qml) appears;
        // the popup appears only when all monitors are occupied (or
        // when a click is required, such as for confirmation).
        if (closedManual || !bridge.popupAllowed) return
        esconder.stop()
        close.stop()
        window.visible = true
        panel.open = true
    }

    function closePanel() {
        closedManual = true
        esconder.stop()
        panel.open = false
        close.start()
    }

    function handle(m) {
        if (m.event === "state") {
            state = m.state
            if (state === "sleeping") {
                if (window.visible) esconder.restart()
            } else {
                if (state === "awakened") {
                    textUser = ""
                    textResponse = ""
                    closedManual = false
                }
                if (state === "error") textResponse = m.message || "Error"
                show()
            }
        } else if (m.event === "level") {
            levelTarget = m.value
            lastLevel = Date.now()
        } else if (m.event === "user_speech") {
            textUser = m.text
            textResponse = ""
        } else if (m.event === "partial_response") {
            textResponse += m.text
        } else if (m.event === "response_end") {
            textResponse = m.text
        } else if (m.event === "confirmation") {
            descriptionCommand = m.description
            state = "awaiting_confirmation"
            closedManual = false
            show()
        }
    }

    Connections {
        target: bridge
        function onEvent(text) { window.handle(JSON.parse(text)) }
    }

    // Disappear automatically a few seconds after going back to sleep (unless
    // you are typing).
    Timer {
        id: esconder
        interval: 3500
        onTriggered: {
            if (field.activeFocus || field.text !== "") {
                esconder.restart()
                return
            }
            panel.open = false
            close.start()
        }
    }
    Timer { id: close; interval: 400; onTriggered: window.visible = false }

    Timer {
        interval: 33
        running: window.visible
        repeat: true
        onTriggered: {
            window.t += 0.033
            window.levelSmooth += (window.levelTarget - window.levelSmooth) * 0.35
            window.levelTarget *= 0.9
            reator.requestPaint()
        }
    }

    Rectangle {
        id: panel
        property bool open: false
        width: window.width - 40
        height: (window.expanded
                 ? Math.max(esquerda.y + esquerda.height, direita.y + direita.height)
                 : direita.y + direita.height) + 20
        y: 12
        x: open ? window.width - width - 20 : window.width + 10
        radius: 20
        color: "#ee0a1018"
        border.width: 1
        border.color: window.color(window.colorCurrent(), 0.35)

        Behavior on x { NumberAnimation { duration: 320; easing.type: Easing.OutCubic } }

        // --- Top bar: drag, expand, close ---
        Item {
            id: cabecalho
            x: 14
            y: 8
            width: panel.width - 28
            height: 26

            DragHandler {
                target: null
                onActiveChanged: if (active) bridge.moveWindow()
            }

            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: "LIA"
                font.pixelSize: 11
                font.letterSpacing: 3
                color: "#7a90a8"
            }

            Row {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                spacing: 8

                Rectangle {
                    width: labelExpandir.implicitWidth + 18
                    height: 22
                    radius: 11
                    color: "#22ffffff"

                    Text {
                        id: labelExpandir
                        anchors.centerIn: parent
                        text: window.expanded ? "COLLAPSE" : "EXPAND"
                        font.pixelSize: 10
                        font.letterSpacing: 1.5
                        color: "#cfe3f5"
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: window.expanded = !window.expanded
                    }
                }

                Rectangle {
                    width: 22
                    height: 22
                    radius: 11
                    color: "#22ffffff"

                    Text {
                        anchors.centerIn: parent
                        text: "×"
                        font.pixelSize: 16
                        color: "#cfe3f5"
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: window.closePanel()
                    }
                }
            }
        }

        // --- Left column: reactor and status ---
        Column {
            id: esquerda
            x: 20
            y: 40
            width: 300
            spacing: 14

            Item {
                width: esquerda.width
                height: 210

                Canvas {
                    id: reator
                    anchors.centerIn: parent
                    width: 210
                    height: 210

                    onPaint: {
                        var ctx = getContext("2d")
                        ctx.clearRect(0, 0, width, height)
                        var cx = width / 2, cy = height / 2
                        var TAU = Math.PI * 2
                        var c = window.colorCurrent()
                        var e = window.state
                        var k = (e === "sleeping") ? 0.35 : 1.0
                        var t = window.t

                        var pulso
                        if (e === "listening" || e === "awakened") pulso = window.levelSmooth
                        else if (e === "speaking") pulso = (Date.now() - window.lastLevel < 400)
                            ? window.levelSmooth
                            : 0.3 + 0.35 * Math.abs(Math.sin(t * 7)) * (0.6 + 0.4 * Math.sin(t * 2.3))
                        else if (e === "thinking" || e === "executing" || e === "transcribing") pulso = 0.18 + 0.1 * Math.sin(t * 4)
                        else if (e === "awaiting_confirmation") pulso = 0.22 + 0.15 * Math.sin(t * 3)
                        else pulso = 0.04
                        var base = 32 + pulso * 24

                        // halo
                        var g = ctx.createRadialGradient(cx, cy, 2, cx, cy, base * 1.9)
                        g.addColorStop(0, window.rgba(c, 0.55 * k))
                        g.addColorStop(0.45, window.rgba(c, 0.15 * k))
                        g.addColorStop(1, window.rgba(c, 0))
                        ctx.fillStyle = g
                        ctx.beginPath(); ctx.arc(cx, cy, base * 1.9, 0, TAU); ctx.fill()

                        // core
                        var claro = [Math.min(255, c[0] + 120), Math.min(255, c[1] + 120), Math.min(255, c[2] + 120)]
                        ctx.fillStyle = window.rgba(claro, 0.9 * k)
                        ctx.beginPath(); ctx.arc(cx, cy, base * 0.45, 0, TAU); ctx.fill()

                        // inner ring
                        ctx.lineWidth = 2
                        ctx.strokeStyle = window.rgba(c, 0.9 * k)
                        ctx.beginPath(); ctx.arc(cx, cy, base, 0, TAU); ctx.stroke()

                        // segments (spin faster while thinking)
                        var gira = (e === "thinking" || e === "executing" || e === "transcribing")
                        var rot = gira ? t * 2.8 : t * 0.35
                        ctx.lineWidth = 5
                        ctx.lineCap = "round"
                        ctx.strokeStyle = window.rgba(c, 0.85 * k)
                        for (var i = 0; i < 10; i++) {
                            var a0 = rot + i * TAU / 10
                            ctx.beginPath(); ctx.arc(cx, cy, base + 20, a0, a0 + 0.42); ctx.stroke()
                        }

                        // outer markers
                        ctx.lineWidth = 1.5
                        ctx.strokeStyle = window.rgba(c, 0.45 * k)
                        var r1 = base + 38, r2 = base + 44
                        for (var j = 0; j < 36; j++) {
                            var a = -rot * 0.4 + j * TAU / 36
                            ctx.beginPath()
                            ctx.moveTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1)
                            ctx.lineTo(cx + Math.cos(a) * r2, cy + Math.sin(a) * r2)
                            ctx.stroke()
                        }
                    }
                }
            }

            Text {
                width: esquerda.width
                horizontalAlignment: Text.AlignHCenter
                text: window.rotulos[window.state] || ""
                font.pixelSize: 14
                font.letterSpacing: 2
                font.capitalization: Font.AllUppercase
                color: "#ffffff"
            }

            // Bars that react to voice volume
            Item {
                width: esquerda.width
                height: 46
                visible: window.state === "listening" || window.state === "awakened"

                Row {
                    anchors.centerIn: parent
                    spacing: 5

                    Repeater {
                        model: 13

                        Rectangle {
                            width: 5
                            radius: 2
                            anchors.verticalCenter: parent.verticalCenter
                            height: 4 + window.levelSmooth * 40 * (0.45 + 0.55 * Math.abs(Math.sin(window.t * 7 + index * 0.9)))
                            color: window.color(window.colorCurrent(), 0.9)
                        }
                    }
                }
            }

            // Three dots while thinking
            Item {
                width: esquerda.width
                height: 24
                visible: window.state === "thinking" && window.textResponse === ""

                Row {
                    anchors.horizontalCenter: parent.horizontalCenter
                    spacing: 10

                    Repeater {
                        model: 3

                        Rectangle {
                            width: 10
                            height: 10
                            radius: 5
                            y: 7 + Math.sin(window.t * 6 + index * 0.8) * 6
                            color: window.color(window.colorCurrent(), 0.9)
                        }
                    }
                }
            }
        }

        // --- Right column: conversation (below when compact) ---
        Column {
            id: direita
            x: window.expanded ? 340 : 20
            y: window.expanded ? 40 : esquerda.y + esquerda.height + 14
            width: window.expanded ? panel.width - 360 : 300
            spacing: 12

            Text {
                width: direita.width
                visible: text !== ""
                text: window.textUser !== "" ? "You: " + window.textUser : ""
                wrapMode: Text.Wrap
                maximumLineCount: 4
                elide: Text.ElideRight
                font.pixelSize: 13
                font.italic: true
                color: "#8fa3b8"
            }

            // Scrollable response (mouse wheel); follows the incoming text
            // and grows more in expanded mode.
            Item {
                width: direita.width
                height: rolagem.height
                visible: window.textResponse !== ""

                Flickable {
                    id: rolagem
                    width: parent.width
                    height: Math.min(responseText.implicitHeight, window.expanded ? 440 : 260)
                    contentWidth: width
                    contentHeight: responseText.implicitHeight
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds
                    onContentHeightChanged: contentY = Math.max(0, contentHeight - height)

                    Text {
                        id: responseText
                        width: rolagem.width - 8
                        text: window.textResponse
                        wrapMode: Text.Wrap
                        font.pixelSize: 15
                        color: "#e8f4ff"
                    }
                }

                Rectangle {
                    visible: rolagem.contentHeight > rolagem.height + 1
                    x: parent.width - 3
                    width: 3
                    radius: 1.5
                    color: "#66ffffff"
                    y: rolagem.height * rolagem.visibleArea.yPosition
                    height: rolagem.height * rolagem.visibleArea.heightRatio
                }
            }

            // Confirmation card
            Rectangle {
                visible: window.state === "awaiting_confirmation"
                width: direita.width
                height: card.implicitHeight + 28
                radius: 14
                color: "#22ffb02a"
                border.width: 1
                border.color: "#aaffb02a"

                Column {
                    id: card
                    x: 14
                    y: 14
                    width: parent.width - 28
                    spacing: 10

                    Text {
                        width: card.width
                        text: "May I " + window.descriptionCommand + "?"
                        wrapMode: Text.Wrap
                        font.pixelSize: 14
                        color: "#ffe2a8"
                    }

                    Row {
                        spacing: 10

                        Rectangle {
                            width: (card.width - 10) / 2
                            height: 36
                            radius: 10
                            color: "#ffb02a"

                            Text { anchors.centerIn: parent; text: "Confirm"; font.bold: true; color: "#1a1200" }
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: bridge.send("confirm")
                            }
                        }

                        Rectangle {
                            width: (card.width - 10) / 2
                            height: 36
                            radius: 10
                            color: "transparent"
                            border.width: 1
                            border.color: "#aaffb02a"

                            Text { anchors.centerIn: parent; text: "Cancel"; color: "#ffd27a" }
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: bridge.send("cancel")
                            }
                        }
                    }

                    Text {
                        text: "or say \"yes\""
                        font.pixelSize: 12
                        color: "#99ffd27a"
                    }
                }
            }

            // Text input for Jarvis
            Rectangle {
                width: direita.width
                height: 38
                radius: 10
                color: "#18ffffff"
                border.width: 1
                border.color: field.activeFocus
                              ? window.color(window.colorCurrent(), 0.8)
                              : "#33ffffff"

                TextInput {
                    id: field
                    anchors.fill: parent
                    anchors.leftMargin: 12
                    anchors.rightMargin: 12
                    verticalAlignment: TextInput.AlignVCenter
                    color: "#ffffff"
                    font.pixelSize: 14
                    clip: true
                    selectByMouse: true

                    onAccepted: {
                        var t = text.trim()
                        if (t === "") return
                        window.textUser = t
                        window.textResponse = ""
                        bridge.sendText(t)
                        text = ""
                    }
                }

                Text {
                    x: 12
                    anchors.verticalCenter: parent.verticalCenter
                    visible: field.text === "" && !field.activeFocus
                    text: "Type to Jarvis..."
                    font.pixelSize: 14
                    color: "#6b7f94"
                }
            }
        }
    }
}
