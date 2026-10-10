"""Blender-Oberfläche: Einstellungen, Bedienknöpfe, Seitenleiste, Zeitgeber.

Der Explorer selbst ist der GraphExplorer im Browser -- derselbe, mit dem auch
sonst gearbeitet wird. Das Addon bringt keinen eigenen mit, sondern hält nur
die Leitung: Die Adresse wird hier kopiert und im GraphExplorer unten links
unter "Connect QGIS / Blender" eingefügt. Damit gibt es genau einen Explorer,
und ein Update des GraphExplorers wirkt sofort auch für Blender.

Der Zeitgeber ist das Herzstück. Er tut drei Dinge, alle auf dem Hauptfaden --
dort, wo ``bpy`` angefasst werden darf:

    bpy.app.timers  ──▶  bridge.poll()          Nachrichten aus dem Browser
                    ──▶  poll_for_graph()       ist im Browser ein Graph geladen?
                    ──▶  sync.poll_selection()  Auswahl in Blender vergleichen

Der WebSocket selbst läuft in einem Hintergrundfaden (``websocket.py``) und
fasst nie etwas von Blender an. Genau diese Trennung verhindert die Abstürze,
die man sonst nicht reproduzieren kann.

**Mehrere Verknüpfungen.** Wie in QGIS lassen sich mehrere anlegen -- eine für
Meshes mit SE-Bezeichnung, eine für Meshes mit Fundbezeichnung. Sie werden
gemeinsam abgeglichen und wirken nebeneinander.
"""

import json
import os
import time

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup, UIList

from . import coloring, shading
from .bridge import BridgeServer
from .linking import (
    DEFAULT_PATTERN,
    NORMALIZE_LOOSE,
    NORMALIZE_MODES,
    NORMALIZE_PATTERN,
    SOURCE_ATTR,
    SOURCE_ID,
    SOURCE_LABEL,
    DisplayNames,
    LinkConfig,
)
from .objectsync import NAME_FROM_MESH, NAME_FROM_OBJECT, ObjectSync, link_label, visible_objects

#: Wo es den GraphExplorer gibt -- für alle, die das Addon vor dem Explorer
#: gefunden haben.
GRAPHEXPLORER_URL = "https://github.com/oeai-dac/GraphExplorer"

#: Die Kurzanleitung, solange nichts verbunden ist. Wie im QGIS-Plugin.
HOW_TO_CONNECT = (
    "1. Open the GraphExplorer in your browser.",
    "2. Click Copy connection address below.",
    "3. In the GraphExplorer, click Connect QGIS / Blender",
    "    at the bottom left, paste the address, click Connect.",
)

TICK_SECONDS = 0.2

#: Platzhalter in Auswahllisten, solange nichts zu wählen ist. Kein leerer
#: Bezeichner: Den nimmt Blender je nach Version nicht an.
NONE_ID = "__none__"

# Die laufende Sitzung. Ein Modul-Global, weil Blender-Operatoren keine
# Instanzen sind und PropertyGroups nur Daten halten dürfen, keine Sockets.
_session = None


# ---------------------------------------------------------------------------
# Die Sitzung
# ---------------------------------------------------------------------------

