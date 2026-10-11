"""Objekte nach einem Wert aus dem Graphen einfärben -- die Rechnung dazu.

Wie ``linking.py`` bewusst **ohne bpy**. Das Setzen von Materialien braucht
Blender (``shading.py``), aber *was* welche Farbe bekommt, entscheidet sich
hier: wie aus Werten Gruppen werden, in welcher Reihenfolge sie stehen und
welche Farbe jede bekommt. ``tests/run-coloring-checks.py`` fährt das mit
reinem Python durch.

**Warum das nicht einfach "je Wert eine Farbe" ist.** Drei Fälle, die in echten
Beständen sofort auftreten:

1. *Ein Objekt, mehrere Werte.* "Das Material der Funde dieser SE" sind selten
   eines. Die Werte werden verbunden und die Verbindung ist die Gruppe --
   "Keramik; Eisen" ist eine eigene Ansprache und keine halbe Keramik.
2. *Zu viele Werte.* Eine freitextliche Interpretation hat 200 verschiedene
   Ausprägungen. 200 Farben sind keine Einfärbung, sondern Konfetti; ab einer
   Obergrenze wandert der Rest in eine gemeinsame Gruppe, und das steht so in
   der Legende.
3. *Zahlen sind keine Kategorien.* Bei "Anzahl der Funde" ist 3 näher an 4 als
   an 27. Zahlen werden deshalb der Größe nach sortiert und bekommen einen
   Verlauf statt zufälliger Farbtöne -- und wenn es zu viele sind, gleich breite
   Klassen ("12–18") statt einer Restgruppe.
"""

import colorsys
import re

try:
    from .linking import DisplayNames, normalize_key
except ImportError:  # pragma: no cover - als Einzelmodul geladen (tests/)
    # Wie in transfer.py des QGIS-Plugins: Die Prüfungen laden das Modul ohne
    # Paket-Kontext, damit sie ohne Blender laufen.
    from linking import DisplayNames, normalize_key


#: Womit mehrere Werte eines Knotens verbunden werden. Wie in der Werteübernahme
#: des QGIS-Plugins ein Semikolon -- Kommas kommen in Ansprachen selbst vor.
SEPARATOR = "; "

#: So viele Gruppen bekommen eine eigene Farbe. Darüber hinaus: bei Text eine
#: gemeinsame Restgruppe, bei Zahlen Klassen. Die Zahl ist keine technische
#: Grenze, sondern eine der Lesbarkeit -- mehr Farben lassen sich im Viewport
#: nicht mehr auseinanderhalten, und eine Legende, die scrollt, liest niemand.
MAX_GROUPS = 24

#: Beschriftung der Restgruppe.
OTHER_LABEL = "… other"

#: Sättigung und Helligkeit aller erzeugten Farben. Gleich zu halten ist der
#: Grund, warum die Einfärbung als *ein* Schema wirkt und nicht wie ein Unfall:
#: Unterschieden wird allein über den Farbton.
SATURATION = 0.62
BRIGHTNESS = 0.92

#: Der goldene Schnitt als Schrittweite im Farbkreis. Aufeinanderfolgende
#: Gruppen landen so möglichst weit auseinander -- gleichmäßig zu verteilen
#: wäre nur dann besser, wenn die Zahl der Gruppen vorher feststünde.
GOLDEN_RATIO = 0.6180339887

#: Farbe der Restgruppe: unbunt, damit sie sich nicht neben die echten stellt.
OTHER_COLOR = (0.55, 0.55, 0.55)

#: Von welchem Farbton der Zahlenverlauf ausgeht (0.62 ≈ Blau) und wo er endet
#: (0.0 = Rot). Wenig zu Viel, nicht Regenbogen: Ein Verlauf mit einer Richtung
#: lässt sich ohne Legende lesen.
RAMP_FROM = 0.62

_NUMBER = re.compile(r"^-?\d+(?:[.,]\d+)?$")

# Alles außer Buchstaben, Ziffern und Unterstrich wird zu einem Unterstrich --
# auch Leerzeichen. Blender nähme sie an, aber ein Materialname wie
# "GE_FUND_ material_Keramik_ Eisen" liest sich wie ein Versehen, und in einer
# Namensliste sortiert er sich an unerwarteter Stelle ein. Umlaute bleiben:
# \w ist in Python 3 unicode-bewusst, und "Verfüllung" soll "Verfüllung" heißen.
_MATERIAL_BAD = re.compile(r"\W+", re.UNICODE)

