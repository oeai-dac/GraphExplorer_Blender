"""Ein ``bpy``-Ersatz, gerade groß genug für ``addon.py`` und ``shading.py``.

Was er kann, ist eng umrissen -- und die Grenze gehört genannt, weil ein
Prüfstand, der mehr zu beweisen scheint, als er kann, schlimmer ist als keiner:

**Geprüft wird die Verdrahtung**, nicht Blender. Ob eine Seitenleiste die
Knöpfe zeichnet, die es gibt; ob die Klassen, die ``register()`` anmeldet, auch
existieren; ob das Einfärben die Materialslots so umstellt, dass das Aufheben
den Zustand von vorher zurückbringt. Also die Sorte Fehler, die sonst erst in
Blender auffällt -- und dort als roter Kasten in der Seitenleiste, was für ein
Werkzeug, das im Institut herumgehen soll, der schlechteste Ort ist.

**Nicht geprüft wird Blender selbst**: kein Zeichnen, kein Depsgraph, keine
Undo-Stufen, keine Frage, ob ein Slot mit ``link='OBJECT'`` im Viewport auch
wirklich das zeigt, was hier steht. Das kann nur Blender.

Der Ersatz bildet deshalb nur ab, was die beiden Module anfassen, und lässt
sich lieber mit einem AttributeError vernehmen, als etwas stillschweigend
hinzunehmen, das echtes bpy zurückweisen würde.
"""

import sys
import types


# ---------------------------------------------------------------------------
# Eigenschaften
# ---------------------------------------------------------------------------

class Prop(object):
    """Was ``StringProperty(...)`` und Konsorten hier zurückgeben."""

    def __init__(self, kind, kwargs):
        self.kind = kind
        self.kwargs = kwargs

    def default(self):
        if "default" in self.kwargs:
            return self.kwargs["default"]
        return {
            "String": "", "Bool": False, "Int": 0,
            "FloatVector": (0.0, 0.0, 0.0), "Enum": "",
        }.get(self.kind)


def _prop_factory(kind):
    def make(**kwargs):
        return Prop(kind, kwargs)
    return make


class Collection(list):
    """``CollectionProperty`` -- eine Liste mit den drei Blender-Methoden."""

    def __init__(self, item_type):
        super().__init__()
        self.item_type = item_type

    def add(self):
        item = self.item_type()
        self.append(item)
        return item

    def clear(self):
        del self[:]

    def remove(self, index):
        del self[index]


class StructBase(object):
    """Gemeinsame Basis für PropertyGroup, Operator, Panel, UIList.

    Legt beim Erzeugen die annotierten Eigenschaften als schlichte Attribute an
    -- in echtem Blender macht das die Registrierung.
    """

    def __init__(self):
        for klass in reversed(type(self).__mro__):
            for name, prop in getattr(klass, "__annotations__", {}).items():
                if not isinstance(prop, Prop):
                    continue
                if prop.kind == "Collection":
                    setattr(self, name, Collection(prop.kwargs["type"]))
                elif prop.kind == "Pointer":
                    setattr(self, name, prop.kwargs["type"]())
                else:
                    setattr(self, name, prop.default())

    def __setattr__(self, name, value):
        # Der update-Rückruf einer Eigenschaft -- daran hängt in addon.py das
        # Nachfärben, wenn jemand in der Legende eine Farbe ändert.
        object.__setattr__(self, name, value)
        prop = self._annotation(name)
        if prop is not None and prop.kwargs.get("update") is not None:
            prop.kwargs["update"](self, Context.current)

    def _annotation(self, name):
        for klass in type(self).__mro__:
            prop = getattr(klass, "__annotations__", {}).get(name)
            if isinstance(prop, Prop):
                return prop
        return None

    def report(self, _level, _message):
        pass


class PropertyGroup(StructBase):
    pass


class Operator(StructBase):
    pass


class Panel(StructBase):
    pass


class UIList(StructBase):
    pass


# ---------------------------------------------------------------------------
# Oberfläche
# ---------------------------------------------------------------------------

class Layout(object):
    """Nimmt alles an und merkt sich, was verlangt wurde.

    Der Zweck ist nicht die Aufzeichnung, sondern der Durchlauf: Jede Zeile in
    ``draw()`` wird ausgeführt, und ein falsch geschriebener Name fällt auf.
    """

    def __init__(self, sink=None):
        self.sink = sink if sink is not None else {"operators": [], "props": [], "lists": []}

    def _child(self, *_args, **_kwargs):
        return Layout(self.sink)

    row = column = box = split = _child

    def label(self, **_kwargs):
        return None

    def separator(self, **_kwargs):
        return None

    def prop(self, data, name, **_kwargs):
        # getattr statt blindem Anhängen: Ein Tippfehler im Eigenschaftsnamen
        # soll hier auffliegen und nicht erst in Blender.
        getattr(data, name)
        self.sink["props"].append(name)

    def operator(self, idname, **_kwargs):
        self.sink["operators"].append(idname)
        return Layout(self.sink)

    def template_list(self, listtype, _id, data, propname, active_data, active_propname, **_kw):
        getattr(data, propname)
        getattr(active_data, active_propname)
        self.sink["lists"].append(listtype)


class Context(object):
    current = None

    def __init__(self, scene):
        self.scene = scene
        self.window_manager = WindowManager()
        self.window = None
        self.view_layer = types.SimpleNamespace(objects=[])
        Context.current = self


class WindowManager(object):
    def __init__(self):
        self.windows = []
        self.clipboard = ""


# ---------------------------------------------------------------------------
# Daten
# ---------------------------------------------------------------------------

