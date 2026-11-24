import QtQuick

// Task panel (dock on the right): one card per task, with a chart
// made of bars (one bar per step).
//   green     = step succeeded
//   red       = step failed
//   pulsing   = step in progress
//
// Fed by "task" events (background_tasks.py):
//   started   {id, title, second_plan}
//   step      {id, n, description, tool}
//   result    {id, success}
//   completed | error | cancelled | limit  {id, state}
//   spoken    {id}   (Lia finished announcing the result: remove the card)
//
// Show/hide rules:
//  - only tasks that ran a command or changed a file appear (a step with
//    executar_comando, criar_arquivo, or editar_arquivo). Questions such as
//    weather, news, or search never appear, even if they take a long time;
//  - among those, background tasks appear immediately; foreground tasks only
//    after 4 seconds (a quick request should not change the layout);
//  - a completed background task stays on screen (even while you are
//    chatting) until Lia announces the result; voice_control.py emits
//    "task" with state "spoken" when that speech ends, and the card
//    DISAPPEAR IMMEDIATELY (without waiting for the timer);
//  - the others disappear 6 seconds after completion.
// QtQuick only (no QtQuick.Controls).
Item {
    id: indicador

    property color colorBase: "#5aa9ff"
    property color colorOk: "#4fd18b"
    property color colorFailure: "#ff6b6b"

    // Number of tasks currently visible (tracked manually; ListModel.get()
    // does not reliably notify bindings).
    property int visibleTaskCount: 0
    readonly property bool hasTasks: visibleTaskCount > 0

    property double nowMs: Date.now()

    // Tools that modify the computer. Only tasks that use one of them
    // appear in this panel; questions (weather, news, search...) never do.
    readonly property var toolsTecnicas: ["executar_comando", "criar_arquivo", "editar_arquivo"]

    // Pulse for the step in progress.
    property real phasePulso: 0
    SequentialAnimation on phasePulso {
        loops: Animation.Infinite
        running: indicador.hasTasks
        NumberAnimation { from: 0; to: 1; duration: 700; easing.type: Easing.InOutSine }
        NumberAnimation { from: 1; to: 0; duration: 700; easing.type: Easing.InOutSine }
    }

    width: 520
    height: hasTasks ? list.implicitHeight + 28 : 0
    opacity: hasTasks ? 1 : 0
    visible: opacity > 0.01

    Behavior on opacity { NumberAnimation { duration: 300 } }

    ListModel { id: tasks }

    function indiceOf(id) {
        for (var i = 0; i < tasks.count; i++)
            if (tasks.get(i).tid === id) return i
        return -1
    }

    function readSteps(i) {
        try { return JSON.parse(tasks.get(i).steps) } catch (e) { return [] }
    }

    function recordSteps(i, list) {
        tasks.setProperty(i, "steps", JSON.stringify(list))
    }

    function recontar() {
        var n = 0
        for (var i = 0; i < tasks.count; i++)
            if (tasks.get(i).visible) n++
        indicador.visibleTaskCount = n
    }

    // Lia finished announcing this task’s result: remove the card immediately.
    function resultSpoken(id) {
        var i = indiceOf(id)
        console.log("[indicador] resultSpoken id=" + id + " index=" + i)
        if (i >= 0) tasks.remove(i)
        recontar()
    }

    // Receives a "task" event from the event bus.
    function receive(m) {
        // The result was spoken to the end (voice_control.py): remove it now.
        if (m.state === "spoken") {
            resultSpoken(m.id)
            return
        }

        var i = indiceOf(m.id)

        if (m.state === "started") {
            if (i < 0) {
                tasks.append({
                    tid: m.id,
                    title: m.title || "task",
                    steps: "[]",
                    state: "active",
                    startMs: Date.now(),
                    finishedAt: 0,
                    secondPlan: m.second_plan === true,
                    // only appears after a technical tool has been used
                    technical: false,
                    visible: false
                })
            } else if (m.second_plan === true) {
                // a task that was in the foreground and moved to the background
                tasks.setProperty(i, "secondPlan", true)
                if (tasks.get(i).technical)
                    tasks.setProperty(i, "visible", true)
            }
            recontar()
            return
        }

        if (i < 0) return

        if (m.state === "step") {
            var list = readSteps(i)
            list.push({ n: m.n, description: m.description || "", ok: null })
            recordSteps(i, list)

            // Ran a command or changed a file: this is now a
            // indeed. Background tasks appear immediately; foreground tasks
            // only if it exceeds 4 seconds (see the timer below).
            if (toolsTecnicas.indexOf(m.tool) >= 0) {
                tasks.setProperty(i, "technical", true)
                if (tasks.get(i).secondPlan)
                    tasks.setProperty(i, "visible", true)
            }
        } else if (m.state === "result") {
            var steps = readSteps(i)
            if (steps.length > 0) steps[steps.length - 1].ok = (m.success === true)
            recordSteps(i, steps)
        } else if (m.state === "completed" || m.state === "error" || m.state === "cancelled" || m.state === "limit") {
            tasks.setProperty(i, "state", m.state)
            tasks.setProperty(i, "finishedAt", Date.now())
        }
        recontar()
    }

    // Timer: updates elapsed times, reveals long tasks, and removes old ones.
    Timer {
        interval: 500
        repeat: true
        running: tasks.count > 0
        onTriggered: {
            var now = Date.now()
            indicador.nowMs = now

            for (var i = tasks.count - 1; i >= 0; i--) {
                var t = tasks.get(i)

                if (t.state === "active") {
                    if (!t.visible && t.technical && now - t.startMs > 4000)
                        tasks.setProperty(i, "visible", true)
                    continue
                }

                var remove = false
                if (!t.visible) {
                    remove = true
                } else if (t.secondPlan && (t.state === "completed" || t.state === "error")) {
                    // safety net: normally removed by resultadoFalado()
                    remove = (now - t.finishedAt > 180000)
                } else {
                    remove = (now - t.finishedAt > 6000)
                }

                if (remove) tasks.remove(i)
            }

            indicador.recontar()
        }
    }

    Rectangle {
        anchors.fill: parent
        radius: 14
        color: Qt.rgba(0.02, 0.05, 0.09, 0.82)
        border.width: 1.5
        border.color: Qt.rgba(indicador.colorBase.r, indicador.colorBase.g, indicador.colorBase.b, 0.55)
    }

    Column {
        id: list
        x: 14
        y: 14
        width: parent.width - 28
        spacing: 16

        Repeater {
            model: tasks

            delegate: Column {
                id: cardTask

                width: list.width
                spacing: 6
                visible: model.visible

                property var stepsTask: JSON.parse(model.steps)
                property string stateTask: model.state
                readonly property int n: stepsTask.length
                readonly property bool finalizada: stateTask !== "active"
                readonly property string detalhe: n > 0
                    ? String(stepsTask[n - 1].description)
                    : "starting"
                readonly property double reference: finalizada ? model.finishedAt : indicador.nowMs
                readonly property int seconds: Math.max(0, Math.floor((reference - model.startMs) / 1000))

                // Title and elapsed time.
                Item {
                    width: cardTask.width
                    height: 16

                    Text {
                        anchors.left: parent.left
                        anchors.right: time.left
                        anchors.rightMargin: 8
                        elide: Text.ElideRight
                        font.pixelSize: 12
                        font.letterSpacing: 1
                        color: Qt.rgba(indicador.colorBase.r, indicador.colorBase.g, indicador.colorBase.b, 0.95)
                        text: (model.secondPlan ? "BACKGROUND · " : "TASK · ")
                              + String(model.title).toUpperCase()
                    }

                    Text {
                        id: time
                        anchors.right: parent.right
                        font.pixelSize: 12
                        color: Qt.rgba(1, 1, 1, 0.6)
                        text: cardTask.seconds < 60
                              ? cardTask.seconds + "s"
                              : Math.floor(cardTask.seconds / 60) + "m " + (cardTask.seconds % 60) + "s"
                    }
                }

                // Most recent step.
                Text {
                    width: cardTask.width
                    elide: Text.ElideRight
                    font.pixelSize: 12
                    color: Qt.rgba(1, 1, 1, 0.7)
                    text: cardTask.detalhe
                }

                // Chart: one bar per step.
                Item {
                    width: cardTask.width
                    height: 72

                    Rectangle {
                        anchors.bottom: parent.bottom
                        width: parent.width
                        height: 1
                        color: Qt.rgba(1, 1, 1, 0.12)
                    }

                    Row {
                        anchors.bottom: parent.bottom
                        height: 72
                        spacing: 3

                        Repeater {
                            model: cardTask.stepsTask

                            delegate: Rectangle {
                                readonly property int result: modelData.ok === true ? 1 : (modelData.ok === false ? 0 : -1)
                                readonly property bool current: index === cardTask.n - 1 && !cardTask.finalizada

                                width: Math.max(3, Math.min(16, (cardTask.width - (cardTask.n - 1) * 3) / Math.max(cardTask.n, 1)))
                                radius: 2
                                height: result === 1 ? 70 : (result === 0 ? 38 : 54)
                                y: 72 - height
                                color: result === 1
                                       ? indicador.colorOk
                                       : (result === 0
                                          ? indicador.colorFailure
                                          : Qt.rgba(indicador.colorBase.r, indicador.colorBase.g,
                                                    indicador.colorBase.b,
                                                    current ? 0.4 + 0.5 * indicador.phasePulso : 0.35))

                                Behavior on height { NumberAnimation { duration: 220 } }
                                Behavior on color { ColorAnimation { duration: 220 } }
                            }
                        }
                    }
                }

                // Footer: progress or outcome.
                Text {
                    font.pixelSize: 11
                    color: Qt.rgba(1, 1, 1, 0.55)
                    text: cardTask.finalizada
                          ? (cardTask.stateTask === "completed" ? "done"
                             : (cardTask.stateTask === "error" ? "failed" : "stopped"))
                            + " · " + cardTask.n + " steps"
                          : "step " + cardTask.n
                }
            }
        }
    }
}
