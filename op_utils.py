"""
op_utils.py

Whether a Blender operator really exists.

``hasattr(bpy.ops.<category>, "<name>")`` cannot answer that: bpy.ops
categories make up an object for any attribute name, so it is True even for
operators that were never registered. Verified in Blender 5.1 (2026-11-04):
hasattr(bpy.ops.wm, "definitely_not_a_tool") is True, and so are
import_scene.obj (removed in 4.0), wm.collada_import (removed in 5.0) and
import_anim.bvh (an optional extension since 4.2). Code that used hasattr to
pick an importer called removed operators and failed with "could not be
found" instead of falling back. ``get_rna_type()`` only succeeds for a
registered operator, so that is what this checks.
"""

try:
    import bpy
except ImportError:  # loaded outside Blender (tests)
    bpy = None


def op_exists(idname: str) -> bool:
    """True if the operator ``"<category>.<name>"`` is registered, e.g.
    ``op_exists("wm.obj_import")``."""
    category, _, name = str(idname).partition(".")
    if bpy is None or not category or not name:
        return False
    try:
        getattr(getattr(bpy.ops, category), name).get_rna_type()
        return True
    except Exception:
        return False