class Session(object):
    """Server, Brücke, Indizes -- alles, was zwischen Verbinden und Beenden lebt.

    Entsteht beim ersten "Copy connection address" und nicht schon beim
    Aktivieren des Addons: Ein offener Port soll eine Folge eines Klicks sein,
    nicht des Startens von Blender. Danach bleibt die Adresse gültig, bis
    Blender beendet oder das Addon abgeschaltet wird -- wie in QGIS.
    """

    def __init__(self):
        self.bridge = BridgeServer()
        self.ws_port = self.bridge.start()
        self.sync = ObjectSync(self.bridge)

        self.connected = False
        self.status = ""
        self.graph_types = []          # aus describeGraph, füttert die Auswahllisten
        self.names = DisplayNames()    # die Namen, unter denen der Explorer sie zeigt
        self.value_sources = []        # aus describeValues, füttert die Einfärbeliste
        self.value_spec = None         # zu welcher Verknüpfung die Liste gehört
        self._last_describe = 0.0

        self.bridge.on_ready = self._on_ready
        self.bridge.on_closed = self._on_closed
        self.bridge.on_selection = self.sync.on_graph_selection
        self.bridge.on_graph_changed = self._on_graph_changed
        self.bridge.on_log = self._log

    @property
    def ws_url(self):
        """Der Anschluss für den GraphExplorer.

        Enthält die Marke, weil der Server ohne sie abweist -- die Adresse ist
        damit selbst das Geheimnis und gehört nicht weitergereicht.
        """
        return "ws://127.0.0.1:%d/?token=%s" % (self.ws_port, self.bridge.token)

    def stop(self):
        self.bridge.stop()

    # -- Ereignisse ------------------------------------------------------------

    def _on_ready(self, _version):
        self.connected = True
        mismatch = self.bridge.api_version_mismatch()
        if mismatch:
            self._set_status(mismatch)
            print("[GraphExplorer] " + mismatch)
        else:
            self._set_status("Connected. Load a graph, then set up the links.")
        self.refresh_types()

    def _on_closed(self):
        # Die Adresse bleibt gültig: Der Server lauscht weiter auf demselben
        # Port mit derselben Marke.
        self.connected = False
        self._set_status("Connection to the GraphExplorer lost. To reconnect, paste "
                         "the same address there again.")

    def _on_graph_changed(self):
        """Im Explorer wurde ein anderer Graph geladen -- auch per Hineinziehen.

        Typen *und* Indizes müssen nachziehen: Ein neuer Graph bringt neue
        Knoten-IDs und neue Bezeichnungen mit. Bleiben die Indizes stehen,
        zeigt die Kopplung auf Knoten, die es nicht mehr gibt.
        """
        self.graph_types = []
        self.value_sources = []
        self.value_spec = None
        self.refresh_types()
        if self.sync.runtimes:
            self._set_status("Another graph in the GraphExplorer — updating the matches …")
            self._relink()

    def _on_described(self, info):
        info = info or {}
        self.graph_types = info.get("types", []) or []
        self.names = DisplayNames(info)
        self.sync.names = self.names
        if not self.graph_types:
            return
        self._set_status("Graph loaded: %d nodes, %d node types."
                         % (info.get("nodeCount") or 0, len(self.graph_types)))
        # Eine gespeicherte Szene bringt ihre Verknüpfungen mit. Sind sie
        # vollständig, gleich abgleichen -- wie QGIS beim Öffnen eines
        # Projekts. Sonst verbindet man sich, klickt ein Mesh an, und nichts
        # passiert, bis jemand an den Knopf denkt.
        settings = _settings()
        if not self.sync.runtimes and settings is not None and _configs_from(settings):
            self._relink()

    def _log(self, text):
        print("[GraphExplorer] " + text)
        self._set_status(text)

    def _set_status(self, text):
        self.status = text
        _redraw()

    def refresh_types(self):
        self._last_describe = time.time()
        self.bridge.describe_graph(self._on_described)

    def poll_for_graph(self):
        """Nachsehen, ob im Browser inzwischen ein Graph geladen wurde.

        Der Graphwechsel wird zwar gemeldet (``_on_graph_changed``); dieses
        Nachfragen fängt den Fall ab, dass beim Verbinden längst einer offen
        war -- dann gab es keine Änderung, die zu melden gewesen wäre.
        """
        if not self.connected or self.graph_types:
            return
        now = time.time()
        if now - self._last_describe < 1.5:
            return
        self.refresh_types()

    def _relink(self):
        """Alle Verknüpfungen der Szene neu abgleichen."""
        settings = _settings()
        if settings is None:
            return
        self.sync.zoom_to_selection = settings.zoom_to_selection

        def done(_reports):
            self._set_status(self.sync.summary())
            # Die Werteliste hängt am Knotentyp der aktiven Verknüpfung. Sie
            # gleich mitzuholen erspart einen zweiten Knopfdruck an der Stelle,
            # an der man ohnehin weiterarbeiten will.
            self.refresh_values()

        self.sync.rebuild(
            _configs_from(settings), settings.name_from, settings.strip_suffix,
            on_done=done,
        )

    # -- Werte zum Einfärben ---------------------------------------------------

    def active_spec(self):
        """Die Spezifikation der gerade gewählten Verknüpfung -- oder ``None``."""
        settings = _settings()
        if settings is None or not (0 <= settings.active_link < len(settings.links)):
            return None
        config = _config_from(settings.links[settings.active_link])
        return config.js_spec() if config is not None else None

    def refresh_values(self):
        """Nachfragen, was die Knoten dieser Verknüpfung an Werten hergeben."""
        spec = self.active_spec()
        if spec is None:
            self.value_sources = []
            self.value_spec = None
            _redraw()
            return

        def done(info):
            self.value_sources = (info or {}).get("values") or []
            self.value_spec = spec
            _redraw()

        self.bridge.describe_values(spec, done)


def _settings():
    scene = getattr(bpy.context, "scene", None)
    return getattr(scene, "graphexplorer", None) if scene else None


def _names():
    return _session.names if _session is not None else DisplayNames()


def _redraw():
    """Die Seitenleiste neu zeichnen lassen.

    Blender merkt von sich aus nichts davon, dass sich ein Modul-Global geändert
    hat -- ohne das hier bliebe der Status stehen, bis jemand mit der Maus über
    das Panel fährt.
    """
    manager = getattr(bpy.context, "window_manager", None)
    if manager is None:
        return
    for window in manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _start_session():
    global _session
    if _session is None:
        _session = Session()
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=TICK_SECONDS, persistent=True)
    return _session


# ---------------------------------------------------------------------------
# Zeitgeber
# ---------------------------------------------------------------------------

def _tick():
    if _session is None:
        return None          # None beendet den Zeitgeber
    try:
        _session.bridge.poll()
        _session.poll_for_graph()
        _session.sync.poll_selection()
    except Exception as exc:  # noqa: BLE001 -- ein Fehler hier darf Blender nicht lahmlegen
        print("[GraphExplorer] Error in timer: %r" % (exc,))
    return TICK_SECONDS


