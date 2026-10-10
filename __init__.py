"""GraphExplorer Link for Blender -- Blender-Extension.

Koppelt Blender-Objekte über ihren Namen an die Knoten eines Wissensgraphen im
GraphExplorer. Der Explorer läuft im Browser, das Addon hält die Leitung --
derselbe Aufbau wie beim QGIS-Plugin, nur ohne Qt.

Die Metadaten stehen in ``blender_manifest.toml`` (Blender 4.2+). Siehe
README.md.
"""


def register():
    # Erst hier importieren, nicht oben: Der Import zieht bpy-abhängige Module
    # nach. Schlägt einer davon fehl, soll das beim Aktivieren des Addons
    # auffallen -- mit einer Meldung, die auf die richtige Zeile zeigt -- und
    # nicht schon beim Einlesen der Addon-Liste.
    from . import addon

    addon.register()


def unregister():
    from . import addon

    addon.unregister()
