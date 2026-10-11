"""Prüft linking.py -- die Zuordnung zwischen Layer-Features und Graph-Knoten.

Läuft mit reinem Python, ohne QGIS:

    python tests/run-linking-checks.py

Das ist der Grund, warum linking.py keine QGIS-Importe hat. Der Teil, an dem
sich echte Daten reiben, soll sich prüfen lassen, ohne QGIS zu starten -- und
ohne einen Layer zu brauchen, den es auf dem Rechner der nächsten Person nicht
gibt. Dieselbe Trennung wie in src/lib/ des GraphExplorers.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linking import (  # noqa: E402
    DEFAULT_PATTERN,
    NORMALIZE_DIGITS,
    NORMALIZE_EXACT,
    NORMALIZE_LAST_NUMBER,
    NORMALIZE_LOOSE,
    NORMALIZE_PATTERN,
    NORMALIZE_TRIM,
    SOURCE_ATTR,
    SOURCE_LABEL,
    DisplayNames,
    LinkConfig,
    LinkIndex,
    normalize_key,
    short_name,
)

failures = []


def check(name, got, want):
    if got != want:
        failures.append("%s\n    erwartet: %r\n    bekommen: %r" % (name, want, got))


def config(normalize=NORMALIZE_LOOSE):
    return LinkConfig(
        layer_id="L1", layer_field="SE", node_type="Befund",
        node_source=SOURCE_ATTR, node_attr="se", normalize=normalize,
    )


# -- Normalisierung ----------------------------------------------------------

check("loose: Leerraum und Schreibung egal",
      normalize_key("  SE  2001 ", NORMALIZE_LOOSE), "se 2001")
check("loose: identisch nach Normalisierung",
      normalize_key("se 2001", NORMALIZE_LOOSE), normalize_key("SE   2001", NORMALIZE_LOOSE))
check("trim: nur außen", normalize_key("  SE 2001  ", NORMALIZE_TRIM), "SE 2001")
check("exact: unverändert", normalize_key(" SE 2001 ", NORMALIZE_EXACT), " SE 2001 ")
check("digits: Zahl herauslösen", normalize_key("SE 2001", NORMALIZE_DIGITS), "2001")
check("digits: reine Zahl", normalize_key(2001, NORMALIZE_DIGITS), "2001")

# Mehrdeutigkeit darf nicht stillschweigend aufgelöst werden: "Schnitt 3, SE 2001"
# enthält zwei Zahlen, und zu raten hieße, Funde am falschen Befund zu zeigen.
check("digits: zwei Zahlen sind kein Schlüssel",
      normalize_key("Schnitt 3, SE 2001", NORMALIZE_DIGITS), None)
check("digits: gar keine Zahl", normalize_key("ohne", NORMALIZE_DIGITS), None)

check("None bleibt None", normalize_key(None), None)
check("leer ist kein Schlüssel", normalize_key("   "), None)
check("NULL ist kein Schlüssel", normalize_key("NULL"), None)
check("Zahl wird zu Text", normalize_key(2001), "2001")


# -- Zuordnung ---------------------------------------------------------------

index = LinkIndex(config())
index.set_layer_side([(1, "SE 2001"), (2, "SE 2002"), (3, "se  2001"), (4, None), (5, "SE 2999")])
index.set_graph_side([
    {"id": "B_1", "key": "SE 2001"},
    {"id": "B_2", "key": "SE 2002"},
    {"id": "B_3", "key": "SE 3000"},
    {"id": "B_4", "key": ""},
])

check("Feature -> Schlüssel", index.keys_for_feature_ids([1]), ["se 2001"])

# n:m ist der Normalfall, nicht die Ausnahme: eine SE, die in zwei Schnitten
# dokumentiert wurde, hat zwei Polygone.
check("Schlüssel -> mehrere Features", index.feature_ids_for_keys(["SE 2001"]), [1, 3])
check("Schlüssel -> Knoten", index.node_ids_for_keys(["se 2001"]), ["B_1"])
check("Rohwert wird beim Nachschlagen normalisiert",
      index.feature_ids_for_keys(["  se   2001  "]), [1, 3])
check("unbekannter Schlüssel", index.feature_ids_for_keys(["SE 9999"]), [])
check("Feature ohne Feldwert taucht nicht auf", index.keys_for_feature_ids([4]), [])

report = index.report()
check("verknüpfte Schlüssel", report.matched_keys, 2)
check("nur im Layer", report.only_in_layer, ["se 2999"])
check("nur im Graphen", report.only_in_graph, ["se 3000"])
check("übergangene Features", report.layer_skipped, 1)
check("übergangene Knoten", report.graph_skipped, 1)
check("mehrdeutig auf Layer-Seite", report.ambiguous_layer, ["se 2001"])
check("eindeutig auf Graph-Seite", report.ambiguous_graph, [])
check("Bericht meldet Erfolg", report.ok, True)


# -- Ein Abgleich, der komplett danebengeht ----------------------------------

miss = LinkIndex(config())
miss.set_layer_side([(1, "2001"), (2, "2002")])
miss.set_graph_side([{"id": "B_1", "key": "SE 2001"}])
miss_report = miss.report()
check("kein Treffer erkannt", miss_report.ok, False)
check("Kurzbericht sagt es deutlich",
      "No match" in miss_report.summary(), True)

# ... und genau dafür ist der Ziffern-Modus da.
rescued = LinkIndex(config(NORMALIZE_DIGITS))
rescued.set_layer_side([(1, "2001"), (2, "2002")])
rescued.set_graph_side([{"id": "B_1", "key": "SE 2001"}])
check("Ziffern-Modus rettet den Abgleich", rescued.feature_ids_for_keys(["SE 2001"]), [1])


# -- Konfiguration -----------------------------------------------------------

check("unvollständig ohne Attribut",
      LinkConfig(layer_id="L", layer_field="SE", node_type="Befund",
                 node_source=SOURCE_ATTR, node_attr="").is_complete(), False)
check("vollständig", config().is_complete(), True)
check("Rundreise durch dict", LinkConfig.from_dict(config().to_dict()).to_dict(),
      config().to_dict())


# ===========================================================================
# Bezeichnungen mit Schnitt: "T2_SE2004"
# ===========================================================================
#
# Alles ab hier bildet einen typischen Bestand nach: Die Bezeichnungen
# tragen den Schnitt vorneweg. Das bricht die einfachen Stufen -- deshalb
# "Letzte Zahl" und "Muster".

BEZEICHNUNG = "T2_SE2004"
GRAPH = "SE 2004"          # so steht es (vermutlich) im Attribut des Knotens


# -- "Letzte Zahl" -----------------------------------------------------------

check("Schnitt davor stört nicht", normalize_key(BEZEICHNUNG, NORMALIZE_LAST_NUMBER), "2004")
check("Graph-Seite ergibt dasselbe", normalize_key(GRAPH, NORMALIZE_LAST_NUMBER), "2004")
check("ohne Zahl kein Schlüssel", normalize_key("Nordprofil", NORMALIZE_LAST_NUMBER), None)

# Zum Vergleich: Warum "Nur Ziffern" hier nicht taugt. Zwei Zahlengruppen, und
# die Stufe verweigert zu Recht die Auskunft, statt 2 oder 2004 zu raten.
check("Nur Ziffern kapituliert bei zwei Zahlen",
      normalize_key(BEZEICHNUNG, NORMALIZE_DIGITS), None)


# -- Muster ------------------------------------------------------------------

check("Vorgabemuster, Layer-Seite", normalize_key(BEZEICHNUNG, NORMALIZE_PATTERN), "2004")
check("Vorgabemuster, Graph-Seite", normalize_key(GRAPH, NORMALIZE_PATTERN), "2004")
for schreibweise in ("SE2004", "SE 2004", "SE-2004", "se_2004", "T2_SE2004"):
    check("Schreibweise %r" % schreibweise,
          normalize_key(schreibweise, NORMALIZE_PATTERN, DEFAULT_PATTERN), "2004")

check("kein Treffer, kein Schlüssel",
      normalize_key("Nordprofil", NORMALIZE_PATTERN, DEFAULT_PATTERN), None)
check("eigenes Muster", normalize_key("Fund-17-b", NORMALIZE_PATTERN, r"Fund-(\d+)"), "17")
check("ohne Klammergruppe zählt der ganze Treffer",
      normalize_key("T2_SE2004", NORMALIZE_PATTERN, r"SE\d+"), "se2004")

# Ein halb getippter Ausdruck im Eingabefeld darf nicht die Zuordnung sprengen.
check("kaputtes Muster wirft nicht",
      normalize_key(BEZEICHNUNG, NORMALIZE_PATTERN, r"SE(\d+"), None)


# -- Und beide Seiten zusammen -----------------------------------------------
#
# Der eigentliche Beweis: Feldwerte im Layer und Attributwerte aus dem
# Graphen finden zusammen, ohne dass irgendwo eine Umrechnungstabelle steht.

for modus in (NORMALIZE_LAST_NUMBER, NORMALIZE_PATTERN):
    config = LinkConfig(node_type="Befund", node_source=SOURCE_ATTR,
                        node_attr="se", normalize=modus)
    index = LinkIndex(config)
    index.set_layer_side([
        (1, "T2_SE2004"),
        (2, "T2_SE2004"),       # zweite Geometrie derselben SE
        (3, "T2_SE2005"),
        (4, "T2_Nordprofil"),   # gar keine SE
    ])
    index.set_graph_side([
        {"id": "B_2004", "key": "SE 2004"},
        {"id": "B_2005", "key": "SE 2005"},
        {"id": "B_2099", "key": "SE 2099"},
    ])

    check("%s: SE 2004 findet beide Objekte" % modus,
          index.feature_ids_for_keys(["SE 2004"]), [1, 2])
    check("%s: Objektname findet den Knoten" % modus,
          index.node_ids_for_keys(["T2_SE2005"]), ["B_2005"])

    report = index.report()
    check("%s: zwei Schlüssel verknüpft" % modus, report.matched_keys, 2)
    check("%s: ein Knoten ohne Geometrie" % modus, len(report.only_in_graph), 1)
    check("%s: doppelte Geometrie erkannt" % modus, len(report.ambiguous_layer), 1)

# "Nordprofil" hat keinen Schlüssel und darf deshalb gar nicht erst mitzählen --
# ein Objekt ohne SE-Bezug ist kein Fehlabgleich, sondern schlicht unbeteiligt.
check("Objekt ohne SE-Bezug bleibt draußen", index.keys_for_feature_ids([4]), [])


# ===========================================================================
# Der Normalfall: beide Seiten gleich benannt
# ===========================================================================
#
# So sieht es in der Praxis aus: Ein Mesh in Blender heißt exakt so, wie die
# Einheit im Graphen heißt -- für SE, Funde, Profile gleichermaßen. Dann
# braucht es kein Muster und keine Zahlenzerlegung, sondern nur einen
# Textvergleich. Das ist zugleich der robusteste Fall, weil er nicht davon
# abhängt, welchem Schema die Bezeichnungen folgen.

label_config = LinkConfig(node_type="Befund", node_source=SOURCE_LABEL,
                          normalize=NORMALIZE_LOOSE)
label_index = LinkIndex(label_config)
label_index.set_layer_side([
    ("Würfel.001", "T2_SE2004"),
    ("Würfel.002", "T2_SE2004"),   # zweite Geometrie
    ("Würfel.003", "T2_SE2005"),
])
label_index.set_graph_side([
    {"id": "B_2004", "key": "T2_SE2004"},     # Label des Knotens, wörtlich
    {"id": "B_2005", "key": "T2_SE2005"},
    {"id": "B_2006", "key": "T2_SE2006"},
])

check("Objektname trifft Label", label_index.node_ids_for_keys(["T2_SE2004"]), ["B_2004"])
check("beide Geometrien derselben SE",
      label_index.feature_ids_for_keys(["T2_SE2004"]), ["Würfel.001", "Würfel.002"])

label_report = label_index.report()
check("zwei Bezeichnungen verknüpft", label_report.matched_keys, 2)
check("eine ohne Geometrie", label_report.only_in_graph, ["t2_se2006"])

# Der Textvergleich ist gleichgültig gegen die Art der Bezeichnung -- genau das
# macht ihn dem Zahlenweg überlegen, sobald beide Seiten dasselbe schreiben.
for bezeichnung in ("T2_SE2004", "Fund-2022-17", "Nordprofil West", "Grab 3a"):
    idx = LinkIndex(LinkConfig(node_source=SOURCE_LABEL, normalize=NORMALIZE_LOOSE))
    idx.set_layer_side([("obj", bezeichnung)])
    idx.set_graph_side([{"id": "N", "key": bezeichnung}])
    check("Textvergleich trägt %r" % bezeichnung, idx.report().matched_keys, 1)


# -- Der Fehlgriff, der in Blender passiert ist -------------------------------
#
# Als Schlüsselquelle war "Attribut" fest verdrahtet, und im Feld stand "SE".
# Ein Attribut dieses Namens gibt es aber nicht -- die Graph-Seite blieb leer,
# und der Bericht sagte nur "keine Übereinstimmung", ohne zu zeigen, woran es
# lag. Beides ist jetzt geprüft: die Erkennung und die Auskunft darüber.

wrong = LinkIndex(LinkConfig(node_type="Befund", node_source=SOURCE_ATTR, node_attr="SE"))
wrong.set_layer_side([("obj", "T2_SE2004")])
wrong.set_graph_side([])          # so kommt es von der JS-Seite, wenn es das Attribut nicht gibt
wrong_report = wrong.report()

check("kein Treffer", wrong_report.matched_keys, 0)
check("Graph-Seite als leer erkannt", wrong_report.graph_keys, 0)
check("Objekt-Seite ist da", wrong_report.layer_keys, 1)
check("Bericht nennt die Ursache",
      "key source" in wrong_report.details(), True)
check("und verweist auf Label", "Node label" in wrong_report.details(), True)

# Beispiele von beiden Seiten -- das Mittel, mit dem sich so etwas auf einen
# Blick erkennen lässt, statt aus Zahlen erschlossen zu werden.
check("Beispiele der Objektseite", wrong_report.layer_samples, ["t2_se2004"])
check("Beispiele der Graphseite leer", wrong_report.graph_samples, [])
check("im Normalfall beide gefüllt",
      (len(label_report.layer_samples) > 0, len(label_report.graph_samples) > 0), (True, True))


# ===========================================================================
# Der Rückweg: schon normalisierte Schlüssel dürfen nicht erneut durch
# ===========================================================================
#
# Der Weg vom Wirt zum Explorer geht über keys_for_feature_ids -- und das
# liefert bereits normalisierte Schlüssel. Sie noch einmal durch normalize_key
# zu schicken, fiel bei den meisten Stufen nicht auf, weil sie idempotent sind.
# Bei "Muster" nicht: Aus "T2_SE2004" wird "2004", und in "2004" findet
# SE[\s_-]*(\d+) nichts mehr. Die Richtung Wirt -> Explorer war damit still
# tot, während die Gegenrichtung tadellos lief -- ein Fehlerbild, das nach
# einem Zufall aussieht.

# NORMALIZE_DIGITS fehlt in der Liste mit Absicht: Diese Stufe verweigert bei
# "T2_SE2004" die Auskunft (zwei Zahlengruppen), und zwar richtigerweise --
# geprüft weiter oben. Sie hier mitzuführen würde nicht den Rückweg prüfen,
# sondern nur diese Verweigerung noch einmal.
for modus in (NORMALIZE_LOOSE, NORMALIZE_EXACT, NORMALIZE_TRIM,
              NORMALIZE_LAST_NUMBER, NORMALIZE_PATTERN):
    idx = LinkIndex(LinkConfig(node_type="Befund", node_source=SOURCE_LABEL, normalize=modus))
    idx.set_layer_side([("Objekt", "T2_SE2004")])
    idx.set_graph_side([{"id": "B_2004", "key": "T2_SE2004"}])

    # Hinweg: Explorer -> Wirt. Rohe Schlüssel, einmal normalisiert.
    check("%s: Hinweg findet das Objekt" % modus,
          idx.feature_ids_for_keys(["T2_SE2004"]), ["Objekt"])

    # Rückweg: Wirt -> Explorer. Erst Feature -> Schlüssel, dann Schlüssel ->
    # Knoten. Der zweite Schritt bekommt bereits Normalisiertes.
    zwischen = idx.keys_for_feature_ids(["Objekt"])
    check("%s: Rückweg findet den Knoten" % modus,
          idx.node_ids_for_normalized_keys(zwischen), ["B_2004"])

# Und der Beleg, dass es der alte Weg *nicht* tat -- damit klar bleibt, warum
# es zwei Methoden gibt und nicht nur eine.
muster = LinkIndex(LinkConfig(node_type="Befund", node_source=SOURCE_LABEL,
                              normalize=NORMALIZE_PATTERN))
muster.set_layer_side([("Objekt", "T2_SE2004")])
muster.set_graph_side([{"id": "B_2004", "key": "T2_SE2004"}])
zwischen = muster.keys_for_feature_ids(["Objekt"])
check("Zwischenergebnis ist schon normalisiert", zwischen, ["2004"])
check("doppelt normalisiert findet nichts", muster.node_ids_for_keys(zwischen), [])
check("einfach normalisiert findet den Knoten",
      muster.node_ids_for_normalized_keys(zwischen), ["B_2004"])

# Unbekannte Schlüssel bleiben unbekannt, und None stört nicht.
check("unbekannter Schlüssel", muster.node_ids_for_normalized_keys(["9999"]), [])
check("leere Liste", muster.node_ids_for_normalized_keys([]), [])
check("None verträgt sich", muster.node_ids_for_normalized_keys(None), [])


# ===========================================================================
# Mehrere Verknüpfungen nebeneinander
# ===========================================================================
#
# Eine Szene enthält Meshes mit SE-Bezeichnung *und* Meshes mit
# Fundbezeichnung. Beide Verknüpfungen bekommen dieselbe Objektseite -- es sind
# dieselben Objekte -- und unterscheiden sich allein in der Graph-Seite. Genau
# daraus folgt das gewünschte Verhalten: Ein SE-Mesh findet nur im SE-Index
# einen Knoten, ein Fund-Mesh nur im Fund-Index, und keiner stört den anderen.

OBJEKTE = [
    ("T2_SE2004", "T2_SE2004"),
    ("T2_SE2004.001", "T2_SE2004"),     # zweite Geometrie derselben SE
    ("T2_FN1115", "T2_FN1115"),
    ("Nordprofil", "Nordprofil"),             # gehört zu keiner Verknüpfung
]

se_index = LinkIndex(LinkConfig(node_type="Befund", node_source=SOURCE_LABEL,
                                normalize=NORMALIZE_LOOSE))
se_index.set_layer_side(OBJEKTE)
se_index.set_graph_side([
    {"id": "B_2004", "key": "T2_SE2004"},
    {"id": "B_2005", "key": "T2_SE2005"},
])

fund_index = LinkIndex(LinkConfig(node_type="Fund", node_source=SOURCE_LABEL,
                                  normalize=NORMALIZE_LOOSE))
fund_index.set_layer_side(OBJEKTE)          # dieselben Objekte, andere Graph-Seite
fund_index.set_graph_side([
    {"id": "F_1115", "key": "T2_FN1115"},
])

indizes = [se_index, fund_index]


def nodes_for(names):
    """Wie ObjectSync._nodes_for_names: über alle Verknüpfungen einsammeln."""
    out = []
    for idx in indizes:
        keys = idx.keys_for_feature_ids(names)
        for node_id in idx.node_ids_for_normalized_keys(keys):
            if node_id not in out:
                out.append(node_id)
    return out


def names_for(keys):
    """Wie ObjectSync.on_graph_selection: über alle Verknüpfungen vereinigen."""
    out = set()
    for idx in indizes:
        out.update(idx.feature_ids_for_keys(keys))
    return sorted(out)


# Blender -> Explorer
check("SE-Mesh findet den Befund", nodes_for(["T2_SE2004"]), ["B_2004"])
check("Fund-Mesh findet den Fund", nodes_for(["T2_FN1115"]), ["F_1115"])
check("Objekt ohne Bezug findet nichts", nodes_for(["Nordprofil"]), [])

# Beide zusammen ausgewählt: beide Knoten kommen, keiner verdrängt den anderen.
check("gemischte Auswahl liefert beide",
      sorted(nodes_for(["T2_SE2004", "T2_FN1115"])), ["B_2004", "F_1115"])

# Explorer -> Blender
check("Befund wählt beide Geometrien", names_for(["T2_SE2004"]),
      ["T2_SE2004", "T2_SE2004.001"])
check("Fund wählt sein Mesh", names_for(["T2_FN1115"]), ["T2_FN1115"])

# Der Kern: Die zweite Verknüpfung darf die erste nicht verdrängen. Ohne
# getrennte Indizes hätte der zuletzt gesetzte Knotentyp gewonnen.
check("SE-Verknüpfung bleibt neben der Fund-Verknüpfung bestehen",
      (nodes_for(["T2_SE2004"]), nodes_for(["T2_FN1115"])),
      (["B_2004"], ["F_1115"]))

# Und jede Verknüpfung berichtet für sich -- ein gemeinsamer Bericht würde
# "nur im Graphen" für Knoten melden, die schlicht zur anderen Verknüpfung
# gehören.
check("SE-Bericht zählt nur Befunde", se_index.report().matched_keys, 1)
check("SE-Bericht kennt den unverknüpften Befund", se_index.report().only_in_graph,
      ["t2_se2005"])
check("Fund-Bericht zählt nur Funde", fund_index.report().matched_keys, 1)


# ===========================================================================
# Anzeige: URIs in Kurzform
# ===========================================================================
#
# Typen und Properties aus importiertem RDF sind volle URIs. Angezeigt wird nur
# das Ende; gespeichert und verglichen wird weiter der volle Wert.

CRM = "http://www.cidoc-crm.org/cidoc-crm/"
check("Pfad-URI", short_name(CRM + "E22_Human-Made_Object"), "E22_Human-Made_Object")
check("Fragment-URI", short_name("http://example.org/ont#Befund"), "Befund")
check("Fragment schlägt Pfad", short_name("http://example.org/a/b#c"), "c")
check("abschließender Schrägstrich", short_name("http://example.org/thing/"), "thing")
check("URN", short_name("urn:x:y/z"), "z")
check("Präfixname bleibt", short_name("crm:P46_is_composed_of"), "crm:P46_is_composed_of")
check("schlichter Name bleibt", short_name("SE"), "SE")
check("None wird leer", short_name(None), "")
check("Zahl wird Text", short_name(2001), "2001")

uri_report = LinkIndex(LinkConfig(node_source=SOURCE_LABEL, normalize=NORMALIZE_EXACT))
uri_report.set_layer_side([(1, "http://example.org/se/2001")])
uri_report.set_graph_side([{"id": "N", "key": "http://example.org/se/2002"}])
check("Bericht zeigt Kurzform", "2001" in uri_report.report().details()
      and "http://" not in uri_report.report().details(), True)


# -- Namen wie im GraphExplorer ----------------------------------------------
#
# Studio-Exporte haben Typschlüssel wie ".../A8_Stratigraphic_Unit#as:stratigraphic_unit";
# der GraphExplorer zeigt "Stratigraphic Unit" (schema.typeLabels) und schickt
# diesen Namen seit Vertragsversion 3 mit.

SU = "http://www.cidoc-crm.org/extensions/crmarchaeo/A8_Stratigraphic_Unit#as:stratigraphic_unit"
FIND = "http://www.cidoc-crm.org/cidoc-crm/E19_Physical_Object#as:fund_human_made"
INFO = {"loaded": True, "types": [{"type": SU, "count": 40}, {"type": FIND, "count": 245}],
        "labels": {"types": {SU: "Stratigraphic Unit"},
                   "rels": {CRM + "P46_is_composed_of": "is composed of"},
                   "attrs": {"Weight (g)": "Weight (g)"}}}
names = DisplayNames(INFO)
check("Typname vom Explorer", names.type(SU), "Stratigraphic Unit")
check("Beziehungsname vom Explorer", names.rel(CRM + "P46_is_composed_of"), "is composed of")
check("Attributname vom Explorer", names.attr("Weight (g)"), "Weight (g)")

# Ohne Namen (älterer GraphExplorer): Kurzform, ohne "as:", Unterstriche weg.
check("Rückfall: ohne Präfix", names.type(FIND), "fund human made")
check("Rückfall: Pfad-URI", DisplayNames().rel(CRM + "P46_is_composed_of"), "P46 is composed of")
check("Rückfall: Präfixname", DisplayNames().rel("crm:P46_is_composed_of"), "P46 is composed of")
check("Rückfall: Attribut wie humaniseKey", DisplayNames().attr("crm:P3_has_note"), "P3 has note")
check("Rückfall: schlichter Name", DisplayNames().type("Befund"), "Befund")
check("Name am Typ-Eintrag genügt",
      DisplayNames({"types": [{"type": FIND, "label": "Find (human-made)"}]}).type(FIND),
      "Find (human-made)")


# -- Ergebnis ----------------------------------------------------------------

if failures:
    print("\n%d Prüfung(en) fehlgeschlagen:\n" % len(failures))
    for f in failures:
        print("  - %s\n" % f)
    sys.exit(1)

print("linking.py: alle Prüfungen bestanden.")
