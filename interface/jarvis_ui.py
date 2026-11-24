"""
Interface do Jarvis B: process separado que só escuta o socket do
Jarvis e desenha. Se esta janela travar ou fechar, o Jarvis continua
funcionando normalmente.

Dois modos de apresentação, escolhidos por context:
  - AMBIENTE (environment.qml): nos monitores cuja área de trabalho está
    livre (sem aplicativos em cima), uma janela transparente logo acima
    do papel de parede: estrutura cinza e estática no IDLE, que acorda
    quando você chama o Jarvis.
  - POP-UP (panel.qml): quando todos os monitores estão ocupados por
    aplicativos (ou quando precisa de um clique, como a confirmação).

Quem informa quais monitores estão livres é o script do KWin
(kwin_center.js), por D-Bus (org.jarvisb.ui).

Uso (o jarvisb.py já abre isto sozinho, mas dá pra rodar à mão):
    python jarvis_ui.py                    # Native Wayland (default)
    python jarvis_ui.py --xcb              # force XWayland/X11
    python jarvis_ui.py --environment-always  # treat ALL monitors as
                                           # clear (testing / outside KDE)
    python jarvis_ui.py --external-environment # the environment is the C++ background
                                           # (background/); do not create its own
"""

import json
import os
import signal
import sys

if "--xcb" in sys.argv:
    os.environ["QT_QPA_PLATFORM"] = "xcb"

from PySide6.QtCore import (
    ClassInfo,
    Property,
    QObject,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtDBus import QDBusConnection
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtQml import QQmlApplicationEngine

from kde import load_script,ensure_rule,log_script

# Opened by jarvisb.py (--child): if Jarvis disappears, it closes itself
# after this period without a connection (prevents a duplicate "orphaned" interface).
SECONDS_WITHOUT_JARVIS = 20

FOLDER = os.path.dirname(os.path.abspath(__file__))


def finish_other_instances():
    """Só pode haver uma interface: fecha as que já estiverem rodando."""

    meu_pid = os.getpid()

    for name in os.listdir("/proc"):

        if not name.isdigit() or int(name) == meu_pid:
            continue

        try:

            with open(f"/proc/{name}/cmdline","rb") as f:
                command = f.read().replace(b"\0",b" ").decode(
                    "utf-8","ignore"
                )

        except OSError:
            continue

        if "jarvis_ui.py" in command and "python" in command:

            try:
                os.kill(int(name),signal.SIGTERM)
            except OSError:
                pass


def socket_path():

    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"

    return os.path.join(base,"jarvis.sock")


@ClassInfo(**{"D-Bus Interface": "org.jarvisb.Ui"})
class DBusService(QObject):
    """Recebe do script do KWin o context (monitores livres/ocupados)."""

    context_recebido = Signal(str)

    @Slot(str)
    def context(self,text):
        self.context_recebido.emit(text)


class Bridge(QObject):
    """Liga o socket do Jarvis (e o D-Bus do KWin) ao QML."""

    event = Signal(str)
    outputsFreeChanged = Signal()
    popupAllowedChanged = Signal()

    def __init__(self,child=False,environment_clickable=False):

        super().__init__()

        self._child = child
        self._environment_clickable = environment_clickable  # the C++ background receives clicks
        self.window = None  # panel window (for dragging)
        self._without_connection = 0
        self._logs_restantes = 2
        self._buffer = b""

        self._free = []          # monitors with a clear desktop
        self._force_popup = False  # e.g. confirmation requires a click

        self._socket = QLocalSocket(self)
        self._socket.readyRead.connect(self._read)
        self._socket.disconnected.connect(self._disconnected)

        # Try to (re)connect every second while disconnected.
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._try)
        self._timer.start()

        self._try()

    # --- properties read by QML ---

    def _get_free(self):
        return list(self._free)

    outputsFree = Property(list,_get_free,notify=outputsFreeChanged)

    def _get_popup(self):
        return (not self._free) or self._force_popup

    popupAllowed = Property(bool,_get_popup,notify=popupAllowedChanged)

    # --- context (clear monitors) ---

    def setFree(self,names):

        names = sorted(names)

        if names == self._free:
            return

        self._free = names

        self.outputsFreeChanged.emit()
        self.popupAllowedChanged.emit()

        print(
            f"[interface] monitores livres: "
            f"{', '.join(names) if names else '(nenhum) -> pop-up'}",
            flush=True
        )

    @Slot(str)
    def processContext(self,text):

        try:
            data = json.loads(text)
        except ValueError:
            return

        for s in data.get("outputs",[]):

            if not s.get("free") and s.get("name"):

                print(
                    f"[interface] {s['name']}: ocupado por "
                    f"{s.get('por') or '?'}",
                    flush=True
                )

        self.setFree([
            s["name"]
            for s in data.get("outputs",[])
            if s.get("free") and s.get("name")
        ])

    def _force(self,value):

        if self._force_popup != value:
            self._force_popup = value
            self.popupAllowedChanged.emit()

    # --- socket connection to Jarvis ---

    def _try(self):

        state = self._socket.state()

        if state == QLocalSocket.LocalSocketState.ConnectedState:
            self._without_connection = 0
            return

        if state == QLocalSocket.LocalSocketState.UnconnectedState:

            self._without_connection += 1

            if self._child and self._without_connection >= SECONDS_WITHOUT_JARVIS:

                print("[interface] Jarvis não está mais rodando; fechando.")
                QGuiApplication.quit()

                return

            self._socket.connectToServer(socket_path())

    def _print_log(self):
        """Mostra o que o script do KWin registrou (diagnóstico)."""

        lines = log_script()

        if not lines:

            print(
                "[interface] nenhuma mensagem do script do KWin no "
                "journal (ele pode não ter carregado).",
                flush=True
            )

            return

        print("[interface] log do script do KWin:",flush=True)

        for line in lines:
            print(f"    {line}",flush=True)

    def _read(self):

        self._buffer += bytes(self._socket.readAll())

        while b"\n" in self._buffer:

            line,self._buffer = self._buffer.split(b"\n",1)

            if not line.strip():
                continue

            # Confirmation requires a click: the environment does not receive
            # clicks, so confirmation uses the popup. Return to normal
            # when Jarvis sleeps. (Update BEFORE forwarding the event.)
            if (
                b'"event": "confirmation"' in line
                and not self._environment_clickable
            ):
                self._force(True)

            elif (
                b'"event": "state"' in line
                and b'"sleeping"' in line
            ):
                self._force(False)

            self.event.emit(line.decode("utf-8","replace"))

            if b'"awakened"' in line and self._logs_restantes > 0:

                self._logs_restantes -= 1
                QTimer.singleShot(2500,self._print_log)

    def _disconnected(self):

        self._buffer = b""

        self.event.emit(
            json.dumps({"event": "state","state": "sleeping"})
        )

    def _send_json(self,data):

        if (
            self._socket.state()
            != QLocalSocket.LocalSocketState.ConnectedState
        ):
            return

        line = json.dumps(data) + "\n"

        self._socket.write(line.encode("utf-8"))
        self._socket.flush()

    @Slot(str)
    def send(self,action):
        self._send_json({"action": action})

    @Slot(str)
    def sendText(self,text):
        """Texto digitado no painel: o Jarvis trata como se fosse falado."""
        self._send_json({"action": "text","text": text})

    @Slot()
    def moveWindow(self):
        """Começa a arrastar a janela (o compositor faz o movimento)."""

        if self.window is not None:
            self.window.startSystemMove()


