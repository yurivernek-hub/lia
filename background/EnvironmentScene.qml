import QtQuick

// Jarvis’s background "scene" (used by the C++ background and the
// fallback windowed environment). Based on the video reference:
//  - an orb of thin, glowing, wavy ribbons (not rigid rings);
//  - a fine background grid, a status label at the top, and a status button below;
//  - WIDGETS: glass cards (weather, news, text responses). The latest
//    card is large in the center, over the orb; previous cards SHRINK
//    into a row below (up to 4). Everything is discarded when returning to IDLE.
//
// Cost: in IDLE, nothing runs (no timer, no repainting). The animation timer
// exists only while awake/slowing down and while the monitor is visible.
Item {
    id: scene

    property string output: ""
    property bool busy: false
    property url image: ""
    property bool backgroundOpaque: true

    property string state: "sleeping"
    property real levelTarget: 0
    property real levelSmooth: 0
    property real lastLevel: 0          // Date.now() of the last "level" event
    property real t: 0
    property string textUser: ""
    property string textResponse: ""
    property string descriptionCommand: ""

    // Widget stack for this session (the last one is the main card).
    property int idxText: -1        // text card for the current interaction
    property bool hasWidget: false   // did the current interaction produce a widget?

    // Awake? The phase (0 = IDLE, 1 = active) animates automatically: fast when
    // waking up (350 ms) and slow when going back to sleep (900 ms).
    property bool active: false
    property real phase: active ? 1 : 0
    Behavior on phase {
        NumberAnimation {
            duration: scene.active ? 350 : 900
            easing.type: Easing.InOutQuad
        }
    }
    onPhaseChanged: orbe.requestPaint()
    onWidthChanged: orbe.requestPaint()
    Component.onCompleted: {
        orbe.requestPaint()
        Qt.callLater(function() { scene.ready = true })
    }

    // The graphics shrink on smaller screens.
    property real scale: Math.max(0.55, Math.min(1, Math.min(width, height) / 1080))
    property real centerY: height * 0.46

    // Widget dock (on the right). When a widget is shown, the orb slides to the
    // left and clears space; without a widget, it returns to the center.
    readonly property bool hasDock: pilha.count > 0
    property real dockPresence: hasDock ? 1 : 0
    Behavior on dockPresence { NumberAnimation { duration: 300 } }
    readonly property real dockScale: Math.min(scale, width * 0.40 / 520)
    readonly property real dockLarg: 520 * dockScale
    // With a task on screen (task panel): the orb stays centered, the chart
    // moves to the right dock, and conversation widgets move to the left.
    readonly property bool hasTask: indicador.hasTasks
    readonly property real graficoX: width - 56 * scale - dockLarg
    property real dockX: hasTask ? 56 * scale : width - 56 * scale - dockLarg
    Behavior on dockX {
        enabled: scene.ready
        NumberAnimation { duration: 500; easing.type: Easing.InOutCubic }
    }
    property real heightMain: 320     // main card height (unscaled)
    Behavior on heightMain { NumberAnimation { duration: 200; easing.type: Easing.OutCubic } }
    readonly property real dockTopo: Math.max(96 * scale, centerY - heightMain * dockScale / 2)

    property bool ready: false            // animate the slide only after the first layout
    property real centerX: (hasDock && !hasTask) ? dockX / 2 : width / 2
    Behavior on centerX {
        enabled: scene.ready
        NumberAnimation { duration: 500; easing.type: Easing.InOutCubic }
    }

    readonly property bool confirming: state === "awaiting_confirmation"
    readonly property bool cardVisible: pilha.count > 0 || confirming

    readonly property var cinza: [150, 160, 175]

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

    // Gray in IDLE, status color when awake.
    function colorCurrent() {
        var c = cores[state] || cores.sleeping
        var g = cinza
        var f = phase
        return [Math.round(g[0] + (c[0] - g[0]) * f),
                Math.round(g[1] + (c[1] - g[1]) * f),
                Math.round(g[2] + (c[2] - g[2]) * f)]
    }

    function rgba(c, a) { return "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + a + ")" }
    function color(c, a) { return Qt.rgba(c[0] / 255, c[1] / 255, c[2] / 255, a) }

    // Reaction strength (0..1): volume of YOUR voice while listening, speech volume
    // when Jarvis speaks (real when available, otherwise simulated).
    function pulso() {
        var e = state
        var real = Math.sqrt(Math.min(1, levelSmooth))

        if (e === "listening" || e === "awakened") return real

        if (e === "speaking") {
            if (Date.now() - lastLevel < 400) return real
            return 0.3 + 0.35 * Math.abs(Math.sin(t * 7)) * (0.6 + 0.4 * Math.sin(t * 2.3))
        }

        if (e === "thinking" || e === "executing" || e === "transcribing")
            return 0.12 + 0.08 * Math.sin(t * 4)

        if (e === "awaiting_confirmation") return 0.15 + 0.10 * Math.sin(t * 3)

        return 0
    }

    function acento(a) { return color(colorCurrent(), a) }

    // One forecast per frame, shared by all widgets.
    readonly property real pulsoCurrent: pulso()

    // Schedule summary for the compact bar.
    function summarySchedule(items) {
        if (!items || items.length === 0) return "No events found"
        var p = items[0]
        return (p.hora ? p.hora + "  " : "") + p.title
               + (items.length > 1 ? "  +" + (items.length - 1) : "")
    }

    // Tap a compact bar to make it the main widget.
    function promover(i) {
        var last = pilha.count - 1
        if (i < 0 || i >= last) return
        pilha.move(i, last, 1)
        if (idxText === i) idxText = last
        else if (idxText > i) idxText--
    }

    // Number or "--" (the API may not provide the data).
    function fmt(v, sufixo) {
        return (v === null || v === undefined) ? "--" : v + sufixo
    }

    // Only the end of the text (a two-line caption while the response arrives).
    function tail(text) {
        if (text.length <= 150) return text
        var part = text.slice(text.length - 150)
        var i = part.indexOf(" ")
        return "…" + (i >= 0 ? part.slice(i) : part)
    }

    function add(tipo, title, data) {
        pilha.append({ tipo: tipo, title: title, data: JSON.stringify(data) })

        while (pilha.count > 5) {
            pilha.remove(0)
            if (idxText >= 0) idxText--
        }

        return pilha.count - 1
    }

    // Text response: turn it into a card (unless there is already a widget).
    function updateText() {
        if (hasWidget || textResponse === "") return

        var data = { text: textResponse }

        if (idxText < 0) idxText = add("text", textUser, data)
        else pilha.setProperty(idxText, "data", JSON.stringify(data))
    }

    function handle(m) {
        if (m.event === "state") {
            state = m.state
            if (state === "sleeping") {
                undo.restart()
            } else {
                undo.stop()
                clear.stop()
                if (state === "awakened") {
                    textUser = ""
                    textResponse = ""
                    idxText = -1
                    hasWidget = false
                }
                if (state === "error") {
                    textResponse = m.message || "Error"
                    updateText()
                }
                active = true
            }
        } else if (m.event === "level") {
            levelTarget = m.value
            lastLevel = Date.now()
        } else if (m.event === "user_speech") {
            textUser = m.text
            textResponse = ""
            idxText = -1
            hasWidget = false
        } else if (m.event === "widget") {
            undo.stop()
            clear.stop()
            active = true
            add(m.widget, m.title || "", m.data || {})
            hasWidget = true
        } else if (m.event === "partial_response") {
            textResponse += m.text
            updateText()
        } else if (m.event === "response_end") {
            textResponse = m.text
            updateText()
        } else if (m.event === "task") {
            // Task progress (foreground or background).
            indicador.receive(m)
        } else if (m.event === "confirmation") {
            // Confirmation is handled HERE, in the environment itself.
            descriptionCommand = m.description
            state = "awaiting_confirmation"
            undo.stop()
            clear.stop()
            active = true
        }
    }

    Connections {
        target: bridge
        function onEvent(text) { scene.handle(JSON.parse(text)) }
    }

    ListModel { id: pilha }

    // After sleeping for a while, return to IDLE and clear everything.
    Timer {
        id: undo
        interval: 2500
        onTriggered: { scene.active = false; clear.start() }
    }
    Timer {
        id: clear
        interval: 1000
        onTriggered: {
            scene.textUser = ""
            scene.textResponse = ""
            scene.levelSmooth = 0
            scene.idxText = -1
            scene.hasWidget = false
            pilha.clear()
        }
    }

    // Animation: only while awake/slowing down and while the monitor is visible.
    Timer {
        interval: 33
        repeat: true
        running: scene.visible && !scene.busy && (scene.active || scene.phase > 0.001)
        onTriggered: {
            scene.t += 0.033
            // quick attack, slower decay: reaction "pulsing" with the voice
            var k = scene.levelTarget > scene.levelSmooth ? 0.6 : 0.2
            scene.levelSmooth += (scene.levelTarget - scene.levelSmooth) * k
            scene.levelTarget *= 0.85
            orbe.requestPaint()
        }
    }

    // ------------------------------------------------------------------
    // Background
    // ------------------------------------------------------------------
    Rectangle {
        anchors.fill: parent
        visible: scene.backgroundOpaque
        gradient: Gradient {
            GradientStop { position: 0.0; color: "#070d18" }
            GradientStop { position: 1.0; color: "#03060b" }
        }
    }

    // Optional image (--image=...). Decoded at screen size.
    Image {
        anchors.fill: parent
        visible: scene.backgroundOpaque && scene.image != ""
        source: scene.image
        sourceSize: Qt.size(scene.width, scene.height)
        fillMode: Image.PreserveAspectCrop
        asynchronous: true
        cache: false
    }

    // Deep blue when waking up.
    Rectangle {
        anchors.fill: parent
        visible: scene.backgroundOpaque && opacity > 0.003
        opacity: 0.92 * scene.phase
        gradient: Gradient {
            GradientStop { position: 0.0; color: "#0a2150" }
            GradientStop { position: 0.55; color: "#06163a" }
            GradientStop { position: 1.0; color: "#030c22" }
        }
    }

    // Fine grid (simple lines: no texture text in memory).
    Item {
        anchors.fill: parent
        opacity: (scene.backgroundOpaque ? 0.05 : 0.03) + 0.11 * scene.phase

        Repeater {
            model: Math.ceil(scene.width / 48) + 1
            Rectangle { x: index * 48; width: 1; height: scene.height; color: "#4aa8ff" }
        }
        Repeater {
            model: Math.ceil(scene.height / 48) + 1
            Rectangle { y: index * 48; height: 1; width: scene.width; color: "#4aa8ff" }
        }
    }

    // ------------------------------------------------------------------
    // Orb: same reactor as the popup (core, rings, segments, markers),
    // at a larger size. Voice volume -> grows and glows; while processing ->
    // the segments spin quickly.
    // ------------------------------------------------------------------
    Canvas {
        id: orbe
        width: 640
        height: 640
        x: scene.centerX - width / 2
        y: scene.centerY - height / 2
        scale: scene.scale
        transformOrigin: Item.Center

        onPaint: {
            var ctx = getContext("2d")
            ctx.clearRect(0, 0, width, height)

            var f = scene.phase
            var e = scene.state
            var t = scene.t
            var c = scene.colorCurrent()
            var p = scene.pulso()
            var cx = width / 2, cy = height / 2
            var TAU = Math.PI * 2
            var K = 2.3                                  // popup (210 px) -> environment

            var recuo = 1                                // the orb clears space (widgets in the dock)
            var k = (0.30 + 0.70 * f) * recuo            // subtle IDLE -> active
            var base = (32 + 24 * p) * K                 // volume -> size

            // halo
            var g = ctx.createRadialGradient(cx, cy, 2 * K, cx, cy, base * 1.9)
            g.addColorStop(0, scene.rgba(c, 0.55 * k))
            g.addColorStop(0.45, scene.rgba(c, 0.15 * k))
            g.addColorStop(1, scene.rgba(c, 0))
            ctx.fillStyle = g
            ctx.beginPath(); ctx.arc(cx, cy, base * 1.9, 0, TAU); ctx.fill()

            // core
            var claro = [Math.min(255, c[0] + 120), Math.min(255, c[1] + 120), Math.min(255, c[2] + 120)]
            ctx.fillStyle = scene.rgba(claro, 0.9 * k)
            ctx.beginPath(); ctx.arc(cx, cy, base * 0.45, 0, TAU); ctx.fill()

            // inner ring
            ctx.lineWidth = 2 * 1.6
            ctx.strokeStyle = scene.rgba(c, 0.9 * k)
            ctx.beginPath(); ctx.arc(cx, cy, base, 0, TAU); ctx.stroke()

            // segments (spin quickly while processing)
            var gira = (e === "thinking" || e === "executing" || e === "transcribing")
            var rot = gira ? t * 2.8 : t * 0.35
            ctx.lineWidth = 5 * 1.6
            ctx.lineCap = "round"
            ctx.strokeStyle = scene.rgba(c, 0.85 * k)
            for (var i = 0; i < 10; i++) {
                var a0 = rot + i * TAU / 10
                ctx.beginPath(); ctx.arc(cx, cy, base + 20 * K, a0, a0 + 0.42); ctx.stroke()
            }

            // outer markers
            ctx.lineWidth = 1.5 * 1.6
            ctx.strokeStyle = scene.rgba(c, 0.45 * k)
            var r1 = base + 38 * K, r2 = base + 44 * K
            for (var j = 0; j < 36; j++) {
                var a = -rot * 0.4 + j * TAU / 36
                ctx.beginPath()
                ctx.moveTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1)
                ctx.lineTo(cx + Math.cos(a) * r2, cy + Math.sin(a) * r2)
                ctx.stroke()
            }
        }
    }

    // ------------------------------------------------------------------
    // HUD: status label (top) and status button (bottom)
    // ------------------------------------------------------------------
    Column {
        x: scene.centerX - width / 2
        y: 60 * scene.scale
        spacing: 8
        opacity: 0.30 + 0.70 * scene.phase

        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: scene.rotulos[scene.state] || ""
            font.pixelSize: 14
            font.letterSpacing: 7
            font.capitalization: Font.AllUppercase
            color: scene.color(scene.colorCurrent(), 1)
        }
        Rectangle {
            anchors.horizontalCenter: parent.horizontalCenter
            width: 140
            height: 1
            color: scene.color(scene.colorCurrent(), 0.5)
        }
    }

    Item {
        x: scene.centerX - width / 2
        y: scene.height - 90 * scene.scale
        width: 40
        height: 40
        opacity: 0.30 + 0.70 * scene.phase
        scale: 1 + 0.25 * scene.pulso()

        Rectangle {
            anchors.centerIn: parent
            width: 40; height: 40; radius: 20
            color: "transparent"
            border.width: 1.5
            border.color: scene.color(scene.colorCurrent(), 0.8)
        }
        Rectangle {
            anchors.centerIn: parent
            width: 12; height: 12; radius: 6
            color: scene.color(scene.colorCurrent(), 0.95)
        }
    }

    // ------------------------------------------------------------------
    // Widgets: stay in a "dock" on the right; the orb slides to the left
    // and clears space. The last widget is the main one (full card);
    // earlier ones become compact bars below (tap a bar to
    // bring it back to the main position).
    // Reactive: the border, glow, and beam connecting the reactor to the card follow
    // voice volume (pulsoAtual) and the status color.
    // ------------------------------------------------------------------

    // Beam: connects the reactor to the main card.
    Item {
        id: feixe
        visible: opacity > 0.01
        opacity: scene.phase * scene.dockPresence * (0.30 + 0.70 * scene.pulsoCurrent)
        x: scene.hasTask ? scene.dockX + scene.dockLarg + 6 : scene.centerX + 255 * scene.scale
        y: scene.dockTopo + scene.heightMain * scene.dockScale / 2 - 1
        width: Math.max(0, scene.hasTask
                           ? (scene.centerX - 255 * scene.scale) - x
                           : scene.dockX - x - 6)
        height: 2

        Rectangle {
            anchors.fill: parent
            gradient: Gradient {
                orientation: Gradient.Horizontal
                GradientStop { position: 0.0; color: scene.hasTask ? scene.acento(0.9) : scene.acento(0.0) }
                GradientStop { position: 1.0; color: scene.hasTask ? scene.acento(0.0) : scene.acento(0.9) }
            }
        }
        Rectangle {
            x: scene.hasTask ? -4 : parent.width - 4
            y: -3
            width: 8
            height: 8
            radius: 4
            color: scene.acento(1)
        }
    }

    Repeater {
        model: pilha

        delegate: Item {
            id: w

            readonly property string tipo: model.tipo
            property var d: JSON.parse(model.data)
            readonly property bool main: index === pilha.count - 1
            readonly property int rank: pilha.count - 2 - index    // 0 = most recently added previous card

            readonly property string icone: tipo === "weather" ? "☀" : (tipo === "news" ? "☰" : (tipo === "schedule" ? "◷" : "◆"))
            readonly property string category: tipo === "weather" ? "Weather" : (tipo === "news" ? "News" : (tipo === "schedule" ? "Schedule" : "Response"))
            readonly property string summary: tipo === "weather"
                ? scene.fmt(d.temp, "°") + "  " + (d.description || "")
                : (tipo === "news" ? d.items[0].headline
                : (tipo === "schedule" ? scene.summarySchedule(d.items) : d.text))

            // entry: slides in from the right
            property real input: 0
            NumberAnimation on input { from: 0; to: 1; duration: 340; easing.type: Easing.OutCubic }

            property real yTarget: main
                ? scene.dockTopo
                : scene.dockTopo + (scene.heightMain + 14 + rank * 60) * scene.dockScale
            Behavior on yTarget { NumberAnimation { duration: 380; easing.type: Easing.OutCubic } }

            width: 520
            height: main ? moldura.height : 52
            Behavior on height { NumberAnimation { duration: 300; easing.type: Easing.OutCubic } }

            x: scene.dockX + (scene.hasTask ? -1 : 1) * (1 - input) * 36 * scene.dockScale
            y: yTarget
            scale: scene.dockScale * (0.95 + 0.05 * input)
            transformOrigin: Item.TopLeft
            opacity: scene.phase * input * (main ? 1 : 0.94)
            visible: opacity > 0.01

            // the main card reports its height (the dock and beam adjust)
            Binding {
                target: scene
                property: "heightMain"
                value: moldura.height
                when: w.main
                restoreMode: Binding.RestoreNone
            }

            // outer outline that pulses with the voice
            Rectangle {
                anchors.fill: moldura
                anchors.margins: -4
                radius: 16
                color: "transparent"
                border.width: 1.5
                border.color: scene.acento(0.08 + 0.45 * scene.pulsoCurrent)
                opacity: moldura.opacity
                visible: moldura.visible
            }

            // ---------------- main card ----------------
            Rectangle {
                id: moldura
                width: 520
                height: content.implicitHeight + 40
                radius: 12
                opacity: w.main ? 1 : 0
                visible: opacity > 0.01
                Behavior on opacity { NumberAnimation { duration: 220 } }

                gradient: Gradient {
                    GradientStop { position: 0.0; color: "#ee0b2756" }
                    GradientStop { position: 1.0; color: "#e6051530" }
                }
                border.width: 1
                border.color: scene.acento(0.40 + 0.25 * scene.pulsoCurrent)

                // HUD detail: the corner bar stretches with the voice
                Rectangle {
                    x: 0
                    y: 0
                    width: 56 + 150 * scene.pulsoCurrent
                    height: 2
                    radius: 1
                    color: scene.acento(0.95)
                }

                Column {
                    id: content
                    x: 22
                    y: 20
                    width: parent.width - 44
                    spacing: 14

                    // header: icon + category + title
                    Row {
                        spacing: 12

                        Rectangle {
                            width: 34
                            height: 34
                            radius: 8
                            color: scene.acento(0.14)
                            border.width: 1
                            border.color: scene.acento(0.6)

                            Text {
                                anchors.centerIn: parent
                                text: w.icone
                                font.pixelSize: 17
                                color: scene.acento(1)
                            }
                        }

                        Column {
                            anchors.verticalCenter: parent.verticalCenter
                            spacing: 1

                            Text {
                                text: w.category
                                font.pixelSize: 11
                                color: scene.acento(0.9)
                            }
                            Text {
                                width: content.width - 46
                                text: model.title
                                elide: Text.ElideRight
                                font.pixelSize: 20
                                font.bold: true
                                color: "#f1f8ff"
                            }
                        }
                    }

                    Loader {
                        width: content.width
                        sourceComponent: w.tipo === "weather" ? compWeather
                                       : (w.tipo === "news" ? compNews
                                       : (w.tipo === "schedule" ? compSchedule : compText))
                    }
                }
            }

            // ---------------- compact bar (previous widgets) ----------------
            Rectangle {
                id: barra
                anchors.fill: parent
                radius: 10
                color: "#d4061a38"
                border.width: 1
                border.color: scene.acento(0.30)
                opacity: w.main ? 0 : 1
                visible: opacity > 0.01
                Behavior on opacity { NumberAnimation { duration: 260 } }

                Rectangle {
                    x: 0
                    y: 10
                    width: 2
                    height: parent.height - 20
                    color: scene.acento(0.85)
                }

                Rectangle {
                    x: 16
                    anchors.verticalCenter: parent.verticalCenter
                    width: 28
                    height: 28
                    radius: 7
                    color: scene.acento(0.12)
                    border.width: 1
                    border.color: scene.acento(0.5)

                    Text {
                        anchors.centerIn: parent
                        text: w.icone
                        font.pixelSize: 14
                        color: scene.acento(1)
                    }
                }

                Column {
                    x: 56
                    width: parent.width - 56 - 16
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 1

                    Text {
                        width: parent.width
                        text: model.title
                        elide: Text.ElideRight
                        font.pixelSize: 14
                        font.bold: true
                        color: "#f1f8ff"
                    }
                    Text {
                        width: parent.width
                        text: w.summary
                        elide: Text.ElideRight
                        font.pixelSize: 12
                        color: "#9fc1e2"
                    }
                }

                MouseArea {
                    anchors.fill: parent
                    enabled: !w.main
                    cursorShape: Qt.PointingHandCursor
                    onClicked: scene.promover(index)
                }
            }

            // --- content models ---

            Component {
                id: compText

                Item {
                    width: content.width
                    height: textResp.implicitHeight

                    Rectangle {
                        width: 3
                        height: parent.height
                        radius: 1.5
                        color: scene.acento(0.8)
                    }
                    Text {
                        id: textResp
                        x: 16
                        width: parent.width - 16
                        text: w.d.text
                        wrapMode: Text.Wrap
                        maximumLineCount: 9
                        elide: Text.ElideRight
                        font.pixelSize: 22
                        color: "#eaf6ff"
                    }
                }
            }

            Component {
                id: compWeather

                Column {
                    id: weather
                    width: content.width
                    spacing: 14

                    readonly property var days: w.d.days || []

                    // shared scale for the daily temperature bars
                    readonly property real gmin: {
                        var m = 1e9
                        for (var i = 0; i < days.length; i++)
                            if (days[i].min != null && days[i].min < m) m = days[i].min
                        return m === 1e9 ? 0 : m
                    }
                    readonly property real gmax: {
                        var m = -1e9
                        for (var i = 0; i < days.length; i++)
                            if (days[i].max != null && days[i].max > m) m = days[i].max
                        return m === -1e9 ? 1 : m
                    }
                    readonly property real faixa: Math.max(1, gmax - gmin)

                    // now
                    Item {
                        width: parent.width
                        height: 90

                        Row {
                            spacing: 16
                            anchors.verticalCenter: parent.verticalCenter

                            Text {
                                anchors.verticalCenter: parent.verticalCenter
                                text: w.d.icon
                                font.pixelSize: 54
                                color: scene.acento(1)
                            }
                            Column {
                                anchors.verticalCenter: parent.verticalCenter

                                Text {
                                    text: scene.fmt(w.d.temp, "°")
                                    font.pixelSize: 58
                                    font.bold: true
                                    color: "#ffffff"
                                }
                                Text {
                                    text: w.d.description
                                    font.pixelSize: 15
                                    color: "#8fb4d8"
                                }
                            }
                        }

                        Column {
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            spacing: 4

                            Text {
                                anchors.right: parent.right
                                text: "Feels like " + scene.fmt(w.d.sensacao, "°")
                                font.pixelSize: 14
                                color: "#cfe3f5"
                            }
                            Text {
                                anchors.right: parent.right
                                text: scene.fmt(w.d.min, "°") + " to " + scene.fmt(w.d.max, "°") + " today"
                                font.pixelSize: 13
                                color: "#8fb4d8"
                            }
                        }
                    }

                    // metrics with proportional bars
                    Row {
                        spacing: 10

                        Repeater {
                            model: [
                                { r: "Humidity", v: scene.fmt(w.d.umidade, "%"), p: w.d.umidade },
                                { r: "Rain",     v: scene.fmt(w.d.rain, "%"),   p: w.d.rain },
                                { r: "Wind",     v: scene.fmt(w.d.vento, " km/h"), p: w.d.vento == null ? 0 : w.d.vento * 2 }
                            ]

                            Rectangle {
                                width: (content.width - 20) / 3
                                height: 62
                                radius: 8
                                color: "#12ffffff"
                                border.width: 1
                                border.color: "#22ffffff"

                                Column {
                                    x: 10
                                    y: 9
                                    spacing: 3

                                    Text {
                                        text: modelData.r
                                        font.pixelSize: 11
                                        color: "#7f9fc0"
                                    }
                                    Text {
                                        text: modelData.v
                                        font.pixelSize: 17
                                        font.bold: true
                                        color: "#eaf6ff"
                                    }
                                }

                                Rectangle {
                                    x: 10
                                    y: parent.height - 11
                                    width: parent.width - 20
                                    height: 3
                                    radius: 1.5
                                    color: "#22ffffff"

                                    Rectangle {
                                        width: parent.width
                                              * Math.max(0, Math.min(1, (modelData.p || 0) / 100))
                                              * w.input
                                        height: parent.height
                                        radius: parent.radius
                                        color: scene.acento(0.9)
                                    }
                                }
                            }
                        }
                    }

                    Rectangle { width: parent.width; height: 1; color: "#26ffffff" }

                    // today, tomorrow, and the day after: min-max range on the same scale
                    Column {
                        width: parent.width
                        spacing: 8

                        Repeater {
                            model: weather.days

                            Item {
                                width: weather.width
                                height: 26

                                Text {
                                    x: 0
                                    width: 80
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: modelData.day
                                    font.pixelSize: 14
                                    color: "#cfe3f5"
                                }
                                Text {
                                    x: 82
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: modelData.icon
                                    font.pixelSize: 16
                                    color: scene.acento(0.9)
                                }
                                Text {
                                    x: 108
                                    width: 38
                                    anchors.verticalCenter: parent.verticalCenter
                                    horizontalAlignment: Text.AlignRight
                                    text: scene.fmt(modelData.min, "°")
                                    font.pixelSize: 13
                                    color: "#8fb4d8"
                                }

                                // track + daily range (+ dot: current temperature)
                                Rectangle {
                                    id: trilho
                                    x: 154
                                    width: parent.width - 154 - 100
                                    height: 4
                                    radius: 2
                                    anchors.verticalCenter: parent.verticalCenter
                                    color: "#1fffffff"

                                    Rectangle {
                                        visible: modelData.min != null && modelData.max != null
                                        x: (modelData.min - weather.gmin) / weather.faixa * trilho.width
                                        width: Math.max(6, (modelData.max - modelData.min) / weather.faixa * trilho.width)
                                               * w.input
                                        height: parent.height
                                        radius: 2
                                        color: scene.acento(0.85)
                                    }
                                    Rectangle {
                                        visible: index === 0 && w.d.temp != null
                                        x: Math.max(-4, Math.min(trilho.width - 4,
                                               (w.d.temp - weather.gmin) / weather.faixa * trilho.width - 4))
                                        y: -2
                                        width: 8
                                        height: 8
                                        radius: 4
                                        color: "#ffffff"
                                    }
                                }

                                Text {
                                    x: trilho.x + trilho.width + 8
                                    width: 36
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: scene.fmt(modelData.max, "°")
                                    font.pixelSize: 13
                                    color: "#eaf6ff"
                                }
                                Text {
                                    x: parent.width - 48
                                    width: 48
                                    anchors.verticalCenter: parent.verticalCenter
                                    horizontalAlignment: Text.AlignRight
                                    text: scene.fmt(modelData.rain, "%")
                                    font.pixelSize: 13
                                    color: (modelData.rain != null && modelData.rain >= 50) ? "#ffb02a" : "#7f9fc0"
                                }
                            }
                        }
                    }
                }
            }

            Component {
                id: compSchedule

                Item {
                    id: schedule
                    width: content.width

                    readonly property var items: w.d.items || []
                    readonly property int n: Math.min(items.length, 6)

                    height: n === 0 ? empty.implicitHeight + 4 : list.implicitHeight

                    // timeline
                    Rectangle {
                        visible: schedule.n > 1
                        x: 70
                        y: 12
                        width: 1
                        height: Math.max(0, list.implicitHeight - 24 - (schedule.items.length > 6 ? 24 : 0))
                        color: scene.acento(0.35)
                    }

                    Text {
                        id: empty
                        visible: schedule.n === 0
                        text: "No events found"
                        font.pixelSize: 16
                        color: "#8fb4d8"
                    }

                    Column {
                        id: list
                        width: parent.width
                        spacing: 4

                        Repeater {
                            model: schedule.items.slice(0, 6)

                            Item {
                                width: list.width
                                height: Math.max(30, title.implicitHeight + 8)
                                // appear in sequence with the card
                                opacity: Math.max(0, Math.min(1, w.input * (schedule.n + 1) - index))

                                Text {
                                    x: 0
                                    y: 3
                                    width: 58
                                    text: modelData.time || ""
                                    font.pixelSize: 15
                                    font.bold: true
                                    color: scene.acento(1)
                                }
                                Rectangle {
                                    x: 66
                                    y: 9
                                    width: 9
                                    height: 9
                                    radius: 4.5
                                    color: "#061a38"
                                    border.width: 1.5
                                    border.color: scene.acento(0.9)
                                }
                                Text {
                                    id: title
                                    x: 90
                                    y: 3
                                    width: parent.width - 90
                                    text: modelData.title
                                    wrapMode: Text.Wrap
                                    maximumLineCount: 2
                                    elide: Text.ElideRight
                                    font.pixelSize: 16
                                    color: "#eaf6ff"
                                }
                            }
                        }

                        Text {
                            visible: schedule.items.length > 6
                            x: 90
                            text: "+" + (schedule.items.length - 6) + " more"
                            font.pixelSize: 13
                            color: "#7f9fc0"
                        }
                    }
                }
            }

            Component {
                id: compNews

                Column {
                    width: content.width
                    spacing: 12

                    // main headline, with a highlight accent
                    Item {
                        width: parent.width
                        height: destaque.implicitHeight

                        Rectangle {
                            width: 3
                            height: parent.height
                            radius: 1.5
                            color: scene.acento(0.9)
                        }

                        Column {
                            id: destaque
                            x: 16
                            width: parent.width - 16
                            spacing: 5

                            Text {
                                width: parent.width
                                text: w.d.items[0].headline
                                wrapMode: Text.Wrap
                                maximumLineCount: 3
                                elide: Text.ElideRight
                                font.pixelSize: 22
                                font.bold: true
                                color: "#f4faff"
                            }
                            Text {
                                visible: text !== ""
                                text: w.d.items[0].source
                                font.pixelSize: 12
                                color: scene.acento(0.9)
                            }
                        }
                    }

                    Rectangle {
                        visible: w.d.items.length > 1
                        width: parent.width
                        height: 1
                        color: "#26ffffff"
                    }

                    Repeater {
                        model: w.d.items.slice(1)

                        Item {
                            width: content.width
                            height: item.implicitHeight

                            Rectangle {
                                x: 0
                                y: 7
                                width: 5
                                height: 5
                                radius: 2.5
                                color: scene.acento(0.7)
                            }

                            Column {
                                id: item
                                x: 16
                                width: parent.width - 16
                                spacing: 2

                                Text {
                                    width: parent.width
                                    text: modelData.headline
                                    wrapMode: Text.Wrap
                                    maximumLineCount: 2
                                    elide: Text.ElideRight
                                    font.pixelSize: 15
                                    color: "#d8ecff"
                                }
                                Text {
                                    visible: text !== ""
                                    text: modelData.source
                                    font.pixelSize: 11
                                    color: "#7f9fc0"
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    // Task panel: right dock, aligned with the orb. High z-order keeps it
    // above the background; independent of scene.fase (also visible in IDLE).
    TaskIndicator {
        id: indicador
        z: 30
        colorBase: scene.active ? scene.acento(1) : "#5aa9ff"
        x: scene.graficoX + (1 - opacity) * 36 * scene.dockScale
        y: Math.max(96 * scene.scale, scene.centerY - height * scene.dockScale / 2)
        scale: scene.dockScale
        transformOrigin: Item.TopLeft
        Behavior on y { NumberAnimation { duration: 300; easing.type: Easing.OutCubic } }
    }

    // Caption (when a widget is present, the spoken response appears below it).
    Text {
        opacity: scene.phase
        visible: scene.hasWidget && scene.textResponse !== "" && opacity > 0.01
        width: scene.hasTask
               ? Math.max(120, Math.min(scene.width - 2 * (scene.dockLarg + 96 * scene.scale), 760))
               : Math.min(scene.dockX - 96 * scene.scale, 760)
        x: scene.centerX - width / 2
        y: scene.height - 175 * scene.scale
        horizontalAlignment: Text.AlignHCenter
        text: scene.tail(scene.textResponse)
        wrapMode: Text.Wrap
        maximumLineCount: 2
        elide: Text.ElideRight
        font.pixelSize: 20
        color: "#e6f3ff"
    }

    // Command confirmation, directly in the environment (click it or say "yes").
    Rectangle {
        visible: scene.confirming && scene.phase > 0.5
        width: Math.min(scene.width - 160, 460)
        height: card.implicitHeight + 28
        x: scene.centerX - width / 2
        y: scene.centerY + 250 * scene.scale      // below the orb
        radius: 12
        color: "#e01a1206"
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
                text: "May I " + scene.descriptionCommand + "?"
                wrapMode: Text.Wrap
                font.pixelSize: 16
                color: "#ffe2a8"
            }

            Row {
                spacing: 10

                Rectangle {
                    width: (card.width - 10) / 2
                    height: 38
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
                    height: 38
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
                font.pixelSize: 13
                color: "#99ffd27a"
            }
        }
    }
}
