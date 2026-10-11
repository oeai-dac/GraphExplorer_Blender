"""Die Protokollschicht über dem WebSocket -- wirtsunabhängig.

Spricht dasselbe Protokoll wie ``wsserver.py`` im QGIS-Plugin und damit
dieselbe Gegenstelle im Browser (``src/explorer-bridge.js`` im GraphExplorer).
Nur der Unterbau ist ausgetauscht: ``websocket.py`` statt ``QtWebSockets``,
Rückrufe statt Qt-Signalen.

Das Addon bringt keinen eigenen Explorer mit. Es lauscht hier auf einem
lokalen Port; der GraphExplorer -- gleich, ob aus ``START-Graph-Explorer.bat``,
als Standalone-Datei oder von einem Webserver -- baut die Verbindung auf,
sobald jemand die Adresse unten links unter "Connect application" einfügt.

    QGIS                       Blender
    ────                       ───────
    QWebSocketServer     ──▶   websocket.WebSocketServer
    pyqtSignal           ──▶   on_ready / on_selection / on_closed / on_log
    Qt-Ereignisschleife  ──▶   poll(), aufgerufen aus bpy.app.timers

Der Unterschied, der Aufmerksamkeit verlangt: In Qt kommen Signale von selbst
an. Hier muss der Wirt ``poll()`` regelmäßig aufrufen. Das ist keine Umständ-
lichkeit, sondern genau das, was Blender braucht -- ``bpy`` ist nicht
fadensicher, und ``poll()`` läuft dort, wo der Wirt seine Daten anfassen darf.
"""

import json
import secrets

from .websocket import WebSocketServer

#: Vertragsversion, die der GraphExplorer (``src/main.jsx``, ``API_VERSION``)
#: mindestens melden muss.
#:
#: Geprüft wird auf *mindestens*, nicht auf *genau*. Beide Wirte hängen an
#: derselben Seite, und die Brücke bekommt Methoden hinzu, ohne welche zu
#: verlieren: Bei einer Gleichheitsprüfung würde jede Erweiterung für den einen
#: Wirt den anderen lahmlegen, obwohl ihm nichts fehlt.
#:
#: 2: describeValues/collectValues -- vom QGIS-Plugin für die Werteübernahme
#:    eingeführt und hier fürs Einfärben nach einem Wert mitbenutzt. Dass beide
#:    Wirte dieselbe Auskunft brauchen, war nicht geplant, ist aber
#:    naheliegend: Die Frage "was hängt an diesem Knoten noch?" stellt sich in
#:    einer Attributtabelle genauso wie an einem Mesh.
#: 3: Anzeigenamen in describeGraph (``labels``). Nicht verlangt -- ohne sie
#:    kürzt ``linking.DisplayNames`` die Schlüssel selbst.
EXPECTED_API_VERSION = 2

#: In wie großen Stücken ein Graph übertragen wird.
CHUNK_CHARS = 2 * 1024 * 1024


def _noop(*_args):
    pass