#: Blender kürzt Datenblocknamen ab dieser Länge.
MATERIAL_NAME_LEN = 63


# ---------------------------------------------------------------------------
# Werte
# ---------------------------------------------------------------------------


def merge_text(value):
    """Einen Wert von der Brücke auf einen Gruppennamen bringen.

    Listen kommen von mehrwertigen Quellen (die Funde einer SE), alles andere
    ist ein einzelner Wert. ``None`` heißt: Dieses Objekt gehört in keine
    Gruppe -- und bleibt deshalb, wie es war.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        seen = []
        for item in value:
            text = merge_text(item)
            if text is not None and text not in seen:
                seen.append(text)
        return SEPARATOR.join(seen) if seen else None

    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float) and value.is_integer():
        text = str(int(value))       # sonst hieße die Gruppe "3.0"
    else:
        text = value if isinstance(value, str) else str(value)
    text = text.strip()
    return text or None


def as_number(text):
    """``"12"`` -> 12.0, ``"12,5"`` -> 12.5, sonst ``None``."""
    s = str(text).strip()
    if not _NUMBER.match(s):
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def format_number(number):
    """Zahlen ohne das ``.0``, das Python an jeden float hängt."""
    if number == int(number):
        return str(int(number))
    return ("%.2f" % number).rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# Gruppen
# ---------------------------------------------------------------------------


class ColorGroup(object):
    """Ein Wert, seine Objekte, seine Farbe."""

    def __init__(self, value, objects=None, color=None):
        self.value = value
        self.objects = list(objects or [])
        #: (r, g, b) in 0..1 -- gesetzt von ``assign_colors``.
        self.color = color or (0.8, 0.8, 0.8)
        #: Die Restgruppe bringt ihre Farbe mit und behält sie.
        self.fixed = color is not None
        #: Name des benutzten Materials -- trägt ``shading.apply_plan`` nach.
        self.material = ""

    @property
    def count(self):
        return len(self.objects)

    def __repr__(self):
        return "ColorGroup(%r, %d objects)" % (self.value, self.count)


class ColorPlan(object):
    """Was eingefärbt würde -- und was nicht, und warum nicht.

    Der zweite Teil zählt wieder mehr: Eine Einfärbung, die 40 von 150 Objekten
    trifft, sieht im Viewport aus wie eine vollständige. Nur der Rest bleibt in
    seiner alten Farbe, und die sagt nichts.
    """

    def __init__(self):
        self.groups = []
        #: Objekte, zu denen es keinen Knoten gibt.
        self.without_node = []
        #: Objekte mit Knoten, aber ohne Wert an dieser Quelle.
        self.without_value = []
        #: Knotenzeilen ohne brauchbaren Schlüssel.
        self.skipped_rows = 0
        #: Wie viele verschiedene Werte es vor dem Zusammenfassen gab.
        self.distinct_values = 0
        #: Wurden Zahlen zu Klassen zusammengefasst?
        self.bucketed = False

    @property
    def colored(self):
        return sum(group.count for group in self.groups)

    def summary(self):
        if not self.groups:
            return "No object gets a colour."
        parts = ["%d objects in %d groups" % (self.colored, len(self.groups))]
        if self.without_value:
            parts.append("%d without a value" % len(self.without_value))
        if self.without_node:
            parts.append("%d without a node" % len(self.without_node))
        return " · ".join(parts)

    def details(self):
        lines = [self.summary(), ""]
        for group in self.groups:
            lines.append("%-28s %4d" % (group.value[:28], group.count))
        if self.distinct_values > len(self.groups):
            lines += ["", "%d distinct values, %d groups%s."
                      % (self.distinct_values, len(self.groups),
                         " (combined into ranges)" if self.bucketed
                         else " (the rest in \"%s\")" % OTHER_LABEL)]
        if self.skipped_rows:
            lines.append("%d nodes without a usable key value skipped." % self.skipped_rows)
        return "\n".join(lines)


def build_plan(index, rows, max_groups=MAX_GROUPS):
    """Aus Objektindex und Graph-Zeilen die Gruppen rechnen.

    ``index`` ist ein aufgebauter ``LinkIndex`` (Objektseite gesetzt), ``rows``
    ist die Antwort von ``collectValues`` für **eine** Quelle:
    ``[{"id": …, "key": rohwert, "values": [wert]}]``.

    Normalisiert wird nur die Graph-Seite -- die Objektseite hat der Index beim
    Aufbau schon normalisiert. Beide durchlaufen dieselbe Stufe, sonst träfe
    "T2_SE2004" sein eigenes Mesh nicht.
    """
    config = index.config
    plan = ColorPlan()

    # Erst je Schlüssel sammeln: Zwei Knoten mit derselben Bezeichnung sollen
    # eine Gruppe ergeben und nicht zwei, von denen die zweite die erste
    # überschreibt -- wer gewönne, hinge sonst an der Reihenfolge im Graphen.
    by_key = {}
    keys_with_node = set()
    for row in rows or []:
        key = normalize_key(row.get("key"), config.normalize, config.pattern)
        if key is None:
            plan.skipped_rows += 1
            continue
        keys_with_node.add(key)
        values = row.get("values") or []
        text = merge_text(values[0] if values else None)
        if text is None:
            continue
        previous = by_key.get(key)
        by_key[key] = text if previous is None else merge_text([previous, text])

    named = {}          # Wert -> [Objektnamen]
    covered = set()
    for key in sorted(by_key):
        names = index.feature_ids_for_normalized_keys([key])
        if not names:
            continue
        covered.update(names)
        named.setdefault(by_key[key], []).extend(names)

    # Zwei Sorten von "bleibt, wie es war", und der Unterschied ist der ganze
    # Ertrag der Diagnose: Ein Objekt *mit* Knoten, aber ohne Wert bedeutet,
    # dass die Verknüpfung greift und die Property fehlt -- da sucht man den
    # Fehler sonst an der falschen Stelle.
    for name in index.feature_ids():
        if name in covered:
            continue
        keys = index.keys_for_feature_ids([name])
        if keys and keys[0] in keys_with_node:
            plan.without_value.append(name)
        else:
            plan.without_node.append(name)

    plan.distinct_values = len(named)
    plan.groups, ordered = _to_groups(named, max_groups, plan)
    assign_colors(plan.groups, ordered)
    return plan


def _to_groups(named, max_groups, plan):
    """Aus ``{wert: [namen]}`` eine geordnete, gekappte Gruppenliste.

    Gibt ``(gruppen, geordnet)`` zurück; ``geordnet`` heißt "die Reihenfolge
    trägt eine Bedeutung" und entscheidet über Verlauf gegen Farbkreis.
    """
    if not named:
        return [], False

    numbers = {value: as_number(value) for value in named}
    if all(number is not None for number in numbers.values()):
        if len(named) > max_groups:
            plan.bucketed = True
            return _bucket(named, numbers, max_groups), True
        # Der Größe nach: Bei "Anzahl der Funde" ist 3 näher an 4 als an 27.
        return [ColorGroup(value, named[value])
                for value in sorted(named, key=lambda v: numbers[v])], True

    # Text: das Häufigste zuerst. Wer die Legende von oben liest, sieht dann als
    # Erstes, was die Szene prägt.
    ordered = sorted(named, key=lambda v: (-len(named[v]), v))
    if len(ordered) <= max_groups:
        return [ColorGroup(value, named[value]) for value in ordered], False

    groups = [ColorGroup(value, named[value]) for value in ordered[:max_groups - 1]]
    rest = []
    for value in ordered[max_groups - 1:]:
        rest.extend(named[value])
    groups.append(ColorGroup("%s (%d values)" % (OTHER_LABEL, len(ordered) - max_groups + 1),
                             rest, OTHER_COLOR))
    return groups, False


def _bucket(named, numbers, max_groups):
    """Zahlen in gleich breite Klassen. ``"12–18"`` statt einer Restgruppe.

    Eine Restgruppe wäre hier die falsche Antwort: Sie fasst nach *Häufigkeit*
    zusammen, und damit landen 3 und 27 nebeneinander, nur weil beide selten
    sind. Bei Zahlen ist die Nachbarschaft aber der Wert selbst.
    """
    low = min(numbers.values())
    high = max(numbers.values())
    span = high - low
    if span <= 0:
        return [ColorGroup(value, named[value]) for value in named]

    width = span / float(max_groups)
    buckets = {}
    for value, number in numbers.items():
        # Der oberste Wert fiele sonst in eine eigene Klasse hinter dem Rand.
        slot = min(int((number - low) / width), max_groups - 1)
        buckets.setdefault(slot, []).extend(named[value])

    groups = []
    for slot in sorted(buckets):
        start = low + slot * width
        end = low + (slot + 1) * width
        label = "%s–%s" % (format_number(start), format_number(end))
        groups.append(ColorGroup(label, buckets[slot]))
    return groups


# ---------------------------------------------------------------------------
# Farben
# ---------------------------------------------------------------------------


def assign_colors(groups, ordered=False):
    """Jeder Gruppe ihre Farbe -- Verlauf bei Zahlen, Farbkreis bei Text.

    Gruppen, die ihre Farbe mitbringen (die Restgruppe), behalten sie.
    """
    open_groups = [group for group in groups if not group.fixed]
    total = len(open_groups)
    for i, group in enumerate(open_groups):
        if ordered:
            group.color = ramp_color(i / float(total - 1) if total > 1 else 0.0)
        else:
            group.color = category_color(i)
    return groups


def category_color(index):
    """Farbe Nummer ``index`` im Farbkreis."""
    hue = (index * GOLDEN_RATIO) % 1.0
    return colorsys.hsv_to_rgb(hue, SATURATION, BRIGHTNESS)


def ramp_color(position):
    """``0.0`` -> Blau, ``1.0`` -> Rot."""
    position = min(max(position, 0.0), 1.0)
    return colorsys.hsv_to_rgb(RAMP_FROM * (1.0 - position), SATURATION, BRIGHTNESS)


# ---------------------------------------------------------------------------
# Namen
# ---------------------------------------------------------------------------


def source_text(source, names=None):
    """Menschenlesbare Beschreibung einer Wertquelle -- wie im QGIS-Plugin.

    Wird hier gebaut und nicht von der Brücke übernommen: Die Namen kommen aus
    ``names`` (``linking.DisplayNames``) und lauten damit wie im GraphExplorer
    -- "is composed of" statt der ganzen URI --, und die Liste spricht dieselbe
    Sprache wie der Rest des Addons.
    """
    names = names or DisplayNames()
    source = source or {}
    kind = source.get("kind")
    if kind == "id":
        return "Node ID"
    if kind == "label":
        return "Node label"
    if kind == "attr":
        return names.attr(source.get("attr")) or "Property"
    if kind == "rel":
        arrow = "→ " if source.get("dir") != "in" else "← "
        what = source.get("what")
        tail = {"count": "count", "label": "labels"}.get(
            what, names.attr(source.get("attr")) or "")
        return "%s%s: %s" % (arrow, names.rel(source.get("rel")) or "?", tail)
    return "unknown source"


def source_stem(source, names=None):
    """Der Teil des Materialnamens, der die Quelle nennt.

    Kürzer als ``source_text``: Pfeile und Doppelpunkte sähen im
    Materialbrowser wie ein Versehen aus. Gebaut wie die Spaltennamen der
    Werteübernahme in QGIS -- ``has_interpretation`` für die Beschriftungen
    einer Beziehung, ``contains_find_count`` für ihre Anzahl.
    """
    names = names or DisplayNames()
    source = source or {}
    kind = source.get("kind")
    if kind == "id":
        return "node_id"
    if kind == "label":
        return "label"
    if kind == "attr":
        return names.attr(source.get("attr")) or "value"
    if kind == "rel":
        rel = names.rel(source.get("rel")) or "rel"
        what = source.get("what")
        if what == "count":
            return rel + "_count"
        if what == "label":
            return rel
        return rel + "_" + (names.attr(source.get("attr")) or "value")
    return "value"


#: Vorsilbe aller erzeugten Materialien. Sie ist mehr als Kosmetik: Daran
#: erkennt das Aufheben, was von hier stammt, und daran erkennt der Benutzer im
#: Materialbrowser, was er löschen darf.
MATERIAL_PREFIX = "GE_"


def material_name(source, value, taken=()):
    """``("interpretation", "Grube")`` -> ``"GE_interpretation_Grube"``.

    Der Wert steht mit im Namen, nicht nur eine laufende Nummer: Im
    Materialbrowser von Blender sieht man sonst zwanzig ``GE_1`` bis ``GE_20``
    und muss jedes anklicken, um zu wissen, was es bedeutet.
    """
    stem = "%s%s_%s" % (MATERIAL_PREFIX, _clean(source), _clean(value))
    stem = stem[:MATERIAL_NAME_LEN].rstrip("_ ") or MATERIAL_PREFIX.rstrip("_")

    lowered = set(str(t).lower() for t in (taken or ()))
    if stem.lower() not in lowered:
        return stem
    for n in range(2, 1000):
        suffix = "_%d" % n
        candidate = stem[:MATERIAL_NAME_LEN - len(suffix)].rstrip("_ ") + suffix
        if candidate.lower() not in lowered:
            return candidate
    return stem


def _clean(text):
    return _MATERIAL_BAD.sub("_", str(text or "").strip()).strip("_ ") or "value"
