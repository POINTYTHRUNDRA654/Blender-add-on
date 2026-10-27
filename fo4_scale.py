"""
fo4_scale.py

One place for the Fallout 4 game-unit <-> Blender-metre round trip.

Fallout 4 uses ~70 Havok game units per metre. Imports are shrunk ÷70 so
Blender measurements are meaningful; exports are grown ×70 back to game units
right before the NIF is written, then put back exactly as they were.

Both directions work on the WHOLE arrangement in world space, not object by
object. The old per-object approach (set each object's .scale, then
transform_apply) broke on real multi-mesh NIFs in three ways, all measured on
vanilla game files before this module existed:

  * It overwrote an object's own scale instead of multiplying it, so pieces
    with a non-1 NIF scale (explosion effects, TV anchor) came in at the wrong
    size.
  * transform_apply on a parent makes Blender compensate its children, so
    whether a child ended up scaled once, twice or not at all depended on the
    order objects were processed in.
  * Skinned meshes and bone-parented helpers (dismemberment cutpoints) were
    placed against bone matrices that had not been re-evaluated, and pose-bone
    offsets (game units) were never scaled.

On export the per-object version also never scaled positions, so the offsets
between pieces of a multi-mesh NIF stayed in metres while the geometry grew
×70.

Here every object's target is "the original world transform, shrunk (or grown)
around the world origin", applied parent-first with a depsgraph refresh between
hierarchy levels. Verified on 78 vanilla NIFs (armour, clothes, creatures,
weapons, furniture, plants, buildings, effects, vehicles): every mesh, measured
after armature deformation, lands at exactly 1/70 of its original world
position and size.
"""

from __future__ import annotations

import bpy
from mathutils import Matrix

FO4_UNIT_SCALE = 1.0 / 69.99125      # import: game units -> metres
FO4_UNIT_SCALE_INV = 69.99125        # export: metres -> game units


def _depth(obj) -> int:
    d, p = 0, obj.parent
    while p is not None:
        d, p = d + 1, p.parent
    return d


def _scale_world(context, objects, factor, *, copy_data=False):
    """Scale *objects* by *factor* around the world origin, baking the scale
    into mesh/armature data so every object ends with scale (1,1,1) (empties
    keep their own scale and only move).

    With ``copy_data`` the mesh/armature data is swapped for a private copy
    before it is changed (used by export so the originals can be put back).
    Returns {obj: original data} for the swapped objects.
    """
    k = float(factor)
    grow = Matrix.Diagonal((k, k, k, 1.0))
    order = sorted(objects, key=_depth)
    original = {o: o.matrix_world.copy() for o in order}
    target = {o: grow @ original[o] for o in order}
    swapped = {}

    last_depth = None
    for o in order:
        d = _depth(o)
        if last_depth is not None and d != last_depth:
            # Children on a bone / under a just-changed parent need that
            # parent's new matrices evaluated first.
            context.view_layer.update()
        last_depth = d

        loc, rot, sca = target[o].decompose()
        if o.type == 'MESH' and o.data is not None:
            if copy_data:
                swapped[o] = o.data
                o.data = o.data.copy()
            elif o.data.users > 1:
                o.data = o.data.copy()
            o.data.transform(Matrix.Diagonal((sca.x, sca.y, sca.z, 1.0)), shape_keys=True)
            o.data.update()
            o.matrix_world = Matrix.LocRotScale(loc, rot, (1.0, 1.0, 1.0))
        elif o.type == 'ARMATURE' and o.data is not None:
            if copy_data:
                swapped[o] = o.data
                o.data = o.data.copy()
            elif o.data.users > 1:
                o.data = o.data.copy()
            o.data.transform(Matrix.Diagonal((sca.x, sca.y, sca.z, 1.0)))
            # Pose offsets are in the same units as the rest bones (PyNifly
            # puts the skin bind in rest and the NIF node transforms in pose).
            if o.pose is not None:
                f = (sca.x + sca.y + sca.z) / 3.0
                for pb in o.pose.bones:
                    pb.location = pb.location * f
            o.matrix_world = Matrix.LocRotScale(loc, rot, (1.0, 1.0, 1.0))
        else:
            o.matrix_world = Matrix.LocRotScale(loc, rot, original[o].to_scale())
    context.view_layer.update()
    return swapped


