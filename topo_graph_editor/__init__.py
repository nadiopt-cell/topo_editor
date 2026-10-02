def classFactory(iface):
    from .plugin import TopoGraphEditorPlugin
    return TopoGraphEditorPlugin(iface)