class BridgeServer(object):
    """Nimmt eine Browser-Sitzung an und spricht das Protokoll mit ihr.

    Der Wirt setzt die ``on_*``-Rückrufe und ruft ``poll()`` regelmäßig auf.
    """

    #: Beschriftung in der Verbindungsleiste der Seite. Seit dort mehrere
    #: Anwendungen gleichzeitig hängen können, ist ein Name aussagekräftiger
    #: als eine Portnummer.
    HOST_NAME = "Blender"

    def __init__(self):
        self.token = secrets.token_urlsafe(16)
        self.on_ready = _noop        # (api_version)
        self.on_closed = _noop       # ()
        self.on_selection = _noop    # (node_id, keys)
        self.on_graph_changed = _noop  # () -- anderer Graph im Explorer geladen
        self.on_log = _noop          # (message)

        self._ws = WebSocketServer()
        self._authenticated = False
        self._next_id = 1
        self._pending = {}
        self._api_version = None

    # -- Auf- und Abbau --------------------------------------------------------

    def start(self, port=0):
        return self._ws.start(port=port)

    def stop(self):
        self._ws.stop()
        self._fail_pending()

    def is_connected(self):
        return self._ws.is_connected() and self._authenticated

    # -- Vom Wirt regelmäßig aufzurufen ---------------------------------------

    def poll(self):
        """Eingetroffene Ereignisse verarbeiten und an die Rückrufe verteilen."""
        for kind, payload in self._ws.poll():
            if kind == "open":
                self._authenticated = False
            elif kind == "close":
                self._authenticated = False
                self._api_version = None
                self._fail_pending()
                self.on_closed()
            elif kind == "message":
                self._handle(payload)

    # -- Eingehend -------------------------------------------------------------

    def _handle(self, text):
        try:
            msg = json.loads(text)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return

        if not self._authenticated:
            # Bis die Marke stimmt, wird nichts anderes angesehen. Der Server
            # lauscht zwar nur auf 127.0.0.1, aber "lokal" heißt nicht "nur
            # wir": Jede andere Anwendung auf dem Rechner könnte sich sonst
            # verbinden und den geladenen Graphen auslesen.
            if msg.get("event") == "hello" and secrets.compare_digest(
                str(msg.get("token") or ""), self.token
            ):
                self._authenticated = True
                self._api_version = int(msg.get("apiVersion") or 0)
                self._ws.send(json.dumps({"event": "host", "name": self.HOST_NAME}))
                self.on_ready(self._api_version)
            else:
                # Nur diesen Anrufer abweisen, nicht den Anschluss schließen --
                # sonst wäre die kopierte Adresse danach unbrauchbar.
                self.on_log("Connection without a valid token refused.")
                self._ws.drop_client()
            return

        if "id" in msg:
            callback = self._pending.pop(msg["id"], None)
            if callback:
                callback(msg.get("result"))
            return

        event = msg.get("event")
        if event == "selection":
            keys = [str(k) for k in (msg.get("keys") or [])]
            self.on_selection(str(msg.get("nodeId") or ""), keys)
        elif event == "graph":
            self.on_graph_changed()
        elif event == "log":
            self.on_log(str(msg.get("message") or ""))

    def _fail_pending(self):
        """Offene Rückrufe einmal mit ``None`` bedienen.

        Sonst warten sie auf eine Antwort, die nach einem Verbindungsabbruch
        nicht mehr kommt -- und der Wirt hätte zwei Fälle zu behandeln statt
        einem.
        """
        pending, self._pending = self._pending, {}
        for callback in pending.values():
            if callback:
                callback(None)

    # -- Ausgehend -------------------------------------------------------------

    def call(self, method, args=None, callback=None):
        message = {"method": method, "args": list(args or [])}
        if callback is not None:
            message["id"] = self._next_id
            self._pending[self._next_id] = callback
            self._next_id += 1

        sent = self._authenticated and self._ws.send(json.dumps(message, ensure_ascii=False))
        if not sent and callback is not None:
            self._pending.pop(message.get("id"), None)
            callback(None)

    # -- Was der Wirt braucht --------------------------------------------------

    def set_link_specs(self, specs):
        self.call("setSpecs", [specs])

    def describe_graph(self, callback):
        self.call("describeGraph", [],
                  lambda r: callback(r or {"loaded": False, "types": []}))

    def collect_keys(self, spec, callback):
        self.call("collectKeys", [spec], lambda r: callback(r or []))

    def describe_values(self, spec, callback):
        """Welche Werte die Knoten dieser Verknüpfung beisteuern könnten."""
        self.call("describeValues", [spec],
                  lambda r: callback(r or {"total": 0, "values": []}))

    def collect_values(self, spec, sources, callback):
        """Die Werte selbst -- ``[{"id", "key", "values": [...]}]``."""
        self.call("collectValues", [spec, list(sources)], lambda r: callback(r or []))

    def select_nodes(self, node_ids):
        self.call("selectNodes", [list(node_ids)])

    def load_graph_text(self, text, callback=None):
        """Graph-JSON stückweise übertragen; der letzte Teil löst das Parsen aus."""
        self.call("abortGraphLoad")
        pieces = split_chunks(text, CHUNK_CHARS)
        for i, piece in enumerate(pieces):
            last = i == len(pieces) - 1
            self.call("loadGraphChunk", [piece, last], callback if last else None)

    def api_version_mismatch(self):
        if self._api_version is None or self._api_version >= EXPECTED_API_VERSION:
            return None
        return (
            "The GraphExplorer is older than this add-on (it reports interface "
            "version %s, at least %s is needed). Please update the GraphExplorer."
            % (self._api_version, EXPECTED_API_VERSION)
        )


def split_chunks(text, size):
    if len(text) <= size:
        return [text]
    return [text[i:i + size] for i in range(0, len(text), size)]