class Material(object):
    def __init__(self, name):
        self.name = name
        self.diffuse_color = (1.0, 1.0, 1.0, 1.0)
        self.use_nodes = False
        self.node_tree = None

    @property
    def users(self):
        """Wer benutzt mich gerade?

        In Blender führt das der Datenblock selbst. Hier wird nachgezählt --
        und zwar wirklich, statt einfach 0 zu liefern: Am Benutzerzähler hängt
        ``shading.purge_materials()``, und ein Ersatz, der immer 0 sagt, ließe
        es genau die Materialien löschen, die gerade in Gebrauch sind. Der
        Fehler stünde dann im Prüfstand statt im Modul.
        """
        # Wie in Blender zählt auch das objektgebundene Material, wenn der Slot
        # gerade wieder auf DATA steht: Die Referenz am Objekt bleibt bestehen,
        # bis jemand sie löscht. Genau das hatte der Ersatz früher übersehen --
        # und mit ihm das Aufheben, das die GE_-Materialien liegen ließ.
        count = 0
        for obj in _DATA.objects if _DATA else ():
            for slot in obj.material_slots:
                if slot._object_material is self:
                    count += 1
                elif slot.link == "DATA" and slot.material is self:
                    count += 1
        return count


class Slot(object):
    def __init__(self, obj, index):
        self._obj = obj
        self._index = index
        self.link = "DATA"
        self._object_material = None

    @property
    def material(self):
        if self.link == "OBJECT":
            return self._object_material
        return self._obj.data.materials[self._index]

    @material.setter
    def material(self, value):
        if self.link == "OBJECT":
            self._object_material = value
        else:
            self._obj.data.materials[self._index] = value


class MaterialList(list):
    def pop(self, index=-1):
        return super().pop(index)


class Mesh(object):
    def __init__(self, name):
        self.name = name
        self.materials = MaterialList()


class Object(object):
    """Nur das, was shading.py anfasst -- samt Custom Properties."""

    def __init__(self, name, materials=()):
        self.name = name
        self.data = Mesh(name + "_mesh")
        for mat in materials:
            self.data.materials.append(mat)
        self.color = (1.0, 1.0, 1.0, 1.0)
        self._props = {}
        self._selected = False
        self._slots = []

    @property
    def material_slots(self):
        """Die Slots bleiben dieselben Objekte -- sonst geht ``slot.link`` verloren.

        In Blender ist die Zahl der Slots eine Eigenschaft der Mesh-Daten,
        ``link`` und das objektgebundene Material dagegen des Objekts. Der
        Ersatz bildet beides nach: mitwachsen mit den Materialien der Daten,
        aber die einmal gesetzte Bindung behalten.
        """
        while len(self._slots) < len(self.data.materials):
            self._slots.append(Slot(self, len(self._slots)))
        while len(self._slots) > len(self.data.materials):
            self._slots.pop()
        return self._slots

    # -- Custom Properties --
    def __contains__(self, key):
        return key in self._props

    def __getitem__(self, key):
        return self._props[key]

    def __setitem__(self, key, value):
        self._props[key] = value

    def __delitem__(self, key):
        del self._props[key]

    def get(self, key, default=None):
        return self._props.get(key, default)

    def select_set(self, state):
        self._selected = bool(state)

    def select_get(self):
        return self._selected

    def visible_get(self):
        return True


class NamedList(list):
    def get(self, name, default=None):
        for item in self:
            if getattr(item, "name", None) == name:
                return item
        return default

    def remove(self, item):
        list.remove(self, item)


class Materials(NamedList):
    def new(self, name):
        mat = Material(name)
        self.append(mat)
        return mat


#: Die eine Datenablage -- damit ``Material.users`` nachzählen kann.
_DATA = None


class Data(object):
    def __init__(self):
        global _DATA
        self.objects = NamedList()
        self.materials = Materials()
        _DATA = self


# ---------------------------------------------------------------------------
# Der Modulbaum
# ---------------------------------------------------------------------------

def install():
    """``bpy`` und ``bpy.props``/``bpy.types`` in ``sys.modules`` hängen."""
    bpy = types.ModuleType("bpy")

    props = types.ModuleType("bpy.props")
    for kind in ("String", "Bool", "Int", "Float", "FloatVector", "Enum",
                 "Collection", "Pointer"):
        setattr(props, kind + "Property", _prop_factory(kind))

    bpy_types = types.ModuleType("bpy.types")
    bpy_types.Operator = Operator
    bpy_types.Panel = Panel
    bpy_types.PropertyGroup = PropertyGroup
    bpy_types.UIList = UIList
    bpy_types.Scene = type("Scene", (object,), {})

    registered = []
    utils = types.SimpleNamespace(
        register_class=registered.append,
        unregister_class=lambda cls: registered.remove(cls),
    )

    timers = types.SimpleNamespace(
        _registered=set(),
        is_registered=lambda fn: fn in timers._registered,
        register=lambda fn, **_kw: timers._registered.add(fn),
        unregister=lambda fn: timers._registered.discard(fn),
    )

    bpy.props = props
    bpy.types = bpy_types
    bpy.utils = utils
    bpy.app = types.SimpleNamespace(timers=timers)
    bpy.data = Data()
    bpy.context = None
    bpy.path = types.SimpleNamespace(abspath=lambda p: p)
    bpy.ops = types.SimpleNamespace(wm=types.SimpleNamespace(url_open=lambda url: None))

    sys.modules["bpy"] = bpy
    sys.modules["bpy.props"] = props
    sys.modules["bpy.types"] = bpy_types
    bpy.registered = registered
    return bpy
