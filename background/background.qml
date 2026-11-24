import QtQuick
import QtQuick.Window

// Jarvis background: one window per monitor, created by main.cpp
// as a layer-shell surface. All drawing is in EnvironmentScene.qml.
Window {
    id: window

    // Set by main.cpp.
    property string output: ""

    title: "Jarvis Fundo " + output
    color: "#05080e"

    EnvironmentScene {
        anchors.fill: parent
        output: window.output
        busy: bridge.busy.indexOf(window.output) >= 0
        image: imageBackground
        backgroundOpaque: true
    }
}