def shrink_imported(context, objects) -> int:
    """Shrink freshly imported FO4 objects (game units) to Blender metres.

    Pass every object the import added (a before/after scene diff), not just
    meshes: parents, armatures and helpers have to move with them. Each object
    is tagged ``fo4_unit_scale`` so export knows to grow it back.
    Returns the number of mesh/armature objects scaled.
    """
    objs = [o for o in objects if o is not None]
    if not objs:
        return 0
    _scale_world(context, objs, FO4_UNIT_SCALE)
    n = 0
    for o in objs:
        # Only geometry/armatures carry the tag (helpers and empties never
        # did); "Fix Wrongly-Scaled Import" and export both key off it.
        if o.type in ('MESH', 'ARMATURE', 'CURVE', 'SURFACE'):
            try:
                o["fo4_unit_scale"] = FO4_UNIT_SCALE_INV
                o["fo4_scale_applied"] = True
            except Exception:
                pass
            n += 1
    return n


def export_factor(meshes, infer=None):
    """The single ×factor an export needs, or None when nothing needs scaling.

    Uses each mesh's ``fo4_unit_scale`` tag; untagged meshes use *infer(obj)*
    (export_helpers' size-based guess). One NIF can only have one scale, so
    the most common value wins.
    """
    votes = {}
    for m in meshes:
        f = m.get("fo4_unit_scale", None)
        if f is None and infer is not None:
            try:
                f = infer(m)
            except Exception:
                f = None
        if f:
            f = round(float(f), 5)
            votes[f] = votes.get(f, 0) + 1
    if not votes:
        return None
    f = max(votes, key=votes.get)
    return None if abs(f - 1.0) < 1e-6 else f


class GameUnitSession:
    """Temporarily grow export objects to game units, then put the scene back
    exactly as it was (original mesh/armature data, local transforms, pose).

        session = GameUnitSession(context, export_objects, factor)
        session.apply()
        try:
            ... export ...
        finally:
            session.restore()
    """

    def __init__(self, context, objects, factor):
        self.context = context
        self.factor = float(factor)
        self.objects = self._collect(objects)
        self._saved = {}
        self._swapped = {}
        self._applied = False

    @staticmethod
    def _collect(objects):
        found = []
        seen = set()

        def add(o):
            if o is not None and o.name not in seen:
                seen.add(o.name)
                found.append(o)

        roots = [o for o in objects if o is not None]
        for o in roots:
            add(o)
            p = o.parent
            while p is not None:
                add(p)
                p = p.parent
            stack = list(o.children)
            while stack:
                c = stack.pop()
                add(c)
                stack.extend(c.children)
            for m in getattr(o, "modifiers", []):
                if m.type == 'ARMATURE' and m.object is not None:
                    add(m.object)
                    p = m.object.parent
                    while p is not None:
                        add(p)
                        p = p.parent
        return found

    def apply(self):
        if self._applied or not self.objects:
            return
        for o in self.objects:
            self._saved[o] = (
                o.matrix_basis.copy(),
                o.matrix_parent_inverse.copy(),
                [pb.location.copy() for pb in o.pose.bones] if (o.type == 'ARMATURE' and o.pose) else None,
            )
        self._swapped = _scale_world(self.context, self.objects, self.factor, copy_data=True)
        self._applied = True

    def restore(self):
        if not self._applied:
            return
        try:
            if self.context.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
        except Exception:
            pass
        for o, original_data in self._swapped.items():
            temp = o.data
            o.data = original_data
            try:
                if temp is not None and temp.users == 0:
                    if isinstance(temp, bpy.types.Mesh):
                        bpy.data.meshes.remove(temp)
                    elif isinstance(temp, bpy.types.Armature):
                        bpy.data.armatures.remove(temp)
            except Exception:
                pass
        for o in sorted(self._saved, key=_depth):
            basis, parent_inv, pose_locs = self._saved[o]
            o.matrix_parent_inverse = parent_inv
            o.matrix_basis = basis
            if pose_locs is not None and o.pose is not None:
                for pb, loc in zip(o.pose.bones, pose_locs):
                    pb.location = loc
        self.context.view_layer.update()
        self._applied = False
