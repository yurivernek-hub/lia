"""
Cliente do OpenCode para a colaboração da Lia.

O OpenCode roda como servidor local (`opencode serve`) e a Lia o controla
por HTTP. Este módulo tem duas peças, só com a biblioteca padrão:

  ServidorOpenCode  sobe e para o process do servidor de UM projeto
                    (só quando a Lia entra no modo colaboração: fora disso
                    não gasta RAM).
  ClienteOpenCode   speech com a API: sessões, prompts, abortar, diff,
                    permissões, reverter, e lê os events em tempo real
                    (SSE) numa thread, entregues numa fila.

Segurança: o servidor do OpenCode consegue rodar comandos no computador.
Por isso ele só escuta em 127.0.0.1, numa porta livre sorteada, e com SENHA
aleatória por execução (OPENCODE_SERVER_PASSWORD, autenticação básica).
Sem a senha, nenhum outro programa nem página da web consegue usá-lo.

Os nomes dos events do OpenCode variam entre versões. Por isso os events
passam adiante como dicionários crus, e os ajudantes de leitura
(sessao_do_event, event_ocioso, event_permissao) são tolerantes.
"""

import atexit
import base64
import http.client
import json
import os
import queue
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
from urllib.parse import quote, urlencode

HOST = "127.0.0.1"


class OpenCodeError(Exception):
    """Qualquer falha ao subir ou falar com o OpenCode."""


# ----------------------------------------------------------------------
# utilidades
# ----------------------------------------------------------------------

def _free_port():

    with socket.socket() as s:
        s.bind((HOST,0))
        return s.getsockname()[1]


def _find(obj,key):
    """Primeiro valor não vazio de `chave`, em qualquer nível."""

    if isinstance(obj,dict):

        if obj.get(key) not in (None,""):
            return obj[key]

        for value in obj.values():

            achado = _find(value,key)

            if achado is not None:
                return achado

    elif isinstance(obj,list):

        for value in obj:

            achado = _find(value,key)

            if achado is not None:
                return achado

    return None


def event_type(ev):

    return str(ev.get("type") or "") if isinstance(ev,dict) else ""


def session_of_event(ev):
    """Id da sessão a que o event se refere (None se não disser)."""

    return _find(ev,"sessionID")


def event_idle(ev):
    """A sessão terminou o que estava fazendo?"""

    type = event_type(ev)

    if type == "session.idle":
        return True

    if type == "session.status":

        status = _find(ev,"status")

        if isinstance(status,dict):
            return status.get("type") == "idle"

        return status == "idle"

    return False


def event_permission(ev):
    """
    Se o event é um pedido de permissão (editar arquivo, rodar comando...),
    devolve {id, sessao, titulo, event}; senão None.
    """

    type = event_type(ev)

    if not type.startswith("permission") or type.endswith("replied"):
        return None

    props = ev.get("properties") if isinstance(ev.get("properties"),dict) else ev

    pid = props.get("id") or _find(ev,"permissionID") or _find(ev,"id")

    if not pid:
        return None

    return {
        "id": pid,
        "session": session_of_event(ev),
        "title": (
            props.get("title") or props.get("permission")
            or _find(ev,"title") or ""
        ),
        "event": ev,
    }


# ----------------------------------------------------------------------
# server
# ----------------------------------------------------------------------