# ---------------------------------------------------------------------------
# Auswahllisten aus dem Graphen
# ---------------------------------------------------------------------------
#
# Knotentyp, Property und Wertquelle kommen aus dem geladenen Graphen. Blender
# kennt dafür dynamische Auswahllisten -- mit zwei Haken, die hier beide
# abgefangen werden:
#
# 1. **Blender speichert die Position, nicht die Kennung.** Ändert sich die
#    Liste (anderer Graph, oder beim Öffnen der .blend noch gar keiner), zeigte
#    die Auswahl stillschweigend auf etwas anderes -- oder auf nichts, und die
#    Verknüpfungen wären nach dem Neuladen leer. Deshalb steht die Wahl als
#    Text in einer eigenen StringProperty (``node_type``, ``node_attr``,
#    ``color_source_key``); die Auswahlliste ist nur das Bedienelement davor
#    und liest und schreibt diesen Text über ``get``/``set``. Fehlt ein
#    gespeicherter Wert im aktuellen Graphen, steht er trotzdem in der Liste,
#    gekennzeichnet -- so sieht man, was eingestellt ist.
#
# 2. **Python muss die Zeichenketten festhalten.** Gibt der Rückruf frisch
#    gebaute Listen zurück, räumt Python sie ab, während Blender noch auf sie
#    zeigt -- ein bekannter Weg zu wirrer Anzeige und Abstürzen. Deshalb der
#    Zwischenspeicher, und deshalb bauen ``get``/``set`` sich eine eigene Liste,
#    statt die gerade angezeigte zu ersetzen.

_items_cache = {}


def _keep(slot, items):
    _items_cache[slot] = items
    return items


def _with_numbers(entries, stored, missing_label, empty_label):
    """``[(id, name, description)]`` -> Enum-Einträge mit fester Nummer."""
    items = [e for e in entries]
    if stored and stored not in [e[0] for e in items]:
        items.append((stored, missing_label, stored))
    if not items:
        items.append((NONE_ID, empty_label, ""))
    return [(ident, name, desc, i) for i, (ident, name, desc) in enumerate(items)]


def _lookup(items, stored):
    for ident, _name, _desc, number in items:
        if ident == stored:
            return number
    return 0


def _ident_for(items, value):
    for ident, _name, _desc, number in items:
        if number == value:
            return "" if ident == NONE_ID else ident
    return ""


def _build_type_items(link):
    names = _names()
    entries = []
    if _session is not None:
        for entry in _session.graph_types:
            key = entry.get("type") or ""
            entries.append((key, "%s (%d)" % (names.type(key), entry.get("count") or 0), key))
    stored = link.node_type
    return _with_numbers(entries, stored,
                         "%s (not in the loaded graph)" % names.type(stored),
                         "(no graph loaded)")


def _type_items(self, _context):
    return _keep("type", _build_type_items(self))


def _get_type(self):
    return _lookup(_build_type_items(self), self.node_type)


def _set_type(self, value):
    self.node_type = _ident_for(_build_type_items(self), value)


def _build_attr_items(link):
    names = _names()
    entries = []
    if _session is not None:
        for entry in _session.graph_types:
            if entry.get("type") == link.node_type:
                entries = [(a, names.attr(a), a) for a in entry.get("attrs") or []]
                break
    stored = link.node_attr
    return _with_numbers(entries, stored,
                         "%s (not in the loaded graph)" % names.attr(stored),
                         "(no properties)")


def _attr_items(self, _context):
    return _keep("attr", _build_attr_items(self))


def _get_attr(self):
    return _lookup(_build_attr_items(self), self.node_attr)


def _set_attr(self, value):
    self.node_attr = _ident_for(_build_attr_items(self), value)


def source_key(source):
    """Die Kennung einer Wertquelle -- zugleich das, was die Brücke braucht."""
    return json.dumps(source or {}, sort_keys=True)


def _build_value_items(settings):
    """Wertquellen für die Einfärbung, aus ``describeValues``.

    Die Kennung ist die JSON-Form der Quelle (``{"attr":"…","kind":"attr"}``).
    Damit ist sie zugleich das, was die Brücke braucht: Der Einfärbe-Operator
    kann sie unmittelbar weiterreichen, ohne eine zweite Tabelle zu führen, die
    dann nicht mehr zu ihr passt.
    """
    names = _names()
    entries = []
    if _session is not None:
        for entry in _session.value_sources:
            source = entry.get("source") or {}
            text = coloring.source_text(source, names)
            count, of = entry.get("count") or 0, entry.get("of") or 0
            entries.append((
                source_key(source),
                "%s (%d/%d)" % (text, count, of) if of else text,
                "Example: %s" % (entry.get("sample") or "—"),
            ))
    stored = settings.color_source_key
    missing = ""
    if stored:
        try:
            missing = coloring.source_text(json.loads(stored), names)
        except ValueError:
            missing = "?"
    return _with_numbers(entries, stored, missing, "(no values loaded)")


def _value_items(self, _context):
    return _keep("value", _build_value_items(self))


def _get_value(self):
    return _lookup(_build_value_items(self), self.color_source_key)


def _set_value(self, value):
    self.color_source_key = _ident_for(_build_value_items(self), value)


# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------

#: Steht, während die Legende aufgebaut wird. Ohne den Merker liefe der
#: update-Rückruf unten je Gruppe einmal über alle Objekte der Datei -- direkt
#: nachdem apply_plan sie ohnehin gerade gesetzt hat.
_filling_legend = False


def _on_group_color(self, _context):
    """Farbe in der Legende geändert -- Material und Objektfarben nachziehen.

    Direkt beim Schieben und nicht erst auf Knopfdruck: Eine Farbe wählt man im
    Vergleich mit den anderen, und dazu muss man sie sehen.
    """
    if _filling_legend:
        return
    try:
        shading.recolor(self.material, tuple(self.color))
    except Exception as exc:  # noqa: BLE001 -- eine Farbänderung darf nichts abwürgen
        print("[GraphExplorer] Colour not applied: %r" % (exc,))


def _on_zoom(self, _context):
    if _session is not None:
        _session.sync.zoom_to_selection = self.zoom_to_selection


