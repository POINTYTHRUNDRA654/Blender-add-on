"""
nif_import.py

The one NIF import for the whole add-on. Every feature that brings a NIF into
the scene (Import button, Batch Import, Import Mod Folder, Asset Library,
Skeleton wizard, Smart Presets) calls import_nif() instead of calling
PyNifly's operator itself, so a fix to importing lands everywhere at once.
There used to be six separate PyNifly calls, each with its own subset of the
steps below.

What every import gets:

  * The active object is cleared first. PyNifly imports onto an active mesh
    as a shape key ("Active object is a mesh, will import as shape key if
    possible"), so a mesh left active silently swallowed the file.
  * Blender's Textures folder preference is made safe for the call. Its
    default is "//" (next to the .blend). PyNifly joins that straight into a
    pathlib.Path; on Windows "//textures/x.dds" is read as a network share
    with an empty name, so the texture search raises, PyNifly drops the
    material and draws the mesh as wireframe. Hit on a loose NIF in Downloads
    (no Meshes folder above it, so PyNifly falls through to that preference),
    2026-11-03.
  * The result is checked: PyNifly returns set() on success and only adds
    'CANCELLED' on failure, so "nothing new in the scene" counts as failure.
  * Objects PyNifly hid (NIF visibility flags) are unhidden.
  * Optionally shrunk from game units to Blender metres (fo4_scale) and
    given their missing textures (fo4_texture_resolver).
"""

import os
from contextlib import contextmanager

import bpy


def pynifly_available() -> bool:
    import_scene = getattr(bpy.ops, 'import_scene', None)
    return import_scene is not None and hasattr(import_scene, 'pynifly')


@contextmanager
def _safe_texture_directory():
    """Swap a blend-relative Textures preference ("//...") for an absolute
    folder (or nothing) while PyNifly runs, then put it back exactly."""
    filepaths = bpy.context.preferences.filepaths
    original = filepaths.texture_directory
    replacement = original
    if original.startswith('//'):
        absolute = bpy.path.abspath(original) if bpy.data.filepath else ''
        replacement = absolute if absolute and os.path.isdir(absolute) else ''
    if replacement != original:
        filepaths.texture_directory = replacement
    try:
        yield
    finally:
        if filepaths.texture_directory != original:
            filepaths.texture_directory = original


def repair_imported_textures(context, objects, filepath) -> str:
    """Fill in textures PyNifly could not find; '' when there is nothing to say."""
    try:
        from . import fo4_texture_resolver as _ftr
    except Exception as exc:
        print(f"[FO4Tex] texture repair unavailable: {exc}")
        return ""
    roots = _ftr.preferred_roots(context)
    filled = missing = 0
    for o in objects:
        if getattr(o, "type", None) != 'MESH':
            continue
        try:
            ok, msg = _ftr.resolve_and_apply(o, filepath, extra_roots=roots)
        except Exception as exc:
            ok, msg = False, f"texture repair failed: {exc}"
        print(f"[FO4Tex] {o.name}: {msg}")
        if "filled in:" in msg:
            filled += 1
        if "NOT found:" in msg or not ok:
            missing += 1
    notes = []
    if filled:
        notes.append(f"textures filled in on {filled} mesh(es)")
    if missing:
        notes.append(f"{missing} mesh(es) have textures that could not be found "
                     "(set your FO4 Data folder in the add-on preferences; details in the console)")
    return (" -- " + "; ".join(notes)) if notes else ""


def import_nif(context, filepath, shrink=True, repair_textures=True):
    """Import one NIF through PyNifly.

    Returns ``(ok, new_objects, message)``. ``new_objects`` is every object
    the import added (a before/after scene diff, never the selection).
    ``shrink`` scales them to Blender metres and tags them for the ×70 on
    export; ``repair_textures`` fills in texture files PyNifly couldn't find.
    """
    filename = os.path.basename(filepath)
    if not pynifly_available():
        return False, [], ("PyNifly not found -- install it via the Setup & "
                           "Status tab and restart Blender.")

    before = set(context.scene.objects)
    try:
        bpy.ops.object.select_all(action='DESELECT')
    except Exception:
        pass
    context.view_layer.objects.active = None
    try:
        with _safe_texture_directory():
            result = bpy.ops.import_scene.pynifly(filepath=filepath)
    except Exception as exc:
        return False, [], f"PyNifly NIF import error for {filename}: {exc}"

    new = [o for o in context.scene.objects if o not in before]
    if 'CANCELLED' in set(result or ()) or not new:
        return False, [], (f"PyNifly did not import anything from {filename} "
                           f"(result={result!r})")

    for o in new:
        try:
            o.hide_set(False)
            o.hide_viewport = False
        except Exception:
            pass

    if shrink:
        from . import fo4_scale
        fo4_scale.shrink_imported(context, new)

    note = repair_imported_textures(context, new, filepath) if repair_textures else ""
    return True, new, f"Imported NIF: {filename}{note}"
