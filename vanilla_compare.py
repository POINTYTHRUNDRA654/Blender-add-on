"""Measurements and plain rule checks for Mossy's "most similar vanilla mesh" lookup.

This add-on stays thin on purpose: it only MEASURES the selected mesh(es) and runs deterministic
checks. The comparison with the base game's real meshes happens in Mossy / Brain B on the
user's computer (see BridgeServer.ts 'mesh_compare'), so no ML or heavy packages are needed
inside Blender's bundled Python.

Everything here is plain Blender API: no network, no keys, no cloud.

Units: bounds are reported in Blender units. They equal NIF units when PyNifly's scale factor
is left at its default of 1.0 (the FO4 pipeline this add-on sets up).
"""
from __future__ import annotations

MAX_NIF_VERTICES = 65535     # BSTriShape stores its vertex count in 16 bits
MAX_INFLUENCES = 4           # FO4 skinning keeps 4 bone weights per vertex
SCALE_TOLERANCE = 1e-3


def selected_meshes(bpy) -> list:
    sel = [o for o in bpy.context.selected_objects if o.type == "MESH"]
    if not sel:
        active = bpy.context.active_object
        if active is not None and active.type == "MESH":
            sel = [active]
    return sel


def _armature_of(obj):
    for m in obj.modifiers:
        if m.type == "ARMATURE" and getattr(m, "object", None) is not None:
            return m.object
    parent = obj.parent
    if parent is not None and parent.type == "ARMATURE":
        return parent
    return None


def _world_bounds(obj):
    """Axis-aligned world-space bounds of an object's bounding box as (mins, maxs)."""
    mw = obj.matrix_world
    corners = [mw @ __import__("mathutils").Vector(c) for c in obj.bound_box]
    return ([min(c[i] for c in corners) for i in range(3)], [max(c[i] for c in corners) for i in range(3)])


def mesh_features(bpy) -> dict:
    """Response for Mossy's 'mesh_features' command."""
    objs = selected_meshes(bpy)
    if not objs:
        return {"status": "error", "message": "Select a mesh object in Blender first."}
    depsgraph = bpy.context.evaluated_depsgraph_get()
    verts = tris = 0
    skinned = False
    bones = 0
    mins = [float("inf")] * 3
    maxs = [float("-inf")] * 3
    checks: list = []
    for obj in objs:
        ev = obj.evaluated_get(depsgraph)
        mesh = ev.to_mesh()
        try:
            mesh.calc_loop_triangles()
            verts += len(mesh.vertices)
            tris += len(mesh.loop_triangles)
            arm = _armature_of(obj)
            if arm is not None:
                skinned = True
                bones = max(bones, len(arm.data.bones))
            lo, hi = _world_bounds(obj)
            for i in range(3):
                mins[i] = min(mins[i], lo[i])
                maxs[i] = max(maxs[i], hi[i])
            checks.extend(rule_checks(obj, mesh, arm))
        finally:
            ev.to_mesh_clear()
    features = {"verts": verts, "tris": tris, "shapes": len(objs), "skinned": int(skinned),
                "bound_x": round(maxs[0] - mins[0], 4), "bound_y": round(maxs[1] - mins[1], 4),
                "bound_z": round(maxs[2] - mins[2], 4)}
    if skinned:
        features["bones"] = bones
    if verts > MAX_NIF_VERTICES:
        checks.append(_check("too_many_vertices", "error", 1, None,
                             f"{verts:,} vertices in total; a single NIF shape stores at most {MAX_NIF_VERTICES:,}. "
                             "Split the mesh (or check each object stays under the limit)."))
    return {"status": "success", "features": features, "checks": checks,
            "objects": [o.name for o in objs],
            "note": "bounds are in Blender units (= NIF units at PyNifly scale factor 1.0). "
                    "BSXFlags, collision and shader flags are deliberately not sent: they are what the vanilla comparison tells you."}


def _check(code, severity, count, obj_name, message):
    return {"code": code, "severity": severity, "count": count, "object": obj_name, "message": message}


def rule_checks(obj, mesh, armature) -> list:
    """Deterministic, verifiable checks on one evaluated mesh. No guessing."""
    out = []
    name = obj.name

    ngons = sum(1 for p in mesh.polygons if len(p.vertices) > 4)
    if ngons:
        out.append(_check("ngons", "warning", ngons, name,
                          f"'{name}' has {ngons} n-gon face(s) (5+ sides). Triangulate or fix them before export; "
                          "the exporter's own triangulation can shade them unpredictably."))

    sx, sy, sz = obj.scale
    if any(abs(abs(s) - 1.0) > SCALE_TOLERANCE for s in (sx, sy, sz)):
        out.append(_check("unapplied_scale", "warning", 1, name,
                          f"'{name}' has unapplied scale ({sx:.3f}, {sy:.3f}, {sz:.3f}). Apply it (Ctrl+A > Scale)."))
    if (sx * sy * sz) < 0:
        out.append(_check("negative_scale", "error", 1, name,
                          f"'{name}' has a negative (mirrored) scale, which flips normals and winding on export. "
                          "Apply the scale and recalculate normals."))

    if len(mesh.uv_layers) == 0:
        out.append(_check("no_uv", "error", 1, name, f"'{name}' has no UV map; FO4 textures need at least one."))

    loose = _count_loose_vertices(mesh)
    if loose:
        out.append(_check("loose_vertices", "warning", loose, name,
                          f"'{name}' has {loose} loose vertex/vertices that belong to no face. They export as stray geometry."))

    if armature is not None:
        bone_names = {b.name for b in armature.data.bones}
        over = unweighted = 0
        for v in mesh.vertices:
            influences = sum(1 for g in v.groups
                             if g.weight > 1e-4 and _group_name(obj, g.group) in bone_names)
            if influences > MAX_INFLUENCES:
                over += 1
            elif influences == 0:
                unweighted += 1
        if over:
            out.append(_check("too_many_influences", "error", over, name,
                              f"{over} vertex/vertices on '{name}' have more than {MAX_INFLUENCES} bone influences; "
                              f"FO4 keeps only {MAX_INFLUENCES}. Limit total (Weights > Limit Total = {MAX_INFLUENCES}) and normalize."))
        if unweighted:
            out.append(_check("unweighted_vertices", "error", unweighted, name,
                              f"{unweighted} vertex/vertices on '{name}' have no bone weights and will stay at the origin when animated."))
    return out


def _group_name(obj, index) -> str:
    try:
        return obj.vertex_groups[index].name
    except (IndexError, AttributeError):
        return ""


def _count_loose_vertices(mesh) -> int:
    used = set()
    for p in mesh.polygons:
        used.update(p.vertices)
    return len(mesh.vertices) - len(used)