def _split(text):
    """``"Tolerant — ignore case"`` -> ``("Tolerant", "ignore case")``."""
    name, _sep, desc = text.partition(" — ")
    return name, desc[:1].upper() + desc[1:]


NORMALIZE_ITEMS = [(key,) + _split(text) for key, text in NORMALIZE_MODES.items()]


class GraphExplorerColorGroup(PropertyGroup):
    """Eine Zeile der Legende: dieser Wert, diese Farbe, so viele Objekte."""

    value: StringProperty(name="Value", default="")
    color: FloatVectorProperty(
        name="Color", subtype="COLOR", size=3, min=0.0, max=1.0,
        default=(0.8, 0.8, 0.8), update=_on_group_color,
    )
    count: IntProperty(name="Objects", default=0)
    #: Name des Materials -- der Faden zurück zur Szene, wenn die Farbe
    #: nachträglich geändert wird.
    material: StringProperty(default="")


class GraphExplorerLink(PropertyGroup):
    """Eine Verknüpfung: dieser Knotentyp, dieser Schlüssel.

    Die Objektseite ist bei allen Verknüpfungen dieselbe -- es sind dieselben
    Objekte in derselben Szene. Unterschieden wird über den Knotentyp: Ein
    SE-Mesh findet nur in der SE-Verknüpfung einen Knoten, ein Fund-Mesh nur in
    der Fund-Verknüpfung.
    """

    # Die gespeicherten Werte -- siehe "Auswahllisten aus dem Graphen".
    node_type: StringProperty(default="")
    node_attr: StringProperty(default="")

    node_type_ui: EnumProperty(
        name="Node type", items=_type_items, get=_get_type, set=_set_type,
        description="Which nodes in the graph belong to these objects",
    )
    node_source: EnumProperty(
        name="Key in the graph",
        items=[
            (SOURCE_LABEL, "Node label",
             "The name shown in the GraphExplorer — the usual case"),
            (SOURCE_ATTR, "Node property",
             "A property of the node that holds the name"),
            (SOURCE_ID, "Node ID",
             "The internal ID — fits if the object names were generated from it"),
        ],
        default=SOURCE_LABEL,
    )
    node_attr_ui: EnumProperty(
        name="Property", items=_attr_items, get=_get_attr, set=_set_attr,
        description="The property of the node that holds the same name as the objects",
    )
    normalize: EnumProperty(
        name="Comparison", items=NORMALIZE_ITEMS, default=NORMALIZE_LOOSE,
        description="How strictly object names and graph values are compared",
    )
    pattern: StringProperty(
        name="Pattern", default=DEFAULT_PATTERN,
        description="Regular expression; the part in brackets is compared",
    )
    enabled: BoolProperty(name="Active", default=True,
                          description="Switch the link off without deleting it")


class GraphExplorerSettings(PropertyGroup):
    #: Nur der Startordner für "Load graph …" -- geladen wird nie von selbst.
    graph_path: StringProperty(name="Graph JSON", subtype="FILE_PATH")
    links: CollectionProperty(type=GraphExplorerLink)
    active_link: IntProperty(name="Active link", default=0)

    # Diese beiden gelten für alle Verknüpfungen: Wie Objekte in dieser Szene
    # benannt sind, ist eine Eigenschaft der Szene, nicht der einzelnen
    # Zuordnung.
    name_from: EnumProperty(
        name="Name from",
        items=[
            (NAME_FROM_OBJECT, "Object", "The name of the object"),
            (NAME_FROM_MESH, "Mesh data", "The name of the object's mesh data"),
        ],
        default=NAME_FROM_OBJECT,
        description="Which name of the objects is compared with the graph",
    )
    strip_suffix: BoolProperty(
        name="Ignore .001 suffixes", default=True,
        description='Remove the ".001" Blender adds to duplicate names, so that '
                    "several objects of the same unit share one key",
    )
    zoom_to_selection: BoolProperty(
        name="Zoom to selection", default=False, update=_on_zoom,
        description="When something is selected in the GraphExplorer, the 3D "
                    "view zooms to it",
    )

    # -- Einfärben --
    color_source_key: StringProperty(default="")
    color_source: EnumProperty(
        name="Value", items=_value_items, get=_get_value, set=_set_value,
        description="The value to color by — a property of the nodes of this "
                    "link, or something connected to them",
    )
    color_materials: BoolProperty(
        name="Use materials", default=True,
        description="Create one material per group and assign it to the object. "
                    "The materials of the mesh data are kept and come back with "
                    '"Remove coloring".\n'
                    "Off: only the object color, visible in Solid mode",
    )
    color_groups: CollectionProperty(type=GraphExplorerColorGroup)
    active_color_group: IntProperty(name="Legend", default=0)
    color_note: StringProperty(default="")


def _config_from(link):
    """Eine ``LinkConfig`` aus einem Listeneintrag -- oder ``None``, wenn er nicht trägt."""
    if not link.node_type:
        return None
    if link.node_source == SOURCE_ATTR and not link.node_attr.strip():
        return None
    return LinkConfig(
        node_type=link.node_type, node_source=link.node_source,
        node_attr=link.node_attr, normalize=link.normalize, pattern=link.pattern,
    )


def _configs_from(settings):
    """Die aktiven Verknüpfungen als ``LinkConfig``-Liste."""
    out = []
    for link in settings.links:
        if not link.enabled:
            continue
        config = _config_from(link)
        if config is not None:
            out.append(config)
    return out


# ---------------------------------------------------------------------------
# Listen
# ---------------------------------------------------------------------------

