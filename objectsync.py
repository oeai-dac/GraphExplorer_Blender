"""Die Blender-Seite der Kopplung: Objektnamen lesen, Auswahl lesen und setzen.

Das Gegenstück zu ``layersync.py`` im QGIS-Plugin -- mit drei Unterschieden,
die aus Blender selbst kommen:

**1. Es gibt kein "Auswahl geändert"-Signal.** Blender kennt nichts, was
``layer.selectionChanged`` entspräche. Deshalb wird verglichen: Bei jedem Tick
die aktuelle Auswahl gegen die zuletzt gesehene halten. Bei 200 ms merkt das
niemand, und es ist robuster als der Umweg über ``depsgraph_update_post``, der
bei jeder Bewegung im Viewport feuert.

**2. Der Name ist der einzige Träger.** In QGIS ließe sich auf ein sauberes
Attributfeld ausweichen, wenn der Abgleich nicht griffe. Hier gibt es diesen
Ausweg nicht -- deshalb wird der Blender-eigene Duplikatzähler ".001" beim
Lesen abgeschnitten, bevor irgendetwas normalisiert wird.

**3. Ausgeblendete Objekte lassen sich nicht auswählen.** ``select_set()``
bleibt bei ihnen wirkungslos, ohne das zu melden. Was tatsächlich ausgewählt
wurde, muss deshalb nachgelesen und nicht angenommen werden.

**Mehrere Verknüpfungen.** Wie in QGIS: Eine Szene kann Meshes mit
SE-Bezeichnung *und* Meshes mit Fundbezeichnung enthalten. Jede Verknüpfung
bekommt ihren eigenen Index -- die Objektseite ist bei allen dieselbe (es sind
dieselben Objekte), die Graph-Seite unterscheidet sich nach Knotentyp. Ein
SE-Mesh findet deshalb nur im SE-Index einen Knoten, ein Fund-Mesh nur im
Fund-Index, und beide dürfen nebeneinander bestehen.
"""

import re

import bpy

from .linking import SOURCE_ATTR, SOURCE_ID, DisplayNames, LinkIndex

#: Woher der Name genommen wird. Objekt- und Mesh-Name laufen im Alltag
#: auseinander -- das gehört gewählt, nicht geraten.
NAME_FROM_OBJECT = "OBJECT"
NAME_FROM_MESH = "MESH"

# Blender hängt an doppelt vergebene Namen ".001", ".002" an. Das ist eine
# Eigenheit von Blender, kein Bestandteil der Bezeichnung -- deshalb wird es
# hier abgeschnitten, wo die Namen gelesen werden, und nicht in linking.py als
# Normalisierungsstufe. Der Unterschied ist nicht kosmetisch: Ein Attributwert
# im Graphen darf durchaus "2.001" heißen und eine Kommazahl sein. Und
# linking.py bleibt so Zeichen für Zeichen dieselbe Datei wie im QGIS-Plugin.
BLENDER_SUFFIX = re.compile(r"\.\d{3}$")


def strip_blender_suffix(name):
    """``"EDP22-SE2004.001"`` -> ``"EDP22-SE2004"``.

    Dass danach zwei Objekte denselben Schlüssel tragen, ist kein Unfall,
    sondern der Zweck: Eine SE, die in zwei Teilen modelliert wurde, hat zwei
    Objekte, und beide sollen aufleuchten, wenn ihr Knoten ausgewählt wird.
    Der Index ist von Anfang an n:m.

    Die Reihenfolge -- erst abschneiden, dann normalisieren -- ist ebenfalls
    nicht kosmetisch: Sonst wäre bei "EDP22-SE2004.001" der Zähler die *letzte
    Zahl*, und ausgerechnet die SE mit zwei Geometrien fielen aus der Zuordnung.
    """
    return BLENDER_SUFFIX.sub("", name or "")


def object_key(obj, name_from=NAME_FROM_OBJECT, strip_suffix=True):
    """Der Rohschlüssel eines Objekts, vor jeder Normalisierung."""
    if name_from == NAME_FROM_MESH:
        name = obj.data.name if obj.data is not None else ""
    else:
        name = obj.name
    return strip_blender_suffix(name) if strip_suffix else name


