"""Prüft addon.py und shading.py gegen einen bpy-Ersatz.

    python tests/run-addon-checks.py

Bisher galt: „Nur addon.py und objectsync.py brauchen bpy, alles andere wird
ohne Blender geprüft." Das stimmt weiter -- nur ist die Seitenleiste inzwischen
groß genug, dass ein Tippfehler darin niemandem auffällt, bis Blender an ihrer
Stelle einen roten Kasten zeichnet. Für ein Werkzeug, das im Institut
herumgehen soll, ist das der schlechteste Ort für so einen Fehler.

Geprüft wird deshalb die **Verdrahtung** (siehe ``_bpy_stub.py``):

- Lässt sich das Addon anmelden, und existiert jede Klasse in ``CLASSES``?
- Läuft ``draw()`` durch, in beiden Zuständen -- ohne Sitzung und mit?
- Bringt das Einfärben die Objekte in den Zustand, den die Legende behauptet?
- Und, der Prüfstein: Führt **Aufheben** genau dorthin zurück, wo es herkam?

Der letzte Punkt ist der einzige, bei dem ein Fehler bleibende Schäden macht.
Ein Grabungsmodell trägt texturierte Meshes aus der Photogrammetrie; deren
Material ist Arbeitsergebnis. Eine Einfärbung, die es nicht zurückgeben kann,
ist kein Werkzeug, sondern ein Unfall.
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import _bpy_stub  # noqa: E402

bpy = _bpy_stub.install()

# Erst nach dem Ersatz importieren -- addon.py holt sich bpy beim Laden.
# Als Paket, damit die relativen Importe (from .coloring import …) tragen, und
# unter dem Namen, den auch Blender vergibt: Der Ordnername taugt dafür nicht,
# ein Klon heißt "GraphExplorer-Blender", und der Bindestrich ist in Python
# kein Bestandteil eines Modulnamens.
_spec = importlib.util.spec_from_file_location(
    "graphexplorer_link", os.path.join(ROOT, "__init__.py"),
    submodule_search_locations=[ROOT])
sys.modules["graphexplorer_link"] = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sys.modules["graphexplorer_link"])

from graphexplorer_link import addon, coloring, shading  # noqa: E402
from graphexplorer_link.linking import (  # noqa: E402
    NORMALIZE_LAST_NUMBER,
    NORMALIZE_LOOSE,
    SOURCE_LABEL,
    DisplayNames,
    LinkConfig,
    LinkIndex,
    normalize_key,
)
from graphexplorer_link.objectsync import strip_blender_suffix  # noqa: E402

failures = []


def check(name, got, want):
    if got != want:
        failures.append("%s\n    erwartet: %r\n    bekommen: %r" % (name, want, got))


# -- Der Duplikatzähler ------------------------------------------------------
#
# Steht hier und nicht in run-linking-checks.py, weil er in objectsync.py lebt:
# linking.py bleibt Zeichen für Zeichen dieselbe Datei wie im QGIS-Plugin.

NAME = "T2_SE2004"
DUP = "T2_SE2004.001"
check("Suffix abgeschnitten", strip_blender_suffix(DUP), NAME)
check("Name ohne Suffix bleibt", strip_blender_suffix(NAME), NAME)
check("dreistellig ist Bedingung", strip_blender_suffix("Profil.2"), "Profil.2")
check("nur am Ende", strip_blender_suffix("A.001.B"), "A.001.B")
check("None verträgt sich", strip_blender_suffix(None), "")

# Der Grund, warum abgeschnitten wird, *bevor* normalisiert wird: Sonst ist der
# Duplikatzähler die letzte Zahl im Namen, und ausgerechnet die SE mit zwei
# Geometrien fielen aus der Zuordnung.
check("Falle: Suffix nicht abgeschnitten", normalize_key(DUP, NORMALIZE_LAST_NUMBER), "001")
check("richtig herum",
      normalize_key(strip_blender_suffix(DUP), NORMALIZE_LAST_NUMBER), "2004")


# -- Wertquellen unter den Namen des GraphExplorers ---------------------------

REL = "http://www.cidoc-crm.org/cidoc-crm/P2_has_type"
names = DisplayNames({"labels": {"rels": {REL: "has interpretation"}}})
check("Beschriftung", coloring.source_text({"kind": "label"}, names), "Node label")
check("Anzahl einer Beziehung",
      coloring.source_text({"kind": "rel", "dir": "out", "rel": REL, "what": "count"}, names),
      "→ has interpretation: count")
check("eingehend",
      coloring.source_text({"kind": "rel", "dir": "in", "rel": REL, "what": "label"}, names),
      "← has interpretation: labels")
check("Materialname ohne URI",
      coloring.source_stem({"kind": "rel", "dir": "out", "rel": REL, "what": "label"}, names),
      "has interpretation")


# -- Anmelden -----------------------------------------------------------------

addon.register()
check("alle Klassen angemeldet", len(bpy.registered), len(addon.CLASSES))
check("die Panel-Klasse ist dabei", addon.GRAPHEXPLORER_PT_panel in bpy.registered, True)

settings = addon.GraphExplorerSettings()
scene = type("Scene", (object,), {})()
scene.graphexplorer = settings
context = _bpy_stub.Context(scene)
bpy.context = context


# -- Die Seitenleiste ---------------------------------------------------------

panel = addon.GRAPHEXPLORER_PT_panel()


def draw():
    layout = _bpy_stub.Layout()
    panel.layout = layout
    panel.draw(context)
    return layout.sink


# Ohne Sitzung: Kurzanleitung und "Copy connection address". Der Zweig läuft
# bei jedem, der das Addon zum ersten Mal aufmacht.
addon._session = None
sink = draw()
check("ohne Sitzung: Adresse kopieren", "graphexplorer.copy_address" in sink["operators"], True)
check("ohne Sitzung: Link zum GraphExplorer", "wm.url_open" in sink["operators"], True)
check("Aufheben bleibt auch ohne Leitung erreichbar",
      "graphexplorer.uncolorize" in sink["operators"], True)

# Erst der Klick öffnet den Port -- und legt die Adresse mit Marke ab.
copy = addon.GRAPHEXPLORER_OT_copy_address()
check("Adresse kopieren", copy.execute(context), {"FINISHED"})
check("die Adresse trägt die Marke",
      context.window_manager.clipboard.startswith("ws://127.0.0.1:")
      and "?token=" in context.window_manager.clipboard, True)
check("der Zeitgeber läuft", bpy.app.timers.is_registered(addon._tick), True)
check("noch nicht verbunden", addon._session.connected, False)
addon._session.stop()
addon._session = None


class FakeBridge(object):
    """Nimmt Aufrufe entgegen und hält die Rückrufe fest."""

    def __init__(self):
        self.calls = []

    def describe_values(self, spec, callback):
        self.calls.append(("describeValues", spec, callback))

    def collect_values(self, spec, sources, callback):
        self.calls.append(("collectValues", spec, sources, callback))

    def collect_keys(self, spec, callback):
        self.calls.append(("collectKeys", spec, callback))

    def set_link_specs(self, specs):
        self.calls.append(("setSpecs", specs))


class FakeSync(object):
    def __init__(self, runtime=None):
        self.runtime = runtime
        self.runtimes = [runtime] if runtime else []

    def is_ready(self):
        return self.runtime is not None

    def reports(self):
        return []

    def summary(self):
        return "abgeglichen"

    def runtime_for_spec(self, _spec):
        return self.runtime


class FakeSession(object):
    def __init__(self, runtime=None):
        self.bridge = FakeBridge()
        self.sync = FakeSync(runtime)
        self.connected = True
        self.status = "connected"
        self.names = DisplayNames()
        self.graph_types = [{"type": "Befund", "count": 3, "attrs": ["interpretation"]}]
        self.value_sources = [
            {"source": {"kind": "attr", "attr": "interpretation"},
             "label": "interpretation", "count": 3, "of": 3, "kind": "text",
             "sample": "Grube"},
        ]
        self.value_spec = None

    def active_spec(self):
        return {"nodeType": "Befund", "nodeSource": "label", "nodeAttr": ""}

    def refresh_values(self):
        self.bridge.describe_values(self.active_spec(), lambda info: None)

    def _set_status(self, text):
        self.status = text

    def stop(self):
        self.stopped = True


# Mit Sitzung, aber ohne Abgleich: Der Einfärbe-Kasten muss sagen, was fehlt,
# statt Knöpfe anzubieten, die nichts tun können.
addon._session = FakeSession()
settings.links.add()
settings.links[0].node_type = "Befund"
sink = draw()
check("noch keine Wertauswahl ohne Abgleich", "color_source" in sink["props"], False)

# Und mit Abgleich: der ganze Kasten.
runtime = type("Runtime", (object,), {})()
runtime.index = LinkIndex(LinkConfig(node_type="Befund", node_source=SOURCE_LABEL,
                                     normalize=NORMALIZE_LOOSE))
runtime.index.set_layer_side([("SE_2001", "SE 2001"), ("SE_2002", "SE 2002"),
                              ("SE_2003", "SE 2003")])
runtime.report = runtime.index.report()

addon._session = FakeSession(runtime)
sink = draw()
for idname in ("graphexplorer.colorize", "graphexplorer.uncolorize",
               "graphexplorer.refresh_values"):
    check("Knopf vorhanden: %s" % idname, idname in sink["operators"], True)
check("die Wertquelle steht im Panel", "color_source" in sink["props"], True)


# -- Einfärben ----------------------------------------------------------------

bpy.data.objects.append(_bpy_stub.Object("SE_2001"))
# Ein Objekt mit eigenem Material -- der Fall, der wehtut, wenn das Aufheben
# nicht trägt: ein texturiertes Mesh aus der Photogrammetrie.
textured = _bpy_stub.Material("Photogrammetrie")
bpy.data.materials.append(textured)
bpy.data.objects.append(_bpy_stub.Object("SE_2002", materials=[textured]))
bpy.data.objects.append(_bpy_stub.Object("SE_2003"))

# Die Auswahlliste schreibt über set() in color_source_key -- der Ersatz kennt
# get/set nicht, also wird der gespeicherte Wert direkt gesetzt.
settings.color_source_key = addon.source_key({"kind": "attr", "attr": "interpretation"})
settings.color_materials = True

# Blender merkt sich bei dynamischen Auswahllisten die Position, nicht die
# Kennung. Die Liste muss deshalb über die Kennung zurückfinden -- auch wenn
# sich die Reihenfolge ändert.
items = addon._build_value_items(settings)
check("die Wertliste nennt die Quelle wie der Explorer", items[0][1], "interpretation (3/3)")
check("die gespeicherte Wahl wird gefunden", addon._get_value(settings), items[0][3])
addon._session.value_sources.insert(0, {"source": {"kind": "label"}, "count": 3, "of": 3})
check("auch an neuer Position",
      addon._build_value_items(settings)[addon._get_value(settings)][0],
      settings.color_source_key)
addon._session.value_sources.pop(0)

# Dasselbe für den Knotentyp: Ohne Graph (frisch geöffnete .blend) darf die
# Verknüpfung nicht leer erscheinen.
link0 = settings.links[0]
type_items = addon._build_type_items(link0)
check("Knotentyp gefunden", type_items[addon._get_type(link0)][0], "Befund")
saved_types, addon._session.graph_types = addon._session.graph_types, []
type_items = addon._build_type_items(link0)
check("ohne Graph bleibt der gespeicherte Typ sichtbar",
      type_items[addon._get_type(link0)][0], "Befund")
check("und ist gekennzeichnet", "not in the loaded graph" in type_items[0][1], True)
addon._set_type(link0, addon._get_type(link0))
check("set() schreibt ihn unverändert zurück", link0.node_type, "Befund")
addon._session.graph_types = saved_types

colorize = addon.GRAPHEXPLORER_OT_colorize()
check("Einfärben angestoßen", colorize.execute(context), {"FINISHED"})

call = addon._session.bridge.calls[-1]
check("die Brücke wird nach den Werten gefragt", call[0], "collectValues")
check("und zwar nach genau einer Quelle", call[2],
      [{"kind": "attr", "attr": "interpretation"}])

# Jetzt die Antwort einspielen -- so, wie sie aus dem Browser käme.
call[3]([
    {"id": "B_1", "key": "SE 2001", "values": ["Grube"]},
    {"id": "B_2", "key": "SE 2002", "values": ["Grube"]},
    {"id": "B_3", "key": "SE 2003", "values": ["Mauer"]},
])

check("die Legende steht", [g.value for g in settings.color_groups], ["Grube", "Mauer"])
check("mit Anzahlen", [g.count for g in settings.color_groups], [2, 1])
check("und mit Materialien",
      [g.material for g in settings.color_groups],
      ["GE_interpretation_Grube", "GE_interpretation_Mauer"])

grube = bpy.data.materials.get("GE_interpretation_Grube")
check("das Material ist angelegt", grube is not None, True)
check("gleiche Ansprache, gleiches Material",
      [slot.material for obj in (bpy.data.objects.get("SE_2001"),
                                 bpy.data.objects.get("SE_2002"))
       for slot in obj.material_slots],
      [grube, grube])
check("die Objektfarbe ist gesetzt",
      bpy.data.objects.get("SE_2001").color[:3], settings.color_groups[0].color[:])

# Das eigene Material des texturierten Meshes darf nicht ersetzt worden sein --
# es ist nur verdeckt. Genau dafür sind die objektgebundenen Slots da.
check("das Mesh-Material liegt unberührt darunter",
      bpy.data.objects.get("SE_2002").data.materials[0], textured)
check("der Slot ist objektgebunden",
      [slot.link for slot in bpy.data.objects.get("SE_2002").material_slots], ["OBJECT"])


# -- Farbe nachträglich ändern ------------------------------------------------

settings.color_groups[0].color = (0.1, 0.2, 0.3)
check("die neue Farbe steht am Material", grube.diffuse_color, (0.1, 0.2, 0.3, 1.0))
check("und an den Objekten der Gruppe",
      bpy.data.objects.get("SE_2001").color, (0.1, 0.2, 0.3, 1.0))
check("aber nicht an denen der anderen",
      bpy.data.objects.get("SE_2003").color != (0.1, 0.2, 0.3, 1.0), True)


# -- Aufheben -----------------------------------------------------------------
#
# Der Prüfstein. Alles andere lässt sich notfalls von Hand richten; ein
# verlorenes Material aus der Photogrammetrie nicht.

uncolorize = addon.GRAPHEXPLORER_OT_uncolorize()
uncolorize.execute(context)

se2002 = bpy.data.objects.get("SE_2002")
check("das eigene Material ist wieder da", se2002.data.materials, [textured])
check("der Slot hängt wieder an den Mesh-Daten",
      [slot.link for slot in se2002.material_slots], ["DATA"])
check("und liefert wieder das eigene Material",
      [slot.material for slot in se2002.material_slots], [textured])

se2001 = bpy.data.objects.get("SE_2001")
check("das Objekt ohne Material hat auch wieder keinen Slot",
      len(se2001.data.materials), 0)
check("die Objektfarbe ist zurückgestellt", tuple(se2001.color), (1.0, 1.0, 1.0, 1.0))
check("der Sicherungsvermerk ist weg", shading.BACKUP_PROP in se2001, False)
check("die Legende ist leer", len(settings.color_groups), 0)
check("und die Materialien sind abgeräumt",
      bpy.data.materials.get("GE_interpretation_Grube"), None)
check("fremde Materialien bleiben", bpy.data.materials.get("Photogrammetrie"), textured)


# -- Zweimal einfärben --------------------------------------------------------
#
# Beim zweiten Durchgang darf nicht der *eingefärbte* Zustand gesichert werden;
# sonst führte Aufheben auf eine Farbe zurück statt auf das Material von einst.

for _ in range(2):
    colorize.execute(context)
    addon._session.bridge.calls[-1][3]([
        {"id": "B_1", "key": "SE 2001", "values": ["Grube"]},
        {"id": "B_2", "key": "SE 2002", "values": ["Mauer"]},
    ])
uncolorize.execute(context)
check("auch nach zwei Durchgängen kommt das Material zurück",
      bpy.data.objects.get("SE_2002").data.materials, [textured])
check("und kein Slot bleibt übrig",
      len(bpy.data.objects.get("SE_2001").data.materials), 0)

# Objekte, die beim zweiten Mal keinen Wert mehr haben, dürfen ihre alte Farbe
# nicht behalten -- sonst zeigte der Viewport etwas anderes als die Legende.
colorize.execute(context)
addon._session.bridge.calls[-1][3]([
    {"id": "B_1", "key": "SE 2001", "values": ["Grube"]},
    {"id": "B_3", "key": "SE 2003", "values": ["Mauer"]},
])
check("SE_2002 ist diesmal außen vor",
      tuple(bpy.data.objects.get("SE_2002").color), (1.0, 1.0, 1.0, 1.0))
check("und sein Material ist wieder das eigene",
      [slot.material for slot in bpy.data.objects.get("SE_2002").material_slots],
      [textured])
uncolorize.execute(context)


# -- Ohne Materialien ---------------------------------------------------------

settings.color_materials = False
colorize.execute(context)
addon._session.bridge.calls[-1][3]([{"id": "B_1", "key": "SE 2001", "values": ["Grube"]}])
check("nur die Objektfarbe",
      bpy.data.objects.get("SE_2001").color[:3], settings.color_groups[0].color[:])
check("kein Material angelegt",
      [m.name for m in bpy.data.materials if m.name.startswith(coloring.MATERIAL_PREFIX)], [])
uncolorize.execute(context)
check("und auch das lässt sich zurücknehmen",
      tuple(bpy.data.objects.get("SE_2001").color), (1.0, 1.0, 1.0, 1.0))


# -- Abmelden -----------------------------------------------------------------

addon.unregister()
check("alles wieder abgemeldet", bpy.registered, [])


# -- Ergebnis -----------------------------------------------------------------

if failures:
    print("\n%d Prüfung(en) fehlgeschlagen:\n" % len(failures))
    for f in failures:
        print("  - %s\n" % f)
    sys.exit(1)
print("addon.py + shading.py: alle Prüfungen bestanden (gegen den bpy-Ersatz).")