class GRAPHEXPLORER_UL_links(UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_prop, _index):
        text = link_label(item.node_type, item.node_source, item.node_attr, _names())
        row = layout.row(align=True)
        row.label(text=text, icon="LINKED" if item.enabled else "UNLINKED")
        row.prop(item, "enabled", text="")


class GRAPHEXPLORER_UL_color_groups(UIList):
    """Die Legende: Farbe, Wert, Anzahl.

    Die Farbe steht links und ist anfassbar -- eine Legende, die nur anzeigt,
    beantwortet die zweite Frage nicht, die immer kommt: "kann das Gelb bitte
    weniger schreien".
    """

    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_prop, _index):
        row = layout.row(align=True)
        row.prop(item, "color", text="")
        row.label(text=item.value or "(empty)")
        row.label(text=str(item.count))


class GRAPHEXPLORER_OT_link_add(Operator):
    bl_idname = "graphexplorer.link_add"
    bl_label = "Add link"
    bl_options = {"UNDO"}

    def execute(self, context):
        settings = context.scene.graphexplorer
        settings.links.add()
        settings.active_link = len(settings.links) - 1
        return {"FINISHED"}


class GRAPHEXPLORER_OT_link_remove(Operator):
    bl_idname = "graphexplorer.link_remove"
    bl_label = "Remove link"
    bl_options = {"UNDO"}

    def execute(self, context):
        settings = context.scene.graphexplorer
        if not settings.links:
            return {"CANCELLED"}
        settings.links.remove(settings.active_link)
        settings.active_link = max(0, min(settings.active_link, len(settings.links) - 1))
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Verbindung und Graph
# ---------------------------------------------------------------------------

class GRAPHEXPLORER_OT_copy_address(Operator):
    bl_idname = "graphexplorer.copy_address"
    bl_label = "Copy connection address"
    bl_description = ("Copy the address of this Blender session. Paste it in the "
                      'GraphExplorer under "Connect QGIS / Blender" (bottom left)')

    def execute(self, context):
        try:
            session = _start_session()
        except OSError as exc:
            self.report({"ERROR"}, "Could not open a local port: %s" % exc)
            return {"CANCELLED"}
        # Die Adresse wird nicht angezeigt, nur kopiert: Sie ist lang, trägt
        # die Zugangsmarke, und gebraucht wird sie ohnehin nur im Explorer.
        context.window_manager.clipboard = session.ws_url
        if session.connected:
            session._set_status("Address copied. The GraphExplorer is already connected.")
        else:
            session._set_status('Address copied. Now paste it in the GraphExplorer under '
                                '"Connect QGIS / Blender" (bottom left).')
        return {"FINISHED"}


class GRAPHEXPLORER_OT_load_graph(Operator):
    bl_idname = "graphexplorer.load_graph"
    bl_label = "Load graph …"
    bl_description = ("Open a graph JSON file in the connected GraphExplorer. "
                      "Optional — you can also open it in the GraphExplorer itself")

    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})

    def invoke(self, context, _event):
        if _session is None or not _session.connected:
            self.report({"ERROR"}, "Not connected to the GraphExplorer.")
            return {"CANCELLED"}
        settings = context.scene.graphexplorer
        if settings.graph_path:
            self.filepath = bpy.path.abspath(settings.graph_path)
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        if _session is None or not _session.connected:
            self.report({"ERROR"}, "Not connected to the GraphExplorer.")
            return {"CANCELLED"}

        path = bpy.path.abspath(self.filepath)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            self.report({"ERROR"}, "File not readable: %s" % exc)
            return {"CANCELLED"}
        context.scene.graphexplorer.graph_path = path
        name = os.path.basename(path)

        def done(error):
            if error:
                _session._set_status("Graph not readable: %s" % error)
            else:
                _session._set_status("Loaded: %s" % name)

        _session._set_status("Loading %s …" % name)
        _session.bridge.load_graph_text(text, done)
        return {"FINISHED"}


class GRAPHEXPLORER_OT_refresh_types(Operator):
    bl_idname = "graphexplorer.refresh_types"
    bl_label = "Refresh graph"
    bl_description = "Ask the GraphExplorer which graph is currently loaded"

    def execute(self, _context):
        if _session is None or not _session.connected:
            self.report({"ERROR"}, "Not connected to the GraphExplorer.")
            return {"CANCELLED"}
        _session.graph_types = []
        _session.refresh_types()
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Abgleich
# ---------------------------------------------------------------------------

class GRAPHEXPLORER_OT_link(Operator):
    bl_idname = "graphexplorer.link"
    bl_label = "Match objects"
    bl_description = ("Match the object names against the nodes in the graph — "
                      "all active links together")

    def execute(self, context):
        settings = context.scene.graphexplorer
        if _session is None or not _session.connected:
            self.report({"ERROR"}, "Not connected to the GraphExplorer.")
            return {"CANCELLED"}

        if not _configs_from(settings):
            self.report({"ERROR"},
                        "No complete link. Choose a node type — and for "
                        '"Node property" also the property.')
            return {"CANCELLED"}

        _session._relink()
        return {"FINISHED"}


class GRAPHEXPLORER_OT_report(Operator):
    bl_idname = "graphexplorer.report"
    bl_label = "Match report"
    bl_description = "Which names found each other — and which did not"

    def execute(self, context):
        if _session is None or not _session.sync.is_ready():
            self.report({"ERROR"}, "No links matched yet.")
            return {"CANCELLED"}

        blocks = []
        for label, report in _session.sync.reports():
            blocks.append("── %s ──" % label)
            blocks.append(report.details())
            blocks.append("")
        text = "\n".join(blocks)

        print("\n[GraphExplorer] Match report\n" + text + "\n")
        _popup(context, text, "Match report", "INFO")
        return {"FINISHED"}


