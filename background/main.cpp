// Jarvis B - background: the desktop background as a program.
//
// One wlr-layer-shell surface per monitor, on the "bottom" layer (below
// all windows, above the Plasma desktop). Drawing and
// animation are in background.qml; this file only handles:
//   - layer configuration (LayerShellQt);
//   - the connection to Jarvis (bridge.cpp).
// In IDLE, nothing is redrawn: the layer exists but costs almost nothing.

#include <QDBusConnection>
#include <QDebug>
#include <QDir>
#include <QFile>
#include <QGuiApplication>
#include <QHash>
#include <QQmlComponent>
#include <QQmlContext>
#include <QQmlEngine>
#include <QQuickWindow>
#include <QScreen>
#include <QUrl>
#include <QVariantMap>

#include <LayerShellQt/Window>

#include <csignal>
#include <sys/types.h>
#include <unistd.h>

#include "bridge.h"

// Only one background can run: close any existing instances.
static void finishOtherInstances()
{
    const qint64 meuPid = QCoreApplication::applicationPid();
    const QStringList entradas = QDir(QStringLiteral("/proc")).entryList(QDir::Dirs | QDir::NoDotAndDotDot);

    bool matou = false;

    for (const QString &name : entradas) {
        bool number = false;
        const qint64 pid = name.toLongLong(&number);
        if (!number || pid == meuPid) {
            continue;
        }

        QFile file(QStringLiteral("/proc/%1/cmdline").arg(name));
        if (!file.open(QIODevice::ReadOnly)) {
            continue;
        }

        const QList<QByteArray> parts = file.readAll().split('\0');
        if (parts.isEmpty() || !parts.first().endsWith("jarvisb-background")) {
            continue;
        }

        kill(static_cast<pid_t>(pid), SIGTERM);
        matou = true;
    }

    if (matou) {
        usleep(300000); // allow time to release the name on D-Bus
    }
}

int main(int argc, char *argv[])
{
    finishOtherInstances();

    // Make Qt use layer-shell instead of a regular window.
    qputenv("QT_WAYLAND_SHELL_INTEGRATION", "layer-shell");

    QGuiApplication app(argc, argv);
    app.setApplicationName(QStringLiteral("jarvisb-background"));
    app.setQuitOnLastWindowClosed(false);

    bool child = false;
    QString image;

    for (const QString &argumento : app.arguments()) {
        if (argumento == QLatin1String("--child")) {
            child = true;
        } else if (argumento.startsWith(QLatin1String("--imagem="))) {
            image = argumento.mid(9);
        }
    }

    Bridge bridge(child);

    // The KWin script reports here which monitors are occupied.
    QDBusConnection barramento = QDBusConnection::sessionBus();
    if (barramento.isConnected() && barramento.registerService(QStringLiteral("org.jarvisb.background"))) {
        barramento.registerObject(QStringLiteral("/background"), &bridge, QDBusConnection::ExportAllSlots);
    } else {
        qWarning() << "[fundo] D-Bus indisponível: o fundo anima em todos os monitores.";
    }

    QQmlEngine motor;
    motor.rootContext()->setContextProperty(QStringLiteral("bridge"), &bridge);
    motor.rootContext()->setContextProperty(
        QStringLiteral("imageBackground"),
        image.isEmpty() ? QUrl() : QUrl::fromLocalFile(image));

    QQmlComponent componente(&motor, QUrl::fromLocalFile(QString::fromUtf8(QML_DIR "/background.qml")));
    if (componente.isError()) {
        qCritical().noquote() << componente.errorString();
        return 1;
    }

    QHash<QScreen *, QQuickWindow *> janelas;

    auto create = [&](QScreen *screen) {
        if (janelas.contains(screen)) {
            return;
        }

        QVariantMap propriedades;
        propriedades.insert(QStringLiteral("output"), screen->name());

        QObject *objeto = componente.createWithInitialProperties(propriedades);
        if (!objeto) {
            qCritical().noquote() << componente.errorString();
            return;
        }

        auto *window = qobject_cast<QQuickWindow *>(objeto);
        if (!window) {
            qCritical() << "[fundo] background.qml precisa ter uma Window como raiz.";
            delete objeto;
            return;
        }

        window->setScreen(screen);

        // "bottom" layer: below windows, above the desktop
        // desktop; covers the entire monitor (exclusive zone -1 = ignores
        // panels) and does not take keyboard focus.
        // KWin determines the window TYPE from the "scope" (namespace): an unknown name
        // becomes a NORMAL window (disappears with "Show Desktop"
        // and counts as an application covering the monitor). "desktop"
        // makes KWin treat it as the desktop.
        if (auto *camada = LayerShellQt::Window::get(window)) {
            camada->setLayer(LayerShellQt::Window::LayerBottom);
            camada->setAnchors(LayerShellQt::Window::Anchors(
                LayerShellQt::Window::AnchorTop | LayerShellQt::Window::AnchorBottom
                | LayerShellQt::Window::AnchorLeft | LayerShellQt::Window::AnchorRight));
            camada->setExclusiveZone(-1);
            camada->setKeyboardInteractivity(LayerShellQt::Window::KeyboardInteractivityNone);
            camada->setScope(QStringLiteral("desktop"));
        }

        window->show();
        janelas.insert(screen, window);
    };

    const QList<QScreen *> telas = QGuiApplication::screens();
    for (QScreen *screen : telas) {
        create(screen);
    }

    QObject::connect(&app, &QGuiApplication::screenAdded, &app, create);
    QObject::connect(&app, &QGuiApplication::screenRemoved, &app, [&](QScreen *screen) {
        delete janelas.take(screen);
    });

    return app.exec();
}