def visible_objects():
    """Alle Objekte, die überhaupt in Frage kommen.

    ``view_layer.objects`` statt ``bpy.data.objects``: Was in einer
    ausgeschlossenen Sammlung liegt, soll nicht mit ausgewählt werden -- eine
    Auswahl, die man nicht sieht, verwirrt mehr, als sie nützt.
    """
    try:
        return list(bpy.context.view_layer.objects)
    except AttributeError:
        # In manchen Kontexten (etwa beim Laden einer Datei) gibt es noch
        # keinen view_layer. Dann eben diesmal nichts.
        return []


def link_label(node_type, node_source, node_attr, names=None):
    """``"Stratigraphic Unit · Node label"`` -- unter den Namen des GraphExplorers."""
    names = names or DisplayNames()
    if node_source == SOURCE_ATTR:
        right = names.attr(node_attr) if node_attr else "?"
    elif node_source == SOURCE_ID:
        right = "Node ID"
    else:
        right = "Node label"
    return "%s · %s" % (names.type(node_type) if node_type else "(no node type)", right)


class LinkRuntime(object):
    """Eine aktive Verknüpfung: Konfiguration, Index, letzter Bericht."""

    def __init__(self, config, sync):
        self.config = config
        self.index = LinkIndex(config)
        self.report = None
        self._sync = sync

    @property
    def label(self):
        # Erst beim Abruf gebaut: Die Anzeigenamen kommen mit describeGraph und
        # können nach dem Abgleich eintreffen oder sich mit dem Graphen ändern.
        c = self.config
        return link_label(c.node_type, c.node_source, c.node_attr, self._sync.names)