class GRAPHEXPLORER_OT_explain(Operator):
    bl_idname = "graphexplorer.explain"
    bl_label = "Explain selection"
    bl_description = ("For when a single object does not link: shows the way from "
                      "its name via the key to the node, and where it breaks off")

    def execute(self, context):
        if _session is None:
            self.report({"ERROR"}, "Not connected to the GraphExplorer.")
            return {"CANCELLED"}
        text = _session.sync.explain_selection()
        print("\n[GraphExplorer] Explain selection\n" + text + "\n")
        _popup(context, text, "Explain selection", "VIEWZOOM")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Einfärben
# ---------------------------------------------------------------------------

class GRAPHEXPLORER_OT_refresh_values(Operator):
    bl_idname = "graphexplorer.refresh_values"
    bl_label = "Refresh values"
    bl_description = "Ask the GraphExplorer which values the nodes of this link offer"

    def execute(self, _context):
        if _session is None or not _session.connected:
            self.report({"ERROR"}, "Not connected to the GraphExplorer.")
            return {"CANCELLED"}
        _session.refresh_values()
        return {"FINISHED"}


class GRAPHEXPLORER_OT_colorize(Operator):
    bl_idname = "graphexplorer.colorize"
    bl_label = "Color by value"
    bl_description = "Color the linked objects by the chosen value — one color per value"
    bl_options = {"UNDO"}

    def execute(self, context):
        settings = context.scene.graphexplorer
        if _session is None or not _session.connected:
            self.report({"ERROR"}, "Not connected to the GraphExplorer.")
            return {"CANCELLED"}

        spec = _session.active_spec()
        if spec is None:
            self.report({"ERROR"}, "No complete link selected.")
            return {"CANCELLED"}

        runtime = _session.sync.runtime_for_spec(spec)
        if runtime is None:
            self.report({"ERROR"}, 'This link has not been matched yet — click '
                                   '"Match objects" first.')
            return {"CANCELLED"}

        raw = settings.color_source_key
        if not raw:
            self.report({"ERROR"}, "No value chosen.")
            return {"CANCELLED"}
        try:
            source = json.loads(raw)
        except ValueError:
            self.report({"ERROR"}, "The chosen value cannot be read.")
            return {"CANCELLED"}

        stem = coloring.source_stem(source, _session.names)
        with_materials = settings.color_materials

        def done(rows):
            # Die Einstellungen neu holen und nicht aus execute mitnehmen: Der
            # Rückruf läuft Ticks später, und zwischendurch kann eine andere
            # Datei geladen worden sein. Ein Zeiger auf die Property-Gruppe von
            # vorhin zeigte dann ins Leere.
            live = _settings()
            if live is None:
                return
            plan = coloring.build_plan(runtime.index, rows or [])
            # Erst alles Frühere zurücknehmen, dann neu setzen. Sonst behielten
            # Objekte, die diesmal keinen Wert haben, ihre Farbe aus dem letzten
            # Durchgang -- und die Legende zeigte etwas anderes als der Viewport.
            shading.clear_all()
            result = shading.apply_plan(plan, stem, with_materials)
            shading.purge_materials()
            _fill_legend(live, plan)

            # Ohne Materialien ist die Objektfarbe der einzige Träger, und die
            # sieht man nur im Solid-Modus mit "Color: Object". Dann wird
            # umgestellt, sonst nicht: Mit Materialien zeigt der Viewport die
            # Einfärbung von sich aus, und in fremden Ansichten herumzuschalten
            # ist eine Zumutung.
            if not with_materials:
                shading.show_object_color()

            note = plan.summary()
            if result.missing:
                note += " · %d objects no longer in the scene" % len(result.missing)
            live.color_note = plan.details()
            _session._set_status(note)

        _session._set_status("Fetching values …")
        _session.bridge.collect_values(spec, [source], done)
        return {"FINISHED"}


class GRAPHEXPLORER_OT_uncolorize(Operator):
    bl_idname = "graphexplorer.uncolorize"
    bl_label = "Remove coloring"
    bl_description = "Restore the materials and object colors from before the coloring"
    bl_options = {"UNDO"}

    def execute(self, context):
        settings = context.scene.graphexplorer
        count = shading.clear_all()
        purged = shading.purge_materials()
        settings.color_groups.clear()
        settings.color_note = ""
        if _session is not None:
            _session._set_status("Coloring removed: %d objects restored, %d materials deleted."
                                 % (count, purged))
        self.report({"INFO"}, "%d objects restored." % count)
        return {"FINISHED"}


class GRAPHEXPLORER_OT_view_object_color(Operator):
    bl_idname = "graphexplorer.view_object_color"
    bl_label = "View object colors"
    bl_description = ("Switch the 3D view to Solid with object colors — otherwise "
                      "it may stay grey although the coloring is in place")

    def execute(self, _context):
        count = shading.show_object_color()
        if not count:
            self.report({"WARNING"}, "No 3D view found.")
        return {"FINISHED"}


class GRAPHEXPLORER_OT_color_report(Operator):
    bl_idname = "graphexplorer.color_report"
    bl_label = "Explain coloring"
    bl_description = "Which groups were made — and what stayed without a color"

    def execute(self, context):
        note = context.scene.graphexplorer.color_note
        if not note:
            self.report({"ERROR"}, "Nothing colored yet.")
            return {"CANCELLED"}
        print("\n[GraphExplorer] Coloring\n" + note + "\n")
        _popup(context, note, "Coloring", "COLOR")
        return {"FINISHED"}