class OpenCodeServer:
    """Processo `opencode serve` de um projeto."""

    def __init__(self,folder,executable="opencode",port=None,file_log=None):

        self.folder = os.path.abspath(os.path.expanduser(folder))
        self.executable = executable
        self.port = port or _free_port()
        self.password = secrets.token_urlsafe(24)
        self.file_log = file_log or os.path.join(
            os.path.expanduser("~"),".cache","lia","opencode_servidor.log"
        )

        self.process = None
        self._log = None

        atexit.register(self.stop)

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    def client(self):
        return OpenCodeClient(self.port,self.password)

    def start(self,wait=45):
        """Sobe o servidor e espera ele responder. Levanta ErroOpenCode."""

        if self.running:
            return

        if not os.path.isdir(self.folder):
            raise OpenCodeError(f"A pasta do projeto não existe: {self.folder}")

        exe = shutil.which(self.executable)

        if not exe:
            raise OpenCodeError(
                f"Não encontrei o programa '{self.executable}'. "
                "Instale com: npm i -g opencode-ai"
            )

        os.makedirs(os.path.dirname(self.file_log),exist_ok=True)

        self._log = open(self.file_log,"ab")

        environment = dict(os.environ)
        environment["OPENCODE_SERVER_PASSWORD"] = self.password
        environment["OPENCODE_SERVER_USERNAME"] = "opencode"

        self.process = subprocess.Popen(
            [exe,"serve","--port",str(self.port),"--hostname",HOST],
            cwd=self.folder,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            start_new_session=True
        )

        client = self.client()
        limit = time.time() + wait

        while time.time() < limit:

            if self.process.poll() is not None:

                tail = self._log_tail()
                self.stop()

                raise OpenCodeError(
                    "O servidor do OpenCode fechou ao iniciar.\n" + tail
                )

            try:

                client.health()

                return

            except OpenCodeError:
                time.sleep(0.3)

        self.stop()

        raise OpenCodeError(
            f"O servidor do OpenCode não respondeu em {wait} s "
            f"(log: {self.file_log})."
        )

    def stop(self):
        """Encerra o servidor e tudo o que ele iniciou."""

        process = self.process
        self.process = None

        if process is not None and process.poll() is None:

            try:
                os.killpg(process.pid,signal.SIGTERM)
            except OSError:
                process.terminate()

            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:

                try:
                    os.killpg(process.pid,signal.SIGKILL)
                except OSError:
                    process.kill()

        if self._log:

            try:
                self._log.close()
            except OSError:
                pass

            self._log = None

    def _log_tail(self,lines=15):

        try:

            with open(self.file_log,"rb") as f:
                text = f.read()[-3000:].decode("utf-8","replace")

            return "\n".join(text.splitlines()[-lines:])

        except OSError:
            return ""


# ----------------------------------------------------------------------
# client
# ----------------------------------------------------------------------

