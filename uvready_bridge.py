"""Optional use of the UVReady extension (GPL, extensions.blender.org-style package "uvready") for automatic UVs.

Measured 2026-11-25 on the Game-Ready mushroom (2,052 tris): UVReady 113 islands, 68.7% of the texture used,
angle error 4.4 deg, 0 flipped, 7.7 s; our UV Kit 215 / 57.5% / 8.3 deg; Smart UV Project 761 / 29.6% / 5.1 deg.
More of the texture used = more texture detail on the same size map (LOD bakes used Smart UV's 30%).

This module only ADDS a case: callers keep their own unwrap and call try_unwrap() first (or after, to replace it).
When UVReady is not installed, refuses the mesh, or leaves collapsed faces, the object's UVs are put back exactly as
they were and False is returned, so the caller's existing path runs unchanged."""
import bpy
import numpy as np

_SETTINGS = "uvready_settings"
_RESULT = "uvready_result"


def available() -> bool:
    """True when the UVReady extension is enabled in this Blender."""
    try:
        return hasattr(bpy.ops, "uvready") and hasattr(bpy.ops.uvready, "unwrap") \
            and hasattr(bpy.context.scene, _SETTINGS) and bpy.ops.uvready.unwrap.get_rna_type() is not None
    except Exception:
        return False


def _collapsed_share(me) -> float:
    """Share of triangles whose UV area is ~0 (squashed flat: the texture smears across them)."""
    me.calc_loop_triangles()
    n = len(me.loop_triangles)
    if not n or not me.uv_layers.active:
        return 1.0
    uv = np.empty(len(me.loops) * 2, np.float32)
    me.uv_layers.active.data.foreach_get("uv", uv)
    uv = uv.reshape(-1, 2)
    li = np.empty(n * 3, np.int32)
    me.loop_triangles.foreach_get("loops", li)
    t = uv[li.reshape(-1, 3)]
    a = np.abs((t[:, 1, 0] - t[:, 0, 0]) * (t[:, 2, 1] - t[:, 0, 1]) - (t[:, 1, 1] - t[:, 0, 1]) * (t[:, 2, 0] - t[:, 0, 0]))
    return float((a < 1e-12).mean())


def try_unwrap(context, obj, tex_size: int = 2048, margin_px: int = 4, hard_surface: bool = False,
               max_faces: int = 200000) -> "tuple[bool, str]":
    """Unwrap *obj* with UVReady into its ACTIVE UV map (replaced, so the UV map count/names do not change).
    Returns (True, summary) when used; (False, reason) with the UVs untouched otherwise."""
    if obj is None or obj.type != 'MESH' or not obj.data.polygons:
        return False, "not a mesh with faces"
    if not available():
        return False, "UVReady is not installed"
    me = obj.data
    if len(me.polygons) > max_faces:
        return False, f"{len(me.polygons):,} faces is over {max_faces:,} (UVReady blocks Blender too long)"
    if obj.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    created = False
    if not me.uv_layers:
        me.uv_layers.new(name="UVMap")
        created = True
    before = np.empty(len(me.loops) * 2, np.float32)
    me.uv_layers.active.data.foreach_get("uv", before)
    n_layers = len(me.uv_layers)
    s = getattr(context.scene, _SETTINGS)
    keys = ("margin", "texture", "match_density", "align_axes", "replace", "max_faces")
    saved = {k: getattr(s, k) for k in keys if hasattr(s, k)}
    sel = list(context.selected_objects)
    act = context.view_layer.objects.active
    ok, msg = False, ""
    try:
        s.margin = int(margin_px)
        sizes = [512, 1024, 2048, 4096, 8192]
        s.texture = str(min(sizes, key=lambda x: abs(x - int(tex_size))))
        s.match_density = False
        s.align_axes = bool(hard_surface)
        s.replace = True
        s.max_faces = max(int(max_faces), 1000)
        for o in context.selected_objects:
            o.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        r = bpy.ops.uvready.unwrap()
        head = ""
        try:
            import json
            info = json.loads(getattr(context.scene, _RESULT, "") or "{}")
            head = info.get("head", "")
            ok = 'FINISHED' in r and bool(info.get("ok"))
        except Exception:
            ok = 'FINISHED' in r
        if ok and len(me.uv_layers) != n_layers:
            ok, msg = False, "UVReady added a UV map instead of replacing the active one"
        if ok:
            bad = _collapsed_share(me)
            if bad > 0.01:
                ok, msg = False, f"UVReady left {bad * 100:.1f}% squashed faces"
        if ok:
            msg = "UVReady" + (f" ({head})" if head else "")
    except Exception as e:
        ok, msg = False, f"UVReady failed: {e}"
    finally:
        for k, v in saved.items():
            try:
                setattr(s, k, v)
            except Exception:
                pass
        for o in context.selected_objects:
            o.select_set(False)
        for o in sel:
            try:
                o.select_set(True)
            except Exception:
                pass
        context.view_layer.objects.active = act
    if not ok:
        # put everything back exactly: the caller's own unwrap runs as before
        while len(me.uv_layers) > n_layers:
            me.uv_layers.remove(me.uv_layers[-1])
        if created:
            me.uv_layers.remove(me.uv_layers[0])
        elif me.uv_layers.active:
            me.uv_layers.active.data.foreach_set("uv", before)
        me.update()
    return ok, msg