class GRAPHEXPLORER_OT_select_group(Operator):
    bl_idname = "graphexplorer.select_group"
    bl_label = "Select group"
    bl_description = ("Select the objects of this group in Blender — and so show "
                      "them in the GraphExplorer as well")

    def execute(self, context):
        settings = context.scene.graphexplorer
        if not (0 <= settings.active_color_group < len(settings.color_groups)):
            return {"CANCELLED"}
        entry = settings.color_groups[settings.active_color_group]

        mat = bpy.data.materials.get(entry.material or "")
        if mat is None:
            self.report({"ERROR"}, 'Groups can only be selected when colored with '
                                   '"Use materials".')
            return {"CANCELLED"}

        count = 0
        for obj in visible_objects():
            hit = any(slot.material is mat for slot in obj.material_slots)
            obj.select_set(hit)
            if hit:
                count += 1
        self.report({"INFO"}, "%d objects selected." % count)
        return {"FINISHED"}


def _fill_legend(settings, plan):
    """Die Legende aus einem Plan neu aufbauen."""
    global _filling_legend
    _filling_legend = True
    try:
        settings.color_groups.clear()
        for group in plan.groups:
            entry = settings.color_groups.add()
            entry.value = group.value
            entry.count = group.count
            entry.material = group.material
            entry.color = group.color
        settings.active_color_group = 0
    finally:
        _filling_legend = False


def _popup(context, text, title, icon):
    """Mehrzeiligen Text zeigen.

    In ein Aufklappfenster *und* in die Konsole. Nur die Konsole wäre ein
    Fehlgriff: Unter Windows ist sie in Blender standardmäßig zu
    (Window -> Toggle System Console), man klickt also auf den Knopf und
    sieht nichts. Der Ausdruck bleibt trotzdem, weil sich von dort
    herauskopieren lässt.
    """
    lines = text.splitlines()

    def draw(popup, _context):
        for line in lines:
            # Blender rückt Labels nicht ein; führende Leerzeichen gehen
            # verloren. Punkte halten die Gliederung lesbar.
            popup.layout.label(text=line.replace("    ", "· ") or " ")

    context.window_manager.popup_menu(draw, title=title, icon=icon)


# ---------------------------------------------------------------------------
# Seitenleiste
# ---------------------------------------------------------------------------

