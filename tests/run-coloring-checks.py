"""Prüft coloring.py -- das Einfärben nach einem Wert aus dem Graphen.

Läuft mit reinem Python, ohne Blender:

    python tests/run-coloring-checks.py

Geprüft wird, was an echten Beständen sofort auftritt und wovon man im
Viewport nichts sieht: mehrere Werte an einem Knoten, mehrere Knoten an einem
Objektnamen, zu viele Ausprägungen für eine lesbare Legende, und der
Unterschied zwischen "kein Knoten" und "Knoten ohne diesen Wert". Eine
Einfärbung, die 40 von 150 Objekten trifft, sieht aus wie eine vollständige --
nur die Zahlen daneben sagen, dass sie es nicht ist.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from coloring import (  # noqa: E402
    MAX_GROUPS,
    OTHER_COLOR,
    OTHER_LABEL,
    SEPARATOR,
    as_number,
    build_plan,
    category_color,
    format_number,
    material_name,
    merge_text,
    ramp_color,
)
from linking import (  # noqa: E402
    NORMALIZE_LAST_NUMBER,
    NORMALIZE_LOOSE,
    SOURCE_LABEL,
    LinkConfig,
    LinkIndex,
)

failures = []


def check(name, got, want):
    if got != want:
        failures.append("%s\n    erwartet: %r\n    bekommen: %r" % (name, want, got))


def index_for(pairs, normalize=NORMALIZE_LOOSE):
    index = LinkIndex(LinkConfig(node_type="Befund", node_source=SOURCE_LABEL,
                                 normalize=normalize))
    index.set_layer_side(pairs)
    return index


def row(key, value):
    return {"id": "N_" + str(key), "key": key, "values": [value]}


# -- Werte zu Gruppennamen ---------------------------------------------------

check("Text bleibt Text", merge_text("Grube"), "Grube")
check("Leerraum außen weg", merge_text("  Grube "), "Grube")
check("nichts bleibt nichts", merge_text(None), None)
check("leer ist kein Wert", merge_text("   "), None)

# Mehrwertige Quellen ("das Material der Funde dieser SE") sind der Normalfall,
# nicht die Ausnahme. Die Verbindung ist die Gruppe.
check("mehrere Werte werden eine Gruppe",
      merge_text(["Keramik", "Eisen"]), "Keramik" + SEPARATOR + "Eisen")
check("Doppeltes fällt weg", merge_text(["Keramik", "Eisen", "Keramik"]),
      "Keramik" + SEPARATOR + "Eisen")

# Aus JSON kommt eine Anzahl als 3 oder 3.0 zurück. Die Gruppe darf nicht
# "3.0" heißen -- sie stünde so in der Legende und im Materialnamen.
check("Zahl ohne .0", merge_text(3.0), "3")
check("Kommazahl bleibt", merge_text(2.5), "2.5")

check("Zahl erkannt", as_number("12"), 12.0)
check("Kommazahl deutsch", as_number("12,5"), 12.5)
check("Text ist keine Zahl", as_number("12a"), None)
check("Bereichsbeschriftung ist keine Zahl", as_number("12–18"), None)
check("Zahl formatiert", format_number(12.0), "12")


# -- Gruppen ------------------------------------------------------------------

index = index_for([
    ("SE_2001", "SE 2001"),
    ("SE_2001_Teil2", "se  2001"),   # zweites Mesh derselben Einheit
    ("SE_2002", "SE 2002"),
    ("SE_2003", "SE 2003"),
    ("Kamera", None),                # ohne Bezeichnung -- steht gar nicht im Index
    ("SE_9999", "SE 9999"),          # ohne Knoten
])
plan = build_plan(index, [
    row("SE 2001", "Grube"),
    row("SE 2002", "Grube"),
    row("SE 2003", "Mauer"),
    row("SE 2004", "Grube"),         # Knoten ohne Mesh
    row("SE 3000", None),            # Knoten ohne Wert -- hat aber ein Mesh? nein
])

check("zwei Gruppen", [g.value for g in plan.groups], ["Grube", "Mauer"])
# Beide Meshes derselben SE landen in derselben Gruppe -- das ist der Zweck des
# n:m-Index, und in Blender der Normalfall (eine SE, zwei Teilobjekte).
check("Objekte je Gruppe", [sorted(g.objects) for g in plan.groups],
      [["SE_2001", "SE_2001_Teil2", "SE_2002"], ["SE_2003"]])
check("das Häufigste zuerst", plan.groups[0].value, "Grube")
check("eingefärbt", plan.colored, 4)
check("ohne Knoten", plan.without_node, ["SE_9999"])
check("ohne Wert", plan.without_value, [])
check("Objekt ohne Bezeichnung taucht nirgends auf",
      "Kamera" in plan.without_node + plan.without_value, False)

# Der aufschlussreiche Unterschied: Das Mesh hat einen Knoten, der Knoten aber
# keinen Wert an dieser Quelle. Wer das nicht auseinanderhält, sucht den Fehler
# bei der Verknüpfung, obwohl sie greift.
mit_leerem = build_plan(index_for([("SE_2001", "SE 2001"), ("SE_2002", "SE 2002")]),
                        [row("SE 2001", "Grube"), row("SE 2002", None)])
check("Knoten ohne Wert ist etwas anderes als kein Knoten",
      (mit_leerem.without_value, mit_leerem.without_node), (["SE_2002"], []))


# -- Mehrere Knoten auf einer Bezeichnung ------------------------------------

zwei = build_plan(index_for([("SE_2001", "SE 2001")]),
                  [row("SE 2001", "Grube"), {"id": "N_b", "key": "se 2001",
                                             "values": ["Mauer"]}])
check("Werte beider Knoten kommen zusammen",
      [g.value for g in zwei.groups], ["Grube" + SEPARATOR + "Mauer"])


# -- Zu viele Ausprägungen ----------------------------------------------------
#
# Eine freitextliche Interpretation hat schnell hundert Ausprägungen. Hundert
# Farben sind keine Einfärbung.

pairs = [("OBJ_%d" % i, "SE %d" % i) for i in range(200)]
rows = [row("SE %d" % i, "Ansprache %d" % i) for i in range(200)]
# Die erste Ansprache bekommt mehr Objekte, damit sie sicher oben steht.
pairs += [("OBJ_extra_%d" % i, "SE 0") for i in range(5)]
viele = build_plan(index_for(pairs), rows)

check("gekappt auf die Obergrenze", len(viele.groups), MAX_GROUPS)
check("die häufigste Ansprache steht oben", viele.groups[0].value, "Ansprache 0")
check("der Rest steht in einer Gruppe", viele.groups[-1].value.startswith(OTHER_LABEL), True)
check("und behält seine unbunte Farbe", viele.groups[-1].color, OTHER_COLOR)
check("kein Objekt geht verloren", viele.colored, 205)
check("die Zahl der Werte steht im Bericht", viele.distinct_values, 200)


# -- Zahlen -------------------------------------------------------------------
#
# Bei "Anzahl der Funde" ist 3 näher an 4 als an 27. Zufällige Farbtöne nach
# Häufigkeit wären hier die falsche Antwort.

zahlen = build_plan(
    index_for([("A", "SE 1"), ("B", "SE 2"), ("C", "SE 3")]),
    [row("SE 1", 12), row("SE 2", 3), row("SE 3", 7)],
)
check("Zahlen stehen der Größe nach", [g.value for g in zahlen.groups], ["3", "7", "12"])
# Verlauf statt Farbkreis: die kleinste Zahl blau, die größte rot.
check("kleinste Zahl ist blau", zahlen.groups[0].color, ramp_color(0.0))
check("größte Zahl ist rot", zahlen.groups[-1].color, ramp_color(1.0))
check("Text bekommt dagegen Farbkreis", plan.groups[0].color, category_color(0))

# Zu viele Zahlen: Klassen statt Restgruppe. Eine Restgruppe fasst nach
# Häufigkeit zusammen -- dann lägen 3 und 27 nebeneinander, nur weil beide
# selten sind.
weite = build_plan(
    index_for([("OBJ_%d" % i, "SE %d" % i) for i in range(100)]),
    [row("SE %d" % i, i) for i in range(100)],
)
check("Zahlen werden zu Klassen", weite.bucketed, True)
check("höchstens so viele Klassen wie erlaubt", len(weite.groups) <= MAX_GROUPS, True)
check("Klassen sind Bereiche", "–" in weite.groups[0].value, True)
check("kein Objekt geht verloren", weite.colored, 100)
check("auch der höchste Wert ist dabei",
      sum(1 for g in weite.groups for o in g.objects if o == "OBJ_99"), 1)


# -- Farben -------------------------------------------------------------------

check("Farben sind verschieden",
      len({category_color(i) for i in range(12)}), 12)
check("Farben liegen im gültigen Bereich",
      all(0.0 <= c <= 1.0 for i in range(12) for c in category_color(i)), True)


# -- Materialnamen ------------------------------------------------------------

check("Name aus Quelle und Wert",
      material_name("interpretation", "Grube"), "GE_interpretation_Grube")
check("Sonderzeichen zusammengefasst",
      material_name("→ FUND: material", "Keramik; Eisen"), "GE_FUND_material_Keramik_Eisen")
check("belegter Name weicht aus",
      material_name("i", "Grube", taken=["GE_i_Grube"]), "GE_i_Grube_2")

lang = material_name("interpretation", "x" * 200)
check("Blenders Namenslänge wird eingehalten", len(lang) <= 63, True)
# Zwei lange Werte dürfen nach dem Kürzen nicht denselben Namen tragen --
# sonst zeigten zwei Gruppen auf dasselbe Material und damit auf dieselbe Farbe.
lang2 = material_name("interpretation", "x" * 200, taken=[lang])
check("und Gekürztes bleibt unterscheidbar", lang2 != lang, True)
check("auch das Ausweichen bleibt in der Grenze", len(lang2) <= 63, True)


# -- Der Muster-Fall ----------------------------------------------------------
#
# Dieselbe Falle wie überall: Bei "Letzte Zahl" ist die Normalisierung nicht
# wiederholbar. Die Objektseite ist beim Aufbau des Index schon normalisiert,
# nur die Graph-Seite darf hier noch durch.
t2 = build_plan(
    index_for([("T2_SE2004", "T2_SE2004")], NORMALIZE_LAST_NUMBER),
    [row("SE 2004", "Grube")],
)
check("Letzte-Zahl findet über beide Seiten zusammen",
      [g.objects for g in t2.groups], [["T2_SE2004"]])


# -- Ergebnis -----------------------------------------------------------------

if failures:
    print("\n%d Prüfung(en) fehlgeschlagen:\n" % len(failures))
    for f in failures:
        print("  - %s\n" % f)
    sys.exit(1)
print("coloring.py: alle Prüfungen bestanden.")
