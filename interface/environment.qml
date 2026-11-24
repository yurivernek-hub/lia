import QtQuick
import QtQuick.Window
import QtQml.Models
import "../background"

// FALLBACK ENVIRONMENT mode (used only when the C++ background was not
// compiled): one transparent window per "clear" monitor, just above the
// wallpaper, which does not capture clicks. The drawing is the same as the real
// background (background/EnvironmentScene.qml), without the opaque backdrop.
Instantiator {
    id: root
    model: bridge.outputsFree

    delegate: Window {
        id: amb

        property string output: modelData
        property var targetScreen: amb.screenOfOutput()

        title: "Jarvis Ambient " + output
        color: "transparent"
        flags: Qt.FramelessWindowHint
               | Qt.WindowTransparentForInput
               | Qt.WindowDoesNotAcceptFocus
        screen: targetScreen
        x: targetScreen ? targetScreen.virtualX : 0
        y: targetScreen ? targetScreen.virtualY : 0
        width: targetScreen ? targetScreen.width : 800
        height: targetScreen ? targetScreen.height : 600
        visible: true

        function screenOfOutput() {
            var l = Qt.application.screens
            for (var i = 0; i < l.length; i++)
                if (l[i].name === output) return l[i]
            return l.length > 0 ? l[0] : null
        }

        EnvironmentScene {
            anchors.fill: parent
            output: amb.output
            busy: false
            backgroundOpaque: false
        }
    }
}