class GRAPHEXPLORER_PT_panel(Panel):
    bl_label = "GraphExplorer"
    bl_idname = "GRAPHEXPLORER_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GraphExplorer"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.graphexplorer
        connected = _session is not None and _session.connected

        # -- Verbindung --
        layout.label(text="Connected to the GraphExplorer" if connected else "Not connected",
                     icon="LINKED" if connected else "UNLINKED")
        if not connected:
            # Die Kurzanleitung verschwindet, sobald die Leitung steht -- wer
            # verbunden ist, braucht sie nicht mehr.
            box = layout.box()
            col = box.column(align=True)
            for line in HOW_TO_CONNECT:
                col.label(text=line)
            op = box.operator("wm.url_open", text="Get the GraphExplorer", icon="URL")
            op.url = GRAPHEXPLORER_URL
        layout.operator(GRAPHEXPLORER_OT_copy_address.bl_idname, icon="COPYDOWN")

        # -- Graph und Verknüpfungen: nur mit Leitung sinnvoll --
        col = layout.column()
        col.enabled = connected
        row = col.row(align=True)
        row.operator(GRAPHEXPLORER_OT_load_graph.bl_idname, icon="IMPORT")
        row.operator(GRAPHEXPLORER_OT_refresh_types.bl_idname, text="", icon="FILE_REFRESH")
        self._draw_links(col, settings)
        col.prop(settings, "zoom_to_selection")

        self._draw_coloring(layout, settings, connected)

        # Nicht in den Verknüpfungs-Kasten und nicht an einen Abgleich gebunden:
        # Gebraucht wird der Knopf genau dann, wenn etwas nicht tut, was es soll
        # -- und dann soll man ihn finden, ohne erst etwas anderes richtig
        # gemacht zu haben.
        layout.operator(GRAPHEXPLORER_OT_explain.bl_idname, icon="VIEWZOOM")

        if _session is not None and _session.status:
            box = layout.box()
            col = box.column(align=True)
            for line in _wrap(_session.status, 38):
                col.label(text=line)

    def _draw_links(self, layout, settings):
        box = layout.box()
        box.label(text="Links", icon="LINKED")

        row = box.row()
        row.template_list("GRAPHEXPLORER_UL_links", "", settings, "links",
                          settings, "active_link", rows=2)
        col = row.column(align=True)
        col.operator(GRAPHEXPLORER_OT_link_add.bl_idname, text="", icon="ADD")
        col.operator(GRAPHEXPLORER_OT_link_remove.bl_idname, text="", icon="REMOVE")

        if 0 <= settings.active_link < len(settings.links):
            link = settings.links[settings.active_link]
            sub = box.column()
            sub.prop(link, "node_type_ui")
            sub.prop(link, "node_source")
            if link.node_source == SOURCE_ATTR:
                sub.prop(link, "node_attr_ui")
            sub.prop(link, "normalize")
            if link.normalize == NORMALIZE_PATTERN:
                sub.prop(link, "pattern")
        else:
            box.label(text="Click + to add a link", icon="INFO")

        # Gelten für alle Verknüpfungen -- die Benennung ist eine Eigenschaft
        # der Szene, nicht der einzelnen Zuordnung.
        box.separator()
        box.prop(settings, "name_from")
        box.prop(settings, "strip_suffix")
        box.operator(GRAPHEXPLORER_OT_link.bl_idname, icon="FILE_REFRESH")

        if _session is None:
            return

        # Beispiele von beiden Seiten, direkt im Panel: Passen die Namen nicht
        # zueinander, sieht man das hier und nicht erst im Bericht.
        for label, report in _session.sync.reports():
            sub = box.box()
            sub.label(text=label)
            col = sub.column(align=True)
            col.label(text="Objects: %d" % report.layer_keys,
                      icon="OUTLINER_OB_MESH" if report.layer_keys else "ERROR")
            for key in report.layer_samples[:2]:
                col.label(text="    " + key)
            col.label(text="Graph: %d" % report.graph_keys,
                      icon="NODETREE" if report.graph_keys else "ERROR")
            for key in report.graph_samples[:2]:
                col.label(text="    " + key)
            sub.label(text="%d linked" % report.matched_keys,
                      icon="CHECKMARK" if report.matched_keys else "ERROR")

        if _session.sync.is_ready():
            box.operator(GRAPHEXPLORER_OT_report.bl_idname, icon="INFO")

    def _draw_coloring(self, layout, settings, connected):
        """Der Kasten "Color by value".

        Sitzt unter den Verknüpfungen, weil er auf ihnen aufbaut: Eingefärbt
        wird, was verknüpft ist, und nach einem Wert des Knotentyps der
        *aktiven* Verknüpfung. Das steht auch so da -- sonst fragt man sich bei
        zwei Verknüpfungen, welche gerade gemeint ist.

        "Remove coloring" bleibt auch ohne Leitung erreichbar: Eine eingefärbte
        Szene muss sich jederzeit zurückstellen lassen.
        """
        box = layout.box()
        box.label(text="Color by value", icon="COLOR")

        ready = connected and _session.sync.is_ready()
        if not ready:
            box.label(text='Match objects first', icon="INFO")
        else:
            if 0 <= settings.active_link < len(settings.links):
                active = settings.links[settings.active_link]
                box.label(text="Nodes: %s" % (_names().type(active.node_type)
                                             if active.node_type else "?"))

            row = box.row(align=True)
            row.prop(settings, "color_source", text="")
            row.operator(GRAPHEXPLORER_OT_refresh_values.bl_idname, text="", icon="FILE_REFRESH")
            box.prop(settings, "color_materials")

        row = box.row(align=True)
        sub = row.row(align=True)
        sub.enabled = ready
        sub.operator(GRAPHEXPLORER_OT_colorize.bl_idname, icon="BRUSH_DATA")
        row.operator(GRAPHEXPLORER_OT_uncolorize.bl_idname, text="", icon="X")

        if not settings.color_groups:
            return

        box.operator(GRAPHEXPLORER_OT_view_object_color.bl_idname, icon="SHADING_SOLID")

        row = box.row()
        row.template_list("GRAPHEXPLORER_UL_color_groups", "", settings, "color_groups",
                          settings, "active_color_group", rows=4)
        col = row.column(align=True)
        col.operator(GRAPHEXPLORER_OT_select_group.bl_idname, text="", icon="RESTRICT_SELECT_OFF")
        col.operator(GRAPHEXPLORER_OT_color_report.bl_idname, text="", icon="INFO")


def _wrap(text, width):
    """Blender-Labels brechen nicht um; also von Hand."""
    words = text.split()
    lines, current = [], ""
    for word in words:
        if current and len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = (current + " " + word).strip()
    if current:
        lines.append(current)
    return lines[:6]


# ---------------------------------------------------------------------------
# An- und Abmelden
# ---------------------------------------------------------------------------

CLASSES = (
    # GraphExplorerLink und GraphExplorerColorGroup zuerst:
    # GraphExplorerSettings verweist als CollectionProperty darauf und braucht
    # die Klassen bereits registriert.
    GraphExplorerLink,
    GraphExplorerColorGroup,
    GraphExplorerSettings,
    GRAPHEXPLORER_UL_links,
    GRAPHEXPLORER_UL_color_groups,
    GRAPHEXPLORER_OT_link_add,
    GRAPHEXPLORER_OT_link_remove,
    GRAPHEXPLORER_OT_copy_address,
    GRAPHEXPLORER_OT_load_graph,
    GRAPHEXPLORER_OT_refresh_types,
    GRAPHEXPLORER_OT_link,
    GRAPHEXPLORER_OT_report,
    GRAPHEXPLORER_OT_explain,
    GRAPHEXPLORER_OT_refresh_values,
    GRAPHEXPLORER_OT_colorize,
    GRAPHEXPLORER_OT_uncolorize,
    GRAPHEXPLORER_OT_view_object_color,
    GRAPHEXPLORER_OT_color_report,
    GRAPHEXPLORER_OT_select_group,
    GRAPHEXPLORER_PT_panel,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.graphexplorer = PointerProperty(type=GraphExplorerSettings)


def unregister():
    # Erst die Sitzung beenden: Ein offener Port und ein laufender Zeitgeber
    # überleben das Abschalten des Addons sonst und blockieren beim nächsten
    # Anschalten den Neustart.
    global _session
    if _session is not None:
        _session.stop()
        _session = None
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)

    del bpy.types.Scene.graphexplorer
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