class OpenCodeClient:

    def __init__(self,port,password,host=HOST,user="opencode"):

        self.host = host
        self.port = port

        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._auth = "Basic " + token

        # Real-time events (raw dictionaries). Lia’s main thread
        # consumes this queue at its own pace.
        self.events = queue.Queue()
        self.ao_event = None          # optional callback (reader thread)

        self._thread = None
        self._stop = threading.Event()
        self._connection_sse = None
        self._soquete_sse = None

    # --- HTTP ---------------------------------------------------------

    def _request(self,method,path,body=None,query=None,timeout=30):

        if query:
            path += "?" + urlencode(query)

        connection = http.client.HTTPConnection(
            self.host,self.port,timeout=timeout
        )

        try:

            headers = {
                "Authorization": self._auth,
                "Accept": "application/json",
            }

            data = None

            if body is not None:
                data = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"

            connection.request(method,path,body=data,headers=headers)

            response = connection.getresponse()
            raw = response.read()

        except (OSError,http.client.HTTPException) as e:

            raise OpenCodeError(
                f"Não consegui falar com o OpenCode: {e}"
            ) from e

        finally:
            connection.close()

        if response.status >= 400:

            raise OpenCodeError(
                f"O OpenCode respondeu {response.status} em "
                f"{method} {path}: "
                f"{raw[:300].decode('utf-8','replace')}"
            )

        if not raw.strip():
            return None

        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode("utf-8","replace")

    @staticmethod
    def _id(value):
        return quote(str(value),safe="")

    # --- API ------------------------------------------------------------

    def health(self):
        return self._request("GET","/global/health",timeout=3)

    def providers(self):
        return self._request("GET","/config/providers")

    def create_session(self,title=None):
        """Devolve o id da sessão nova."""

        session = self._request(
            "POST","/session",body={"title": title} if title else {}
        )

        sid = session.get("id") if isinstance(session,dict) else None

        if not sid:
            raise OpenCodeError(f"Resposta inesperada ao criar sessão: {session}")

        return sid

    def sessions(self):
        return self._request("GET","/session")

    def status(self):
        return self._request("GET","/session/status")

    def send(self,session,text,model=None,agent=None):
        """
        Manda um prompt SEM esperar a resposta (o andamento vem pelos
        events). `modelo` = "provedor/modelo", por exemplo
        "groq/openai/gpt-oss-120b"; vazio = o padrão do OpenCode.
        """

        body = {"parts": [{"type": "text","text": text}]}

        if model:

            provedor,_,id_model = model.partition("/")
            body["model"] = {"providerID": provedor,"modelID": id_model}

        if agent:
            body["agent"] = agent

        self._request(
            "POST",f"/session/{self._id(session)}/prompt_async",body=body
        )

    def abort(self,session):
        return self._request("POST",f"/session/{self._id(session)}/abort")

    def messages(self,session,limit=30):
        return self._request(
            "GET",f"/session/{self._id(session)}/message",
            query={"limit": limit}
        )

    def last_response(self,session):
        """Texto da última resposta do assistente ('' se não houver)."""

        for item in reversed(self.messages(session) or []):

            info = item.get("info") or {}

            if info.get("role") != "assistant":
                continue

            text = "".join(
                p.get("text","") for p in item.get("parts") or []
                if p.get("type") == "text"
            ).strip()

            if text:
                return text

        return ""

    def diff(self,session,message_id=None):
        """Mudanças feitas na sessão (lista de arquivos com o diff)."""

        return self._request(
            "GET",f"/session/{self._id(session)}/diff",
            query={"messageID": message_id} if message_id else None
        )

    def revert(self,session,message_id,part_id=None):
        """Desfaz a partir de uma mensagem (reversão do próprio OpenCode)."""

        body = {"messageID": message_id}

        if part_id:
            body["partID"] = part_id

        return self._request(
            "POST",f"/session/{self._id(session)}/revert",body=body
        )

    def undo_reversao(self,session):
        return self._request("POST",f"/session/{self._id(session)}/unrevert")

    def respond_permission(self,session,permission_id,response="once",remember=False):
        """resposta: 'once' (esta vez), 'always' (sempre) ou 'reject' (negar)."""

        body = {"response": response}

        if remember:
            body["remember"] = True

        return self._request(
            "POST",
            f"/session/{self._id(session)}/permissions/{self._id(permission_id)}",
            body=body
        )

    # --- real-time events (SSE) ----------------------------------------

    def start_events(self):
        """Começa a ler GET /event numa thread; cada event vai pra `events`."""

        if self._thread and self._thread.is_alive():
            return

        self._stop.clear()

        self._thread = threading.Thread(
            target=self._read_events,
            daemon=True
        )

        self._thread.start()

    def stop_events(self):

        self._stop.set()

        # Close the underlying socket to wake the blocked readline().
        # (We keep the socket when connecting: HTTPConnection may release it
        # when the response has no defined length.)
        soquete = self._soquete_sse

        if soquete is not None:

            try:
                soquete.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        if self._thread:
            self._thread.join(timeout=3)

    def _deliver(self,ev):

        if not isinstance(ev,dict):
            ev = {"type": "desconhecido","data": ev}

        ev.setdefault("_recebido",time.time())

        self.events.put(ev)

        if self.ao_event:

            try:
                self.ao_event(ev)
            except Exception as e:
                print(f"[!] Erro no callback de event do OpenCode: {e}")

    def _read_events(self):

        tentativas = 0

        while not self._stop.is_set():

            try:

                connection = http.client.HTTPConnection(
                    self.host,self.port,timeout=None
                )

                self._connection_sse = connection

                connection.request("GET","/event",headers={
                    "Authorization": self._auth,
                    "Accept": "text/event-stream",
                })

                self._soquete_sse = connection.sock

                response = connection.getresponse()

                if response.status >= 400:
                    raise OSError(f"HTTP {response.status}")

                # stop_events() may have been called while connecting
                if self._stop.is_set():
                    break

                if tentativas:
                    self._deliver({"type": "cliente.reconectado"})

                tentativas = 0
                lines_dado = []

                while not self._stop.is_set():

                    line = response.readline()

                    if not line:
                        break                      # server fechou

                    line = line.decode("utf-8","replace").rstrip("\r\n")

                    if line.startswith("data:"):
                        lines_dado.append(line[5:].lstrip())

                    elif line == "" and lines_dado:

                        raw = "\n".join(lines_dado)
                        lines_dado = []

                        try:
                            self._deliver(json.loads(raw))
                        except ValueError:
                            self._deliver({"type": "desconhecido","text": raw})

            except (OSError,http.client.HTTPException):
                pass

            finally:

                try:
                    self._connection_sse.close()
                except Exception:
                    pass

            if self._stop.is_set():
                break

            tentativas += 1

            self._deliver({"type": "cliente.desconectado","tentativa": tentativas})

            if tentativas > 5:
                return

            self._stop.wait(min(tentativas,5))