class ObjectSync(object):
    """Hält die Indizes und vermittelt zwischen Blender-Auswahl und Explorer."""

    def __init__(self, bridge):
        self.bridge = bridge
        self.runtimes = []
        self.zoom_to_selection = False
        #: Die Namen, unter denen der GraphExplorer Typen und Properties zeigt.
        #: Setzt die Sitzung, sobald describeGraph geantwortet hat.
        self.names = DisplayNames()

        self._name_from = NAME_FROM_OBJECT
        self._strip_suffix = True

        # Die Rückkopplungssperre. Blender meldet keine Auswahländerung, es wird
        # verglichen -- also genügt *ein* Merker: der zuletzt gesehene Stand.
        # Wird eine Auswahl aus dem Explorer gesetzt, wird er gleich
        # mitgeschrieben; der nächste Vergleich findet dann keine Änderung und
        # schweigt.
        #
        # Zwei frühere Fassungen sind hier gescheitert, beide mit demselben
        # Muster "geht meistens, manchmal nicht":
        #   * ein zweiter Merker "was haben wir zuletzt gesetzt", der nur in
        #     einer Richtung fortgeschrieben wurde und deshalb veraltete;
        #   * das Vermerken der *Absicht* statt des Ergebnisses -- bei
        #     ausgeblendeten Objekten weicht beides voneinander ab.
        self._last_seen_selection = None

    # -- Aufbau ---------------------------------------------------------------

    def rebuild(self, configs, name_from, strip_suffix, on_done=None):
        """Alle Verknüpfungen neu aufbauen.

        Die Objektseite steht sofort, die Graph-Seite kommt je Verknüpfung
        asynchron aus dem Browser zurück. ``on_done`` läuft erst, wenn alle da
        sind -- sonst zeigte der Bericht einen Zwischenstand.
        """
        self._name_from = name_from
        self._strip_suffix = strip_suffix
        self.runtimes = [LinkRuntime(c, self) for c in configs if c.node_type]

        objects = visible_objects()
        pairs = [(obj.name, object_key(obj, name_from, strip_suffix)) for obj in objects]
        for runtime in self.runtimes:
            # Dieselben Objekte für alle Verknüpfungen -- unterschieden wird
            # allein über die Graph-Seite.
            runtime.index.set_layer_side(pairs)

        # Beim nächsten Tick soll die Auswahl neu gemeldet werden, auch wenn
        # sie sich nicht geändert hat -- die Indizes sind ja andere.
        self._last_seen_selection = None

        self.bridge.set_link_specs([r.config.js_spec() for r in self.runtimes])

        if not self.runtimes:
            if on_done:
                on_done([])
            return

        pending = {"count": len(self.runtimes)}

        def make_callback(runtime):
            def done(entries):
                runtime.index.set_graph_side(entries or [])
                runtime.report = runtime.index.report()
                pending["count"] -= 1
                if pending["count"] == 0 and on_done:
                    on_done(self.reports())
            return done

        for runtime in self.runtimes:
            self.bridge.collect_keys(runtime.config.js_spec(), make_callback(runtime))

    def is_ready(self):
        return any(r.report is not None for r in self.runtimes)

    def runtime_for_spec(self, spec):
        """Die aktive Verknüpfung zu einer Spezifikation -- oder ``None``.

        Gebraucht vom Einfärben, das sich auf *eine* Verknüpfung bezieht. Über
        die Spezifikation und nicht über den Listenindex: In den Einstellungen
        stehen auch abgeschaltete und unvollständige Verknüpfungen, unter den
        Runtimes nicht -- die Positionen laufen also auseinander, und zwar
        genau dann, wenn jemand eine abschaltet.
        """
        for runtime in self.runtimes:
            if runtime.report is not None and runtime.config.js_spec() == spec:
                return runtime
        return None

    def reports(self):
        return [(r.label, r.report) for r in self.runtimes if r.report is not None]

    def summary(self):
        parts = ["%s: %s" % (label, report.summary()) for label, report in self.reports()]
        return " · ".join(parts) if parts else "No links matched yet."

    # -- Blender -> Explorer ---------------------------------------------------
    #
    # Objekt-IDs sind hier die Objektnamen: In Blender sind sie eindeutig und
    # überleben, anders als ein Zeiger, den nächsten Undo-Schritt.

    def _nodes_for_names(self, names):
        """Knoten zu einer Menge von Objektnamen -- über alle Verknüpfungen.

        Ein Objekt findet nur in der Verknüpfung einen Knoten, deren Knotentyp
        zu seiner Bezeichnung passt. Über alle zu gehen und einzusammeln ist
        deshalb nicht unscharf, sondern genau richtig: Es ist die Frage "welche
        Verknüpfung auch immer -- gibt es einen Knoten dazu?"
        """
        out = []
        for runtime in self.runtimes:
            if runtime.report is None:
                continue
            keys = runtime.index.keys_for_feature_ids(names)
            # ...for_normalized_keys: keys_for_feature_ids liefert schon
            # normalisierte Schlüssel, ein zweiter Durchlauf würde sie
            # verfälschen (siehe linking.py).
            for node_id in runtime.index.node_ids_for_normalized_keys(keys):
                if node_id not in out:
                    out.append(node_id)
        return out

    def poll_selection(self):
        """Aus dem Zeitgeber aufzurufen. Meldet eine geänderte Auswahl."""
        if not self.is_ready():
            return

        selected = {obj.name for obj in visible_objects() if obj.select_get()}
        if selected == self._last_seen_selection:
            return
        self._last_seen_selection = selected

        if not selected:
            return  # Abwählen in Blender lässt den Explorer stehen -- wie in QGIS

        node_ids = self._nodes_for_names(selected)
        if not node_ids:
            return
        self.bridge.select_nodes(node_ids)

    # -- Explorer -> Blender ---------------------------------------------------

    def on_graph_selection(self, _node_id, keys):
        if not self.is_ready():
            return

        # Über alle Verknüpfungen einsammeln: Der Explorer schickt die
        # Schlüssel, die er aus dem Knoten und seiner Nachbarschaft aufgelöst
        # hat -- welche Verknüpfung darauf passt, entscheidet sich hier.
        names = set()
        for runtime in self.runtimes:
            if runtime.report is None:
                continue
            names.update(runtime.index.feature_ids_for_keys(keys))

        objects = visible_objects()
        for obj in objects:
            obj.select_set(obj.name in names)

        if names:
            active = next((obj for obj in objects if obj.name in names), None)
            if active is not None:
                try:
                    bpy.context.view_layer.objects.active = active
                except (AttributeError, RuntimeError):
                    pass

        # Den **tatsächlichen** Stand vermerken, nicht die Absicht: select_set()
        # bleibt bei ausgeblendeten Objekten wirkungslos, ohne das zu melden.
        # Stünde hier die Absicht, wiche der Merker vom wirklichen Stand ab --
        # der nächste Tick hielte das für eine Benutzeraktion, schickte sie
        # zurück und überschriebe den Merker. Wählte man dieselben Objekte
        # danach von Hand aus, gälte das als "unverändert" und würde
        # verschluckt.
        self._last_seen_selection = {obj.name for obj in objects if obj.select_get()}

        if names and self.zoom_to_selection:
            _zoom_to_selected()

    # -- Diagnose --------------------------------------------------------------

    def explain_selection(self):
        """Warum passiert bei *dieser* Auswahl nichts? Schritt für Schritt.

        Gedacht für den Fall, der sich nicht aus der Ferne erklären lässt: Die
        Kopplung geht bei fast allen Einheiten, bei einer aber nicht. Dann
        nützt keine allgemeine Aussage, sondern nur der konkrete Weg vom Objekt
        über seinen Namen und den Schlüssel zum Knoten -- und die Stelle, an
        der er abbricht.
        """
        if not self.is_ready():
            return "No links matched yet — click \"Match objects\" first."

        lines = []
        objects = visible_objects()
        selected = [obj for obj in objects if obj.select_get()]
        if not selected:
            return "Nothing selected."

        # Ausgeblendete Objekte lassen sich nicht auswählen. Wenn der Explorer
        # eine Einheit anwählt, deren Geometrie ausgeblendet ist, passiert in
        # Blender sichtbar nichts -- und das ist keine Fehlfunktion der
        # Kopplung, sondern der Zustand der Szene.
        hidden = [obj for obj in objects if not obj.visible_get()]
        if hidden:
            lines.append("Note: %d object(s) are hidden and cannot be selected." % len(hidden))
            lines.append("This affects the direction GraphExplorer -> Blender.")
            lines.append("")

        lines.append("%d object(s) selected, %d link(s) active:"
                     % (len(selected), len(self.runtimes)))

        for obj in selected[:8]:
            raw = object_key(obj, self._name_from, self._strip_suffix)
            lines.append("")
            lines.append(obj.name)
            lines.append("    Name used for matching: %r" % raw)

            found_any = False
            for runtime in self.runtimes:
                if runtime.report is None:
                    continue
                keys = runtime.index.keys_for_feature_ids([obj.name])
                if not keys:
                    lines.append("    [%s] no key from this name" % runtime.label)
                    continue
                nodes = runtime.index.node_ids_for_normalized_keys(keys)
                if nodes:
                    found_any = True
                    lines.append("    [%s] %r -> %s" % (runtime.label, keys[0], ", ".join(nodes[:3])))
                else:
                    lines.append("    [%s] %r -> no node" % (runtime.label, keys[0]))
                    near = [k for k in sorted(runtime.index._key_to_nodes)
                            if k[:4] == keys[0][:4]][:3]
                    if near:
                        lines.append("        Similar in the graph: %s"
                                     % ", ".join(repr(n) for n in near))

            if not found_any:
                lines.append("    -> no link matches this object.")

        if len(selected) > 8:
            lines.append("")
            lines.append("… and %d more" % (len(selected) - 8))

        # Der zweithäufigste Grund für "es passiert nichts": Es *passiert*
        # etwas, nur sieht man es nicht -- der Explorer zeigt den Knoten schon.
        lines.append("")
        lines.append("If the GraphExplorer already shows this node, nothing changes")
        lines.append("there, so a connected QGIS gets no new selection either. That is")
        lines.append("intended, even though it looks like a fault.")
        return "\n".join(lines)


def _zoom_to_selected():
    """Auf die Auswahl zoomen.

    Der heikelste Aufruf im ganzen Addon: Operatoren aus einem Zeitgeber heraus
    brauchen einen passenden Kontext, den es dort nicht von selbst gibt. Ohne
    die Überschreibung wirft Blender einen Kontextfehler; mit ihr funktioniert
    es, sofern gerade ein 3D-Viewport offen ist. Und wenn nicht, ist ein
    ausgelassener Zoom kein Grund, die Kopplung abzubrechen.
    """
    window = getattr(bpy.context, "window", None)
    screen = getattr(window, "screen", None) if window else None
    if screen is None:
        return
    for area in screen.areas:
        if area.type != "VIEW_3D":
            continue
        region = next((r for r in area.regions if r.type == "WINDOW"), None)
        if region is None:
            continue
        try:
            with bpy.context.temp_override(window=window, area=area, region=region):
                bpy.ops.view3d.view_selected()
        except (RuntimeError, TypeError, AttributeError):
            pass
        return
