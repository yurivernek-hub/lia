#include "bridge.h"

#include <QCoreApplication>
#include <QDebug>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonParseError>
#include <QJsonValue>

namespace {

// If Jarvis disappears, the "child" interface closes by itself after this interval.
constexpr int kSecondsWithoutJarvis = 20;

QString pathSocket()
{
    QString base = qEnvironmentVariable("XDG_RUNTIME_DIR");
    if (base.isEmpty()) {
        base = QStringLiteral("/tmp");
    }
    return base + QStringLiteral("/jarvis.sock");
}

} // namespace

Bridge::Bridge(bool child, QObject *parent)
    : QObject(parent)
    , m_child(child)
{
    connect(&m_socket, &QLocalSocket::readyRead, this, &Bridge::readData);
    connect(&m_socket, &QLocalSocket::disconnected, this, &Bridge::disconnected);

    // Try to (re)connect every second while disconnected.
    m_timer.setInterval(1000);
    connect(&m_timer, &QTimer::timeout, this, &Bridge::tryConnect);
    m_timer.start();

    tryConnect();
}

void Bridge::tryConnect()
{
    switch (m_socket.state()) {
    case QLocalSocket::ConnectedState:
        m_semConexao = 0;
        return;

    case QLocalSocket::UnconnectedState:
        ++m_semConexao;
        if (m_child && m_semConexao >= kSecondsWithoutJarvis) {
            qInfo() << "[fundo] Jarvis não está mais rodando; fechando.";
            QCoreApplication::quit();
            return;
        }
        m_socket.connectToServer(pathSocket());
        return;

    default:
        return;
    }
}

void Bridge::readData()
{
    m_buffer += m_socket.readAll();

    int indice = 0;
    while ((indice = m_buffer.indexOf('\n')) >= 0) {
        const QByteArray line = m_buffer.left(indice).trimmed();
        m_buffer.remove(0, indice + 1);

        if (!line.isEmpty()) {
            emit event(QString::fromUtf8(line));
        }
    }
}

void Bridge::disconnected()
{
    m_buffer.clear();
    emit event(QStringLiteral("{\"event\": \"state\", \"state\": \"sleeping\"}"));
}

void Bridge::send(const QString &action)
{
    if (m_socket.state() != QLocalSocket::ConnectedState) {
        return;
    }

    QJsonObject objeto;
    objeto.insert(QStringLiteral("action"), action);

    m_socket.write(QJsonDocument(objeto).toJson(QJsonDocument::Compact) + '\n');
    m_socket.flush();
}

void Bridge::context(const QString &json)
{
    QJsonParseError error;
    const QJsonDocument documento = QJsonDocument::fromJson(json.toUtf8(), &error);

    if (error.error != QJsonParseError::NoError || !documento.isObject()) {
        return;
    }

    QStringList busy;

    const QJsonArray outputs = documento.object().value(QStringLiteral("outputs")).toArray();
    for (const QJsonValue &value : outputs) {
        const QJsonObject output = value.toObject();
        if (!output.value(QStringLiteral("free")).toBool(true)) {
            busy << output.value(QStringLiteral("name")).toString();
        }
    }

    busy.sort();

    if (busy == m_busy) {
        return;
    }

    m_busy = busy;
    qInfo().noquote() << "[fundo] monitores ocupados:"
                      << (busy.isEmpty() ? QStringLiteral("(nenhum)") : busy.join(QStringLiteral(", ")));
    emit busyChanged();
}