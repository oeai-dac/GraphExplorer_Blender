"""Materialien und Viewport-Farben setzen -- der Teil, der die Szene anfasst.

Das Gegenstück zu ``coloring.py``, das rechnet, ohne ``bpy`` zu kennen. Hier
steht alles, was einen echten Blend-Zustand braucht -- und die eine Frage, die
dieses Modul im Kern beantwortet: **wie man einfärbt, ohne etwas kaputtzumachen.**

Ein Grabungsmodell ist keine Sammlung von Platzhaltern. Da hängen texturierte
Meshes aus der Photogrammetrie, und deren Material ist Arbeitsergebnis, kein
Beiwerk. Eine Einfärbung, die es überschreibt, ist keine Ansicht, sondern ein
Datenverlust. Deshalb zwei Vorkehrungen:

1. **Objektgebundene Materialslots.** Blender kann einen Slot entweder aus den
   Mesh-Daten oder aus dem Objekt speisen (``slot.link``). Die Einfärbung
   schaltet auf ``OBJECT`` um und setzt ihr Material dort. Das Material in den
   Mesh-Daten bleibt unangetastet -- es ist nur verdeckt, nicht ersetzt.
2. **Ein Sicherungsvermerk am Objekt.** Was vorher eingestellt war, steht als
   Custom Property am Objekt und wird beim Aufheben Slot für Slot
   zurückgestellt. Der Vermerk hängt am Objekt und nicht in einem Modul-Global:
   Er muss ein Speichern, ein Neuladen und ein Aus- und Einschalten des Addons
   überstehen, sonst bliebe die Szene bunt und niemand käme mehr heran.

Zusätzlich wird ``object.color`` gesetzt. Das kostet nichts, ist völlig
folgenlos und wirkt im Solid-Modus mit "Farbe: Objekt" sofort -- also genau
dort, wo man beim Sichten ohnehin arbeitet.
"""

import json

import bpy

from .coloring import MATERIAL_PREFIX, material_name

#: Der Sicherungsvermerk am Objekt. Der Name ist absichtlich sprechend: Wer ihn
#: im Objekt-Eigenschaftenfeld unter "Custom Properties" sieht, soll erkennen,
#: woher er kommt -- und dass er verschwindet, wenn man die Einfärbung aufhebt.
BACKUP_PROP = "graphexplorer_shading"


# ---------------------------------------------------------------------------
# Sichern und Zurückstellen
# ---------------------------------------------------------------------------


def _backup(obj, added_slot):
    """Den Zustand vor der Einfärbung festhalten -- nur beim ersten Mal.

    Beim zweiten Einfärben darf nicht der eingefärbte Zustand gesichert werden;
    sonst führte "Aufheben" zurück auf eine Farbe statt auf das Material, das
    einmal da war.
    """
    if BACKUP_PROP in obj:
        return
    obj[BACKUP_PROP] = json.dumps({
        "added_slot": bool(added_slot),
        "color": list(obj.color),
        "slots": [
            {"link": slot.link,
             # Bei link='DATA' steht das Material in den Mesh-Daten und bleibt
             # ohnehin liegen. Zu merken ist nur, was am *Objekt* hing.
             "material": slot.material.name
                         if (slot.link == "OBJECT" and slot.material) else ""}
            for slot in obj.material_slots
        ],
    })


def restore(obj):
    """Ein Objekt in den Zustand vor der Einfärbung zurückversetzen."""
    raw = obj.get(BACKUP_PROP)
    if not raw:
        return False
    try:
        state = json.loads(raw)
    except (TypeError, ValueError):
        # Ein beschädigter Vermerk darf das Aufheben nicht blockieren -- sonst
        # bliebe das Objekt für immer eingefärbt. Lieber den Vermerk loswerden
        # und die Farbe stehen lassen, als beides zu behalten.
        del obj[BACKUP_PROP]
        return False

    slots = state.get("slots") or []
    for i, slot in enumerate(obj.material_slots):
        if i >= len(slots):
            break
        saved = slots[i]
        # Erst das objektgebundene Material zurückstellen -- solange der Slot
        # noch auf OBJECT steht --, dann die Bindung. Andersherum bliebe das
        # GE_-Material am Objekt hängen, unsichtbar, aber als Benutzer gezählt:
        # purge_materials räumte es nie ab, und mit jedem Einfärben kämen
        # weitere dazu.
        if slot.link == "OBJECT":
            slot.material = bpy.data.materials.get(saved.get("material") or "")
        slot.link = saved.get("link") or "DATA"

    if state.get("added_slot") and obj.data is not None:
        # Genau den Slot wieder weg, den die Einfärbung angelegt hat: den
        # letzten. Vorher die Objektbindung lösen, sonst hinge das Material
        # noch am Objekt, während der Slot verschwindet.
        try:
            obj.material_slots[len(obj.material_slots) - 1].link = "DATA"
            obj.data.materials.pop(index=len(obj.data.materials) - 1)
        except (RuntimeError, IndexError, AttributeError):
            pass

    color = state.get("color")
    if color and len(color) == 4:
        obj.color = color

    del obj[BACKUP_PROP]
    return True


def clear_all(objects=None):
    """Alle Einfärbungen aufheben. Gibt zurück, wie viele Objekte betroffen waren.

    Läuft über *alle* Objekte der Datei, nicht nur über die sichtbaren: Was in
    einer ausgeschlossenen Sammlung liegt, wurde vielleicht in einer früheren
    Sitzung eingefärbt, und ein Aufheben, das die Hälfte stehenlässt, ist keins.
    """
    count = 0
    for obj in (objects if objects is not None else bpy.data.objects):
        if restore(obj):
            count += 1
    return count


