"""
Barramento de events do Jarvis B.

O Jarvis é o SERVIDOR de um socket Unix local
($XDG_RUNTIME_DIR/jarvis.sock) e manda uma linha JSON por event.
A interface conecta como cliente e só escuta (e pode mandar de volta
{"action": "confirm"} ou {"action": "cancel"}).

Regra de ouro: nada aqui pode travar ou derrubar o Jarvis. Sem
cliente conectado, emitir() retorna na hora. Qualquer erro de
socket só derruba aquele cliente.
"""

import json
import os
import socket
import struct
import threading


def socket_path():

    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"

    return os.path.join(base,"jarvis.sock")


class EventBus:

    def __init__(self):

        self.path = socket_path()
        self._server = None
        self._clientes = []
        self._lock = threading.Lock()
        self._last = None
        self._decision = None
        self._text = None

    def start(self):

        try:

            if os.path.exists(self.path):
                os.unlink(self.path)

            server = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            server.bind(self.path)
            os.chmod(self.path,0o600)
            server.listen(2)

        except OSError as e:

            print(f"[!] Barramento de events desativado: {e}")
            return False

        self._server = server

        threading.Thread(target=self._accept,daemon=True).start()

        return True

    def _accept(self):

        while True:

            try:
                connection,_ = self._server.accept()
            except Exception:
                return

            try:

                # Sending has a 200 ms limit, so a stalled client never
                # keeps Jarvis awake.
                connection.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_SNDTIMEO,
                    struct.pack("ll",0,200000)
                )

                # A client that connects during a conversation immediately gets
                # the current state.
                if self._last:
                    connection.sendall(self._encode(self._last))

                with self._lock:
                    self._clientes.append(connection)

                threading.Thread(
                    target=self._read,
                    args=(connection,),
                    daemon=True
                ).start()

            except Exception:
                self._remove(connection)

    def _read(self,connection):

        try:

            with connection.makefile("r",encoding="utf-8") as file:

                for line in file:

                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue

                    action = message.get("action")

                    if action in ("confirm","cancel"):
                        self._decision = action

                    elif action == "text":

                        digitado = str(message.get("text") or "").strip()

                        if digitado:
                            self._text = digitado

        except Exception:
            pass

        finally:
            self._remove(connection)

    def _remove(self,connection):

        with self._lock:

            if connection in self._clientes:
                self._clientes.remove(connection)

        try:
            connection.close()
        except Exception:
            pass

    @staticmethod
    def _encode(message):

        return (
            json.dumps(message,ensure_ascii=False) + "\n"
        ).encode("utf-8")

    def has_clientes(self):
        return bool(self._clientes)

    def emit(self,type,**data):

        try:

            message = {"event": type,**data}

            if type in ("state","confirmation"):
                self._last = message

            with self._lock:
                clientes = list(self._clientes)

            if not clientes:
                return

            pacote = self._encode(message)

            for connection in clientes:

                try:
                    connection.sendall(pacote)
                except Exception:
                    self._remove(connection)

        except Exception:
            pass

    # --- decision from the interface (confirm/cancel button) ---

    def has_decision(self):
        return self._decision is not None

    def get_decision(self):

        decision = self._decision
        self._decision = None

        return decision

    def clear_decision(self):
        self._decision = None

    # --- text typed in the interface (treated as spoken input) ---

    def has_text(self):
        return self._text is not None

    def peek_text(self):
        """Olha o texto digitado SEM consumir (pra detectar 'stop')."""
        return self._text

    def get_text(self):

        text = self._text
        self._text = None

        return text

    def stop(self):

        try:
            if self._server:
                self._server.close()
        except Exception:
            pass

        with self._lock:
            clientes = list(self._clientes)

        for connection in clientes:
            self._remove(connection)

        try:
            if os.path.exists(self.path):
                os.unlink(self.path)
        except OSError:
            pass


bus = EventBus()
emit = bus.emit