def register_dbus(bridge):
    """Expõe org.jarvisb.ui no D-Bus pro script do KWin avisar o context."""

    barramento = QDBusConnection.sessionBus()

    if not barramento.isConnected():

        print("[interface] D-Bus indisponível: só pop-up.",flush=True)

        return None

    servico = DBusService()
    servico.context_recebido.connect(bridge.processContext)

    if not barramento.registerService("org.jarvisb.ui"):

        print(
            "[interface] não consegui registrar org.jarvisb.ui "
            "(outra interface ainda viva?).",
            flush=True
        )

        return None

    barramento.registerObject(
        "/ui",
        servico,
        QDBusConnection.RegisterOption.ExportAllSlots
    )

    return servico


def main():

    finish_other_instances()

    app = QGuiApplication(sys.argv)

    # Windows disappear and reappear; the process must not exit because of that.
    app.setQuitOnLastWindowClosed(False)

    screen = app.primaryScreen().availableGeometry()

    external = "--external-environment" in sys.argv

    bridge = Bridge(
        child="--child" in sys.argv,
        environment_clickable=external
    )

    if "--environment-always" in sys.argv:

        # Testing / outside KDE: all monitors count as clear.
        bridge.setFree([s.name() for s in app.screens()])

    # The D-Bus service must exist BEFORE the KWin script loads
    # (it sends the context as soon as it starts).
    servico_dbus = register_dbus(bridge)  # noqa: F841 (keep reference)

    try:

        rule_ok = ensure_rule()

        script_ok,detalhe = load_script(
            os.path.join(FOLDER,"kwin_center.js")
        )

        print(
            f"[interface] regra do KWin: "
            f"{'ok' if rule_ok else 'não aplicada'} | "
            f"script: "
            f"{'ok' if script_ok else 'NÃO carregou (' + detalhe + ')'}",
            flush=True
        )

    except Exception as e:
        print(f"[!] Não consegui configurar o KWin: {e}")

    motor = QQmlApplicationEngine()

    context = motor.rootContext()
    context.setContextProperty("bridge",bridge)
    context.setContextProperty("geoX",screen.x())
    context.setContextProperty("geoY",screen.y())
    context.setContextProperty("geoW",screen.width())
    context.setContextProperty("geoH",screen.height())

    motor.load(QUrl.fromLocalFile(os.path.join(FOLDER,"panel.qml")))

    if not motor.rootObjects():
        sys.exit(1)

    bridge.window = motor.rootObjects()[0]

    # Windowed environment mode (fallback): only when there is NO C++ background.
    if not external:
        motor.load(QUrl.fromLocalFile(os.path.join(FOLDER,"environment.qml")))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()