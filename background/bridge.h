#pragma once

#include <QByteArray>
#include <QLocalSocket>
#include <QObject>
#include <QString>
#include <QStringList>
#include <QTimer>

// Connects the background to Jarvis (local socket) and the KWin script (D-Bus).
class Bridge : public QObject
{
    Q_OBJECT
    Q_CLASSINFO("D-Bus Interface", "org.jarvisb.Background")

    // Monitors covered by applications (the layer is hidden and does not animate).
    Q_PROPERTY(QStringList busy READ busy NOTIFY busyChanged)

public:
    explicit Bridge(bool child, QObject *parent = nullptr);

    QStringList busy() const { return m_busy; }

public slots:
    // Called by the KWin script (D-Bus): {"outputs":[{"name":..,"free":..}]}
    void context(const QString &json);

    // Called by QML: "confirm" / "cancel".
    void send(const QString &action);

signals:
    void event(const QString &json);
    void busyChanged();

private:
    void tryConnect();
    void readData();
    void disconnected();

    QLocalSocket m_socket;
    QTimer m_timer;
    QByteArray m_buffer;
    QStringList m_busy;
    bool m_child;
    int m_semConexao = 0;
};