def purge_materials():
    """Übriggebliebene Einfärbungs-Materialien löschen.

    Nur solche mit der Vorsilbe und ohne Benutzer. Blender räumt sie beim
    nächsten Speichern zwar selbst ab, aber bis dahin stehen zwanzig graue
    Einträge im Materialbrowser, die niemand mehr zuordnen kann.
    """
    doomed = [mat for mat in bpy.data.materials
              if mat.name.startswith(MATERIAL_PREFIX) and mat.users == 0]
    for mat in doomed:
        bpy.data.materials.remove(mat)
    return len(doomed)


# ---------------------------------------------------------------------------
# Einfärben
# ---------------------------------------------------------------------------


def ensure_material(name, color):
    """Ein Material dieses Namens mit dieser Farbe -- angelegt oder aufgefrischt.

    Gesetzt wird beides: die Anzeigefarbe für den Solid-Modus *und* die
    Grundfarbe des Principled BSDF für Material- und Renderansicht. Nur eines
    von beidem zu setzen ist der übliche Grund, warum eine Einfärbung "beim
    Umschalten verschwindet".
    """
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)

    rgba = (color[0], color[1], color[2], 1.0)
    mat.diffuse_color = rgba

    if not mat.use_nodes:
        mat.use_nodes = True
    principled = mat.node_tree.nodes.get("Principled BSDF") if mat.node_tree else None
    if principled is None and mat.node_tree:
        principled = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if principled is not None:
        principled.inputs["Base Color"].default_value = rgba
    return mat


def apply_material(obj, mat):
    """Das Material objektgebunden in *alle* Slots -- die Mesh-Daten bleiben.

    Alle Slots und nicht nur der erste: Ein Objekt mit drei Materialien soll
    eine Farbe zeigen und nicht zwei Drittel seiner alten.
    """
    added = False
    if not obj.material_slots:
        if obj.data is None or not hasattr(obj.data, "materials"):
            return False        # Empties, Kameras -- nichts zum Einfärben
        obj.data.materials.append(None)
        added = True

    _backup(obj, added)
    for slot in obj.material_slots:
        slot.link = "OBJECT"
        slot.material = mat
    return True


class ApplyResult(object):

    def __init__(self):
        self.objects = 0
        self.materials = 0
        self.missing = []      # Objektnamen, die es in der Szene nicht (mehr) gibt
        self.skipped = 0       # Objekte ohne Mesh-Daten


def apply_plan(plan, source_label, with_materials=True):
    """Einen ``coloring.ColorPlan`` auf die Szene anwenden.

    Setzt an jeder Gruppe zusätzlich ``material`` auf den benutzten
    Materialnamen -- daran findet die Legende später ihr Material wieder, wenn
    jemand die Farbe von Hand ändert.
    """
    result = ApplyResult()
    taken = []

    for group in plan.groups:
        mat = None
        if with_materials:
            name = material_name(source_label, group.value, taken)
            taken.append(name)
            mat = ensure_material(name, group.color)
            group.material = name
            result.materials += 1
        else:
            group.material = ""

        for name in group.objects:
            obj = bpy.data.objects.get(name)
            if obj is None:
                result.missing.append(name)
                continue
            if mat is not None and not apply_material(obj, mat):
                result.skipped += 1
                continue
            if mat is None:
                # Auch ohne Material soll sich die Farbe zurücknehmen lassen.
                _backup(obj, False)
            obj.color = (group.color[0], group.color[1], group.color[2], 1.0)
            result.objects += 1

    return result


def recolor(material_name_, color, objects=None):
    """Die Farbe einer Gruppe nachträglich ändern.

    Das Material anzufassen genügt für alles, was es benutzt -- aber
    ``object.color`` hängt an jedem Objekt einzeln und muss nachgezogen werden.
    Gesucht wird darüber, wer das Material trägt: Eine Liste von Objektnamen je
    Gruppe mitzuführen wäre bei tausend Meshes eine Liste, die in jeder
    Blend-Datei mitwandert, und sie wäre nach dem ersten Umbenennen falsch.
    """
    mat = bpy.data.materials.get(material_name_ or "")
    if mat is not None:
        ensure_material(mat.name, color)

    rgba = (color[0], color[1], color[2], 1.0)
    touched = 0
    for obj in (objects if objects is not None else bpy.data.objects):
        if BACKUP_PROP not in obj:
            continue
        if mat is not None and not any(slot.material is mat for slot in obj.material_slots):
            continue
        obj.color = rgba
        touched += 1
    return touched


# ---------------------------------------------------------------------------
# Ansicht
# ---------------------------------------------------------------------------


def show_object_color():
    """Alle 3D-Ansichten auf Solid + Objektfarbe stellen.

    Der unscheinbarste und meistgebrauchte Knopf des ganzen Werkzeugs: Ohne
    diese Einstellung bleibt der Viewport grau, die Einfärbung sitzt aber
    längst. Man sucht den Fehler dann in der Kopplung.
    """
    manager = getattr(bpy.context, "window_manager", None)
    if manager is None:
        return 0
    count = 0
    for window in manager.windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type != "VIEW_3D":
                    continue
                space.shading.type = "SOLID"
                space.shading.color_type = "OBJECT"
                count += 1
            area.tag_redraw()
    return count
