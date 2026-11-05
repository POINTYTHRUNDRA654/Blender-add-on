"""
fo4_uv_kit.py
=============
UV and texture tools for Fallout 4 modding: texel density matched to the
vanilla game, a UV checker preview, and curvature / ID map bakes for texture
painting.

The tool set is modelled on TexTools for Blender (renderhjs, SavMartin,
franMarz; GPL-3.0), whose public releases are broken in Blender 5. The code
here is this add-on's own and adds the Fallout 4 parts: densities measured
from the vanilla game and checks that the exporter respects.

Vanilla texel density (pixels of diffuse texture per metre of surface),
median of 40 meshes per category from an unpacked Fallout 4 (2026-11-04):
each shape's UV area times its real diffuse DDS size, over its surface area,
at 70 game units per metre.
"""

import math
import os

import bpy
import bmesh
from bpy.props import EnumProperty, FloatProperty, IntProperty
from bpy.types import Operator, Panel

try:
    from .ui_panels import _FO4SubPanel
except Exception:
    _FO4SubPanel = Panel


VANILLA_TEXEL_DENSITY = {   # px per metre: (median, 25th, 75th percentile)
    "WEAPONS": ("Weapons", 4323, 2846, 9230),
    "CLOTHES": ("Clothes", 3057, 2159, 3799),
    "ARMOR": ("Armor", 2659, 2084, 3338),
    "CREATURES": ("Creatures", 2536, 1082, 2578),
    "FURNITURE": ("Furniture", 1019, 562, 1869),
    "PLANTS": ("Plants", 976, 511, 1976),
    "PROPS": ("Props / clutter", 709, 490, 1120),
    "ARCHITECTURE": ("Architecture", 544, 280, 649),
    "ROCKS": ("Rocks", 358, 337, 670),
}

CHECKER_MAT = "FO4_UVChecker"
_SAVED_KEY = "fo4_uvkit_saved_materials"


# ─────────────────────────────────────────────────────────────────────────────
# Texel density
# ─────────────────────────────────────────────────────────────────────────────

def material_texture_size(obj):
    """Width, height of the diffuse (or first) image in the object's
    materials, or None."""
    best = None
    for slot in obj.material_slots:
        mat = slot.material
        if not mat or not mat.use_nodes:
            continue
        imgs = [n for n in mat.node_tree.nodes if n.type == 'TEX_IMAGE' and n.image
                and n.image.size[0] > 0]
        if not imgs:
            continue
        diff = next((n for n in imgs if "diff" in (n.name + n.label + n.image.name).lower()
                     or n.image.name.lower().endswith(("_d.dds", "_d.png", "_d.tga"))), imgs[0])
        return tuple(diff.image.size)
    return best


def uv_islands(bm, faces, uv):
    """Group *faces* into UV islands (faces joined by an edge whose two UV
    ends match on both sides)."""
    faces = list(faces)
    fset = set(faces)
    parent = {f: f for f in faces}

    def find(f):
        while parent[f] is not f:
            parent[f] = parent[parent[f]]
            f = parent[f]
        return f

    for f in faces:
        for l in f.loops:
            other = l.link_loop_radial_next
            g = other.face
            if g is f or g not in fset:
                continue
            # l runs v1->v2 in f; in g the matching loop runs v2->v1
            if ((l[uv].uv - other.link_loop_next[uv].uv).length < 1e-5 and
                    (l.link_loop_next[uv].uv - other[uv].uv).length < 1e-5):
                parent[find(f)] = find(g)
    groups = {}
    for f in faces:
        groups.setdefault(find(f), []).append(f)
    return list(groups.values())


def _areas(faces, uv, matrix):
    a3 = auv = 0.0
    for f in faces:
        co = [matrix @ l.vert.co for l in f.loops]
        uvs = [l[uv].uv for l in f.loops]
        for i in range(1, len(co) - 1):
            a3 += ((co[i] - co[0]).cross(co[i + 1] - co[0])).length * 0.5
            u1, u2 = uvs[i] - uvs[0], uvs[i + 1] - uvs[0]
            auv += abs(u1.x * u2.y - u1.y * u2.x) * 0.5
    return a3, auv


def density_px_per_m(a3, auv, w, h):
    if a3 <= 0 or auv <= 0:
        return 0.0
    return math.sqrt(auv * w * h / a3)


def _scene_tex_size(context):
    s = int(getattr(context.scene, "fo4_uvkit_tex_size", "2048"))
    return s, s


def _get_bm(obj):
    if obj.mode == 'EDIT':
        return bmesh.from_edit_mesh(obj.data), True
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    return bm, False


class FO4_OT_UVKitTexelGet(Operator):
    """Measure the texel density (texture pixels per metre) of the selected
    faces, or the whole mesh, and compare it with vanilla Fallout 4"""
    bl_idname = "fo4.uvkit_texel_get"
    bl_label = "Measure Texel Density"

    @classmethod
    def poll(cls, context):
        o = context.active_object
        return o is not None and o.type == 'MESH' and o.data.uv_layers

    def execute(self, context):
        obj = context.active_object
        bm, editing = _get_bm(obj)
        uv = bm.loops.layers.uv.active
        faces = [f for f in bm.faces if f.select] if editing else list(bm.faces)
        if not faces:
            faces = list(bm.faces)
        size = material_texture_size(obj)
        src = "material texture"
        if not size:
            size, src = _scene_tex_size(context), "Texture Size setting"
        a3, auv = _areas(faces, uv, obj.matrix_world)
        if not editing:
            bm.free()
        d = density_px_per_m(a3, auv, *size)
        key = context.scene.fo4_uvkit_category
        name, med, lo, hi = VANILLA_TEXEL_DENSITY[key]
        ratio = d / med if med else 0
        context.scene.fo4_uvkit_measured = d
        verdict = ("in the vanilla range" if lo <= d <= hi else
                   "blurrier than most vanilla" if d < lo else "sharper than most vanilla")
        self.report({'INFO'}, f"{d:,.0f} px/m at {size[0]}x{size[1]} ({src}); vanilla {name} median "
                              f"{med:,} ({ratio:.2f}x): {verdict}")
        return {'FINISHED'}


class FO4_OT_UVKitTexelSet(Operator):
    """Scale UV islands so the selected faces (or the whole mesh) reach the
    target texel density. Each island is scaled about its own centre"""
    bl_idname = "fo4.uvkit_texel_set"
    bl_label = "Set Texel Density"
    bl_options = {'REGISTER', 'UNDO'}

    target: FloatProperty(name="Target px/m", default=0.0, min=0.0,
                          description="0 uses the vanilla median of the chosen category")

    @classmethod
    def poll(cls, context):
        o = context.active_object
        return o is not None and o.type == 'MESH' and o.data.uv_layers

    def execute(self, context):
        obj = context.active_object
        target = self.target or VANILLA_TEXEL_DENSITY[context.scene.fo4_uvkit_category][1]
        size = material_texture_size(obj) or _scene_tex_size(context)
        bm, editing = _get_bm(obj)
        uv = bm.loops.layers.uv.active
        faces = [f for f in bm.faces if f.select] if editing else list(bm.faces)
        if not faces:
            faces = list(bm.faces)
        islands = uv_islands(bm, faces, uv)
        scaled = 0
        for isl in islands:
            a3, auv = _areas(isl, uv, obj.matrix_world)
            d = density_px_per_m(a3, auv, *size)
            if d <= 0:
                continue
            k = target / d
            loops = [l for f in isl for l in f.loops]
            c = sum((l[uv].uv for l in loops), loops[0][uv].uv * 0) / len(loops)
            for l in loops:
                l[uv].uv = c + (l[uv].uv - c) * k
            scaled += 1
        if editing:
            bmesh.update_edit_mesh(obj.data)
        else:
            bm.to_mesh(obj.data)
            bm.free()
            obj.data.update()
        self.report({'INFO'}, f"Scaled {scaled} UV island(s) to {target:,.0f} px/m at {size[0]}x{size[1]}. "
                              "Islands may now overlap or leave 0-1: pack them (UV > Pack Islands) if needed")
        return {'FINISHED'}


# ─────────────────────────────────────────────────────────────────────────────
# UV checker
# ─────────────────────────────────────────────────────────────────────────────

def _checker_material():
    mat = bpy.data.materials.get(CHECKER_MAT)
    if mat:
        return mat
    img = bpy.data.images.get(CHECKER_MAT) or bpy.data.images.new(CHECKER_MAT, 1024, 1024)
    img.generated_type = 'COLOR_GRID'
    mat = bpy.data.materials.new(CHECKER_MAT)
    mat.use_nodes = True
    nt = mat.node_tree
    tex = nt.nodes.new('ShaderNodeTexImage')
    tex.image = img
    bsdf = next(n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED')
    nt.links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])
    return mat


def checker_on(obj):
    return any(s.material and s.material.name == CHECKER_MAT for s in obj.material_slots)


class FO4_OT_UVKitChecker(Operator):
    """Show a UV checker grid on the selected meshes, or put their own
    materials back. Export refuses while the checker is on"""
    bl_idname = "fo4.uvkit_checker"
    bl_label = "Toggle UV Checker"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        objs = [o for o in context.selected_objects if o.type == 'MESH']
        turn_off = any(checker_on(o) for o in objs)
        mat = None if turn_off else _checker_material()
        for o in objs:
            if turn_off:
                saved = list(o.get(_SAVED_KEY, []))
                for i, slot in enumerate(o.material_slots):
                    name = saved[i] if i < len(saved) else ""
                    slot.material = bpy.data.materials.get(name) if name else None
                if not saved:
                    o.data.materials.clear()
                if _SAVED_KEY in o:
                    del o[_SAVED_KEY]
            else:
                o[_SAVED_KEY] = [s.material.name if s.material else "" for s in o.material_slots]
                if not o.material_slots:
                    o.data.materials.append(mat)
                for slot in o.material_slots:
                    slot.material = mat
        self.report({'INFO'}, f"UV checker {'off' if turn_off else 'on'} for {len(objs)} mesh(es)")
        return {'FINISHED'}


# ─────────────────────────────────────────────────────────────────────────────
# Bakes: curvature and ID maps for texture painting
# ─────────────────────────────────────────────────────────────────────────────

def _bake_emit(context, obj, size, build_material, image_name):
    """Bake an emission-only material made by *build_material(slot_index,
    original_material)* into a new image; restores everything it changes."""
    scn = context.scene
    img = bpy.data.images.get(image_name)
    if img:
        bpy.data.images.remove(img)
    img = bpy.data.images.new(image_name, size, size, alpha=False)
    saved_slots = [s.material for s in obj.material_slots]
    saved = dict(engine=scn.render.engine, samples=getattr(scn.cycles, "samples", None),
                 sel=list(context.selected_objects), active=context.view_layer.objects.active)
    bk = scn.render.bake
    saved_bake = {k: getattr(bk, k) for k in ("use_selected_to_active", "margin", "target")}
    temp = []
    try:
        if not obj.material_slots:
            obj.data.materials.append(None)
        for i, slot in enumerate(obj.material_slots):
            m = build_material(i, saved_slots[i] if i < len(saved_slots) else None)
            nt = m.node_tree
            tex = nt.nodes.new('ShaderNodeTexImage')
            tex.image = img
            nt.nodes.active = tex
            slot.material = m
            temp.append(m)
        scn.render.engine = 'CYCLES'
        scn.cycles.samples = 16
        bk.use_selected_to_active = False
        bk.target = 'IMAGE_TEXTURES'
        bk.margin = max(4, size // 256)
        for o in context.selected_objects:
            o.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        bpy.ops.object.bake(type='EMIT')
    finally:
        for i, slot in enumerate(obj.material_slots):
            slot.material = saved_slots[i] if i < len(saved_slots) else None
        if not saved_slots:
            obj.data.materials.clear()
        for m in temp:
            bpy.data.materials.remove(m)
        scn.render.engine = saved["engine"]
        if saved["samples"] is not None:
            scn.cycles.samples = saved["samples"]
        for k, v in saved_bake.items():
            setattr(bk, k, v)
        obj.select_set(False)
        for o in saved["sel"]:
            o.select_set(True)
        context.view_layer.objects.active = saved["active"]
    return img


def _save_image(img, obj):
    folder = bpy.path.abspath("//") if bpy.data.filepath else ""
    if not folder:
        img.pack()
        return "packed into the .blend (save the .blend to get a file)"
    path = os.path.join(folder, f"{img.name}.png")
    img.filepath_raw = path
    img.file_format = 'PNG'
    img.save()
    return path


class FO4_OT_UVKitBakeCurvature(Operator):
    """Bake a curvature map (edges light, cavities dark, flat grey) for
    painting wear and dirt in a texture editor"""
    bl_idname = "fo4.uvkit_bake_curvature"
    bl_label = "Bake Curvature Map"
    bl_options = {'REGISTER', 'UNDO'}

    size: EnumProperty(name="Size", default='2048', items=[(s, s, "") for s in ('512', '1024', '2048', '4096')])
    contrast: FloatProperty(name="Contrast", default=4.0, min=0.5, max=20.0)

    @classmethod
    def poll(cls, context):
        o = context.active_object
        return o is not None and o.type == 'MESH' and o.data.uv_layers and context.mode == 'OBJECT'

    def execute(self, context):
        obj = context.active_object
        contrast = self.contrast

        def build(i, _orig):
            m = bpy.data.materials.new("FO4_TmpCurvature")
            m.use_nodes = True
            nt = m.node_tree
            for n in list(nt.nodes):
                nt.nodes.remove(n)
            out = nt.nodes.new('ShaderNodeOutputMaterial')
            em = nt.nodes.new('ShaderNodeEmission')
            geo = nt.nodes.new('ShaderNodeNewGeometry')
            sub = nt.nodes.new('ShaderNodeMath'); sub.operation = 'SUBTRACT'; sub.inputs[1].default_value = 0.5
            mul = nt.nodes.new('ShaderNodeMath'); mul.operation = 'MULTIPLY'; mul.inputs[1].default_value = contrast
            add = nt.nodes.new('ShaderNodeMath'); add.operation = 'ADD'; add.inputs[1].default_value = 0.5
            add.use_clamp = True
            nt.links.new(geo.outputs['Pointiness'], sub.inputs[0])
            nt.links.new(sub.outputs[0], mul.inputs[0])
            nt.links.new(mul.outputs[0], add.inputs[0])
            nt.links.new(add.outputs[0], em.inputs['Color'])
            nt.links.new(em.outputs[0], out.inputs['Surface'])
            return m

        try:
            img = _bake_emit(context, obj, int(self.size), build, f"{obj.name}_curvature")
        except Exception as exc:
            self.report({'ERROR'}, f"Curvature bake failed: {exc}")
            return {'CANCELLED'}
        where = _save_image(img, obj)
        self.report({'INFO'}, f"Curvature map baked ({self.size}px): {where}. Pointiness follows "
                              "vertex density, so very low-poly flat areas read as grey")
        return {'FINISHED'}


class FO4_OT_UVKitBakeID(Operator):
    """Bake an ID map: one flat colour per material or per mesh part, for
    quick masking in a texture editor"""
    bl_idname = "fo4.uvkit_bake_id"
    bl_label = "Bake ID Map"
    bl_options = {'REGISTER', 'UNDO'}

    source: EnumProperty(name="Colour By", default='MATERIAL', items=[
        ('MATERIAL', "Material", "One colour per material slot"),
        ('PART', "Mesh Part", "One colour per connected piece of the mesh"),
    ])
    size: EnumProperty(name="Size", default='2048', items=[(s, s, "") for s in ('512', '1024', '2048', '4096')])

    @classmethod
    def poll(cls, context):
        o = context.active_object
        return o is not None and o.type == 'MESH' and o.data.uv_layers and context.mode == 'OBJECT'

    @staticmethod
    def _colour(i):
        # well-separated hues (golden-angle steps), full saturation
        import colorsys
        r, g, b = colorsys.hsv_to_rgb((i * 0.61803398875) % 1.0, 0.85, 1.0)
        return (r, g, b, 1.0)

    def execute(self, context):
        obj = context.active_object
        attr_name = "FO4_IDPart"
        part_attr = None
        if self.source == 'PART':
            bm = bmesh.new()
            bm.from_mesh(obj.data)
            ids, seen, n = {}, set(), 0
            for f in bm.faces:
                if f in seen:
                    continue
                stack = [f]
                seen.add(f)
                while stack:
                    g = stack.pop()
                    ids[g.index] = n
                    for e in g.edges:
                        for h in e.link_faces:
                            if h not in seen:
                                seen.add(h)
                                stack.append(h)
                n += 1
            bm.free()
            part_attr = obj.data.attributes.new(attr_name, 'FLOAT_COLOR', 'FACE')
            for p in obj.data.polygons:
                part_attr.data[p.index].color = self._colour(ids.get(p.index, 0))
        source = self.source

        def build(i, _orig):
            m = bpy.data.materials.new("FO4_TmpID")
            m.use_nodes = True
            nt = m.node_tree
            for nd in list(nt.nodes):
                nt.nodes.remove(nd)
            out = nt.nodes.new('ShaderNodeOutputMaterial')
            em = nt.nodes.new('ShaderNodeEmission')
            if source == 'PART':
                at = nt.nodes.new('ShaderNodeAttribute')
                at.attribute_name = attr_name
                nt.links.new(at.outputs['Color'], em.inputs['Color'])
            else:
                em.inputs['Color'].default_value = FO4_OT_UVKitBakeID._colour(i)
            nt.links.new(em.outputs[0], out.inputs['Surface'])
            return m

        try:
            img = _bake_emit(context, obj, int(self.size), build, f"{obj.name}_id")
        except Exception as exc:
            self.report({'ERROR'}, f"ID bake failed: {exc}")
            return {'CANCELLED'}
        finally:
            if part_attr is not None:
                obj.data.attributes.remove(obj.data.attributes[attr_name])
        where = _save_image(img, obj)
        self.report({'INFO'}, f"ID map baked by {self.source.lower()} ({self.size}px): {where}")
        return {'FINISHED'}


# ─────────────────────────────────────────────────────────────────────────────
# Automatic unwrap
# ─────────────────────────────────────────────────────────────────────────────
#
# The usual manual rules, done automatically:
#   1. unapplied object scale skews an unwrap  -> apply it first
#   2. cut along hard edges and material borders (Hard Surface style)
#   3. every island must lie flat: holes, tubes and loops are joined into one
#      outline by a cut graph; cuts go where they are least visible
#      (underneath, then at the back)
#   4. unwrap, measure stretch per island, add seams only where it stretches
#   5. pack the islands with a margin that keeps mipmaps from bleeding

def _face_islands(bm):
    """Face groups separated by seams (or non-manifold edges)."""
    seen, islands = set(), []
    for f in bm.faces:
        if f in seen:
            continue
        stack, isl = [f], []
        seen.add(f)
        while stack:
            g = stack.pop()
            isl.append(g)
            for e in g.edges:
                if e.seam or len(e.link_faces) != 2:
                    continue
                for h in e.link_faces:
                    if h not in seen:
                        seen.add(h)
                        stack.append(h)
        islands.append(isl)
    return islands


def _boundary_loops(isl):
    """Boundary loops of an island as vertex sets: its outline plus any seam
    running inside it. One loop = lies flat (if it has no handles); none = a
    closed shape; two or more = a tube."""
    fset = set(isl)
    edges = {e for f in isl for e in f.edges}
    bnd = [e for e in edges if e.seam or sum(1 for lf in e.link_faces if lf in fset) < 2]
    adj = {}
    for e in bnd:
        for v in e.verts:
            adj.setdefault(v, []).append(e)
    loops, seen = [], set()
    for v in adj:
        if v in seen:
            continue
        comp, stack = set(), [v]
        while stack:
            u = stack.pop()
            if u in comp:
                continue
            comp.add(u)
            stack.extend(e.other_vert(u) for e in adj[u])
        seen |= comp
        loops.append(comp)
    return loops


def _visibility_cost(e):
    """Edges facing up or toward the front cost more to cut."""
    n = sum((f.normal for f in e.link_faces), e.verts[0].normal * 0)
    if n.length > 1e-9:
        n.normalize()
    return 1.0 + 3.0 * max(0.0, n.z) + 1.5 * max(0.0, -n.y)


def _cheapest_path(isl, sources, targets):
    """Edges of the cheapest path through *isl* from any vertex in *sources*
    to any in *targets* (Dijkstra; cost = length x visibility)."""
    import heapq
    fset = set(isl)
    allowed = {e for f in isl for e in f.edges if not e.seam}
    dist, prev, heap = {}, {}, []
    for v in sources:
        dist[v] = 0.0
        heapq.heappush(heap, (0.0, id(v), v))
    tset = set(targets)
    while heap:
        d, _, v = heapq.heappop(heap)
        if d > dist.get(v, 1e30):
            continue
        if v in tset and v not in sources:
            path, cur = [], v
            while cur in prev:
                e = prev[cur]
                path.append(e)
                cur = e.other_vert(cur)
            return path
        for e in v.link_edges:
            if e not in allowed or not any(lf in fset for lf in e.link_faces):
                continue
            u = e.other_vert(v)
            nd = d + e.calc_length() * _visibility_cost(e)
            if nd < dist.get(u, 1e30):
                dist[u] = nd
                prev[u] = e
                heapq.heappush(heap, (nd, id(u), u))
    return []


def _cut_graph_seams(bm, only=None):
    """Seam every island just enough to lie flat as ONE piece, however many
    holes and handles (loops) it has: a spanning tree is grown across the
    faces, keeping long and visible edges uncut; the edges it does not cross
    form a cut graph, and dead-end branches of that graph are pruned away.
    What is left joins every handle and hole into one outline. Closed shapes
    with no handles get a hidden slit. Returns the number of seams added."""
    added = 0
    for isl in _face_islands(bm):
        if only is not None and not all(f in only for f in isl):
            continue
        fset = set(isl)
        inner = list({e for f in isl for e in f.edges
                      if not e.seam and len(e.link_faces) == 2
                      and all(lf in fset for lf in e.link_faces)})
        parent = {f: f for f in isl}

        def find(f):
            while parent[f] is not f:
                parent[f] = parent[parent[f]]
                f = parent[f]
            return f
        crossed = set()
        # Kruskal: maximum spanning tree, weight = length x visibility, so the
        # tree crosses (keeps uncut) the long and visible edges
        for e in sorted(inner, key=lambda e: e.calc_length() * _visibility_cost(e), reverse=True):
            a, b = e.link_faces
            ra, rb = find(a), find(b)
            if ra is not rb:
                parent[ra] = rb
                crossed.add(e)
        cut = set(inner) - crossed
        if not cut:
            continue
        on_outline = {v for f in isl for e in f.edges
                      if e.seam or sum(1 for lf in e.link_faces if lf in fset) < 2 for v in e.verts}
        deg = {}
        for e in cut:
            for v in e.verts:
                deg[v] = deg.get(v, 0) + 1
        changed = True
        while changed:
            changed = False
            for e in list(cut):
                for v in e.verts:
                    if deg.get(v, 0) == 1 and v not in on_outline:
                        cut.discard(e)
                        for u in e.verts:
                            deg[u] -= 1
                        changed = True
                        break
        for e in cut:
            e.seam = True
            added += 1
    for isl in _face_islands(bm):
        if only is not None and not all(f in only for f in isl):
            continue
        if not _boundary_loops(isl):
            verts = {v for f in isl for v in f.verts}
            low = min(verts, key=lambda v: v.co.z)
            far = max(verts, key=lambda v: (v.co - low.co).length)
            for e in _cheapest_path(isl, {low}, {far}):
                if not e.seam:
                    e.seam = True
                    added += 1
    return added


def _merge_tiny_islands(bm, max_faces=3, rounds=6):
    """Remove the seams around islands of *max_faces* faces or fewer, joining
    each to the neighbour it shares the longest seam with. Noisy meshes
    (scans, AI meshes) otherwise shatter into hundreds of one-triangle
    islands that waste texture space. Returns the number of seams removed."""
    removed = 0
    for _ in range(rounds):
        islands = _face_islands(bm)
        owner = {f: i for i, isl in enumerate(islands) for f in isl}
        changed = False
        for i, isl in enumerate(islands):
            if len(isl) > max_faces:
                continue
            shared = {}
            for f in isl:
                for e in f.edges:
                    if not e.seam or len(e.link_faces) != 2:
                        continue
                    other = next((lf for lf in e.link_faces if owner.get(lf) != i), None)
                    if other is None:
                        continue
                    shared.setdefault(owner[other], []).append(e)
            if not shared:
                continue
            best = max(shared.values(), key=lambda es: sum(e.calc_length() for e in es))
            for e in best:
                if e.seam:
                    e.seam = False
                    removed += 1
                    changed = True
        if not changed:
            break
    return removed


def _face_stretch_values(isl, uv):
    """Per-face log(UV area / surface area), minus the island's mean."""
    rows = []
    for f in isl:
        a3 = f.calc_area()
        co = [l[uv].uv for l in f.loops]
        auv = sum(abs((co[i] - co[0]).x * (co[i + 1] - co[0]).y
                      - (co[i] - co[0]).y * (co[i + 1] - co[0]).x) * 0.5
                  for i in range(1, len(co) - 1))
        if a3 <= 1e-12:
            continue
        # a face squashed to nothing in UV space is the worst stretch there
        # is; skipping it used to make a fully collapsed unwrap score 0.00
        rows.append((f, a3, math.log(max(auv, 1e-14) / a3)))
    tot = sum(a for _, a, _ in rows) or 1.0
    mean = sum(a * r for _, a, r in rows) / tot
    return [(f, r - mean) for f, _, r in rows], rows, tot


def _island_stretch(bm, uv, isl):
    """Area-weighted spread of UV-to-surface area ratio over an island
    (0 = no stretch; 0.3+ is clearly visible on a checker)."""
    vals, rows, tot = _face_stretch_values(isl, uv)
    if not rows:
        return 0.0
    return math.sqrt(sum(a * d * d for (f, d), (_, a, _) in zip(vals, rows)) / tot)


def _split_by_stretch_regions(isl, uv, ratio=1.8, min_share=0.03, min_faces=20):
    """Cut an island along the borders of connected regions whose texture
    scale is clearly off from the rest (squeezed or blown up by more than
    *ratio*), so each becomes its own island and gets unwrapped at its own
    scale. On a creature this lifts a fin off the body; cutting once through
    the worst spot never separated it (2026-11-05). Returns seams added."""
    vals, rows, _ = _face_stretch_values(isl, uv)
    if not vals:
        return 0
    lim = math.log(ratio)
    label = {f: (-1 if d < -lim else (1 if d > lim else 0)) for f, d in vals}
    fset = set(isl)
    min_n = max(min_faces, int(len(isl) * min_share))
    added, seen = 0, set()
    for f, lab in label.items():
        if lab == 0 or f in seen:
            continue
        region, stack = set(), [f]
        seen.add(f)
        while stack:
            g = stack.pop()
            region.add(g)
            for e in g.edges:
                if e.seam:
                    continue
                for h in e.link_faces:
                    if h in fset and h not in seen and label.get(h) == lab:
                        seen.add(h)
                        stack.append(h)
        if len(region) < min_n or len(region) > len(isl) * 0.8:
            continue
        for g in region:
            for e in g.edges:
                if e.seam or len(e.link_faces) != 2:
                    continue
                if (e.link_faces[0] in region) != (e.link_faces[1] in region):
                    e.seam = True
                    added += 1
    return added


def _split_through_worst(bm, uv, isl):
    """Cut a stretched island in two along one hidden seam running from its
    outline, through its most stretched spot, back to its outline."""
    vals, _, _ = _face_stretch_values(isl, uv)
    worst = max(vals, key=lambda fv: abs(fv[1]))[0]
    loops = _boundary_loops(isl)
    if not loops:
        return 0
    outline = set().union(*loops)
    start = set(worst.verts)
    p1 = _cheapest_path(isl, outline, start)
    if not p1:
        return 0
    used = {v for e in p1 for v in e.verts}
    end_v = next(iter(start & used), next(iter(start)))
    for e in p1:
        e.seam = True
    p2 = _cheapest_path(isl, {end_v}, outline - used)
    for e in p2:
        e.seam = True
    return len(p1) + len(p2)


def _collapsed_faces(obj):
    """Faces whose UVs have (almost) no area: the unwrap failed there."""
    bm = bmesh.from_edit_mesh(obj.data)
    uv = bm.loops.layers.uv.active
    n = 0
    for f in bm.faces:
        co = [l[uv].uv for l in f.loops]
        a = sum(abs((co[i] - co[0]).x * (co[i + 1] - co[0]).y - (co[i] - co[0]).y * (co[i + 1] - co[0]).x)
                for i in range(1, len(co) - 1))
        n += a < 1e-12
    return n


def _texture_warning(obj):
    """' The mesh has textures ...' when its materials use image textures:
    a new unwrap moves the islands, so images painted or baked for the old
    UVs stop lining up (a re-unwrapped squid, 2026-11-05)."""
    for slot in obj.material_slots:
        m = slot.material
        if not m or m.name == CHECKER_MAT or not m.use_nodes:
            continue
        if any(n.type == 'TEX_IMAGE' and n.image for n in m.node_tree.nodes):
            return (". The mesh's textures were made for its old UVs and no longer line up: "
                    "re-bake them (select the source, then this mesh: Re-bake from Source) or repaint")
    return ""


def _unwrap_all(obj):
    """Unwrap everything with the least-stretch method this Blender has.
    Minimum Stretch can silently squash a whole mesh flat (an AI vine mesh:
    6,376 of 6,376 faces, 2026-11-05), so collapsed faces are counted and
    Angle Based is tried too; the method that collapses fewer faces wins.
    Returns (method, collapsed faces)."""
    bpy.ops.mesh.select_all(action='SELECT')
    try:
        methods = [i.identifier for i in bpy.ops.uv.unwrap.get_rna_type().properties["method"].enum_items]
    except Exception:
        methods = ['ANGLE_BASED']
    method = 'MINIMUM_STRETCH' if 'MINIMUM_STRETCH' in methods else 'ANGLE_BASED'
    bpy.ops.uv.unwrap(method=method, margin=0.0)
    collapsed = _collapsed_faces(obj)
    if collapsed and method != 'ANGLE_BASED':
        bpy.ops.uv.unwrap(method='ANGLE_BASED', margin=0.0)
        other = _collapsed_faces(obj)
        if other < collapsed:
            return 'ANGLE_BASED', other
        bpy.ops.uv.unwrap(method=method, margin=0.0)
    return method, collapsed


class FO4_OT_UVKitAutoUnwrap(Operator):
    """Unwrap the whole mesh automatically and pack it. Hard Surface puts
    seams on hard edges; Organic makes as few and as big islands as it can.
    The mesh is stashed first"""
    bl_idname = "fo4.uvkit_auto_unwrap"
    bl_label = "Auto Unwrap"
    bl_options = {'REGISTER', 'UNDO'}

    style: EnumProperty(name="Style", default='HARD_SURFACE', items=[
        ('HARD_SURFACE', "Hard Surface", "Seams along hard edges and material borders "
                                         "(weapons, props, buildings)"),
        ('ORGANIC', "Organic: Fewest Islands", "As few and as big islands as possible, no "
                                               "hard-edge seams (roots, rocks, creatures; best "
                                               "for repainting textures)"),
    ])
    hard_angle: FloatProperty(name="Hard Edge Angle", default=60.0, min=5.0, max=180.0,
                              description="Hard Surface: cut where neighbouring faces bend more "
                                          "than this (degrees)")
    keep_seams: bpy.props.BoolProperty(name="Keep Existing Seams", default=True)
    stretch_limit: FloatProperty(name="Stretch Limit", default=0.25, min=0.05, max=2.0,
                                 description="Islands stretching more than this get extra seams")
    margin_px: IntProperty(name="Margin (px)", default=8, min=1, max=64,
                           description="Space between islands, in pixels of the texture size")
    apply_scale: bpy.props.BoolProperty(name="Apply Scale First", default=True)

    @classmethod
    def poll(cls, context):
        o = context.active_object
        return o is not None and o.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        was_edit = obj.mode == 'EDIT'
        if was_edit:
            bpy.ops.object.mode_set(mode='OBJECT')
        notes = []
        try:
            from . import fo4_mesh_shaper as _ms
            _ms.create_stash(obj, "before Auto Unwrap")
        except Exception:
            pass
        if self.apply_scale and any(abs(s - 1.0) > 1e-4 for s in obj.scale):
            if obj.data.users > 1:
                notes.append("scale not applied (mesh shared with other objects)")
            else:
                with context.temp_override(selected_editable_objects=[obj], active_object=obj, object=obj):
                    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
                notes.append("applied object scale")
        if not obj.data.uv_layers:
            obj.data.uv_layers.new(name="UVMap")

        organic = self.style == 'ORGANIC'
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        if not self.keep_seams:
            for e in bm.edges:
                e.seam = False
        hard = 0
        if not organic:
            lim = math.radians(self.hard_angle)
            for e in bm.edges:
                if len(e.link_faces) != 2 or e.seam:
                    continue
                f1, f2 = e.link_faces
                if f1.normal.angle(f2.normal, 0.0) > lim or f1.material_index != f2.material_index:
                    e.seam = True
                    hard += 1
        merged = _merge_tiny_islands(bm)
        cuts = _cut_graph_seams(bm)
        bm.to_mesh(obj.data)
        bm.free()

        bpy.ops.object.mode_set(mode='EDIT')
        method, collapsed = _unwrap_all(obj)
        relief = 0
        for rounds in range(1, 13 if organic else 4):
            ebm = bmesh.from_edit_mesh(obj.data)
            uv = ebm.loops.layers.uv.active
            bad = [isl for isl in _face_islands(ebm) if _island_stretch(ebm, uv, isl) > self.stretch_limit]
            if not bad:
                break
            if organic:
                # lift clearly squeezed / blown-up regions (fins, flaps) off
                # as their own islands; failing that, cut once through the
                # worst spot
                for isl in bad:
                    n = _split_by_stretch_regions(isl, uv)
                    relief += n if n else _split_through_worst(ebm, uv, isl)
            else:
                # cut stretched islands at a lower angle
                sub_lim = math.radians(max(15.0, self.hard_angle - 20.0 * rounds))
                for isl in bad:
                    fset = set(isl)
                    for f in isl:
                        for e in f.edges:
                            if e.seam or len(e.link_faces) != 2 or not all(lf in fset for lf in e.link_faces):
                                continue
                            a, b = e.link_faces
                            if a.normal.angle(b.normal, 0.0) > sub_lim:
                                e.seam = True
                                relief += 1
                merged += _merge_tiny_islands(ebm)
            relief += _cut_graph_seams(ebm)
            bmesh.update_edit_mesh(obj.data)
            method, collapsed = _unwrap_all(obj)
        ebm = bmesh.from_edit_mesh(obj.data)
        uv = ebm.loops.layers.uv.active
        islands = _face_islands(ebm)
        worst = max((_island_stretch(ebm, uv, i) for i in islands), default=0.0)
        size = material_texture_size(obj) or _scene_tex_size(context)
        bpy.ops.mesh.select_all(action='SELECT')
        pack_kw = dict(rotate=True, margin=self.margin_px / float(max(size)))
        try:
            if "shape_method" in bpy.ops.uv.pack_islands.get_rna_type().properties:
                pack_kw["shape_method"] = 'CONCAVE'   # tightest packing
        except Exception:
            pass
        bpy.ops.uv.pack_islands(**pack_kw)
        if not was_edit:
            bpy.ops.object.mode_set(mode='OBJECT')
        bm2, _e = _get_bm(obj)
        uv2 = bm2.loops.layers.uv.active
        a3, auv = _areas(list(bm2.faces), uv2, obj.matrix_world)
        if not _e:
            bm2.free()
        d = density_px_per_m(a3, auv, *size)
        context.scene.fo4_uvkit_measured = d
        msg = (f"Auto Unwrap {('organic' if organic else 'hard surface')} "
               f"({method.replace('_', ' ').lower()}): {len(islands)} islands; seams: "
               f"{hard} hard-edge, {cuts} to lie flat, {relief} stretch relief, "
               f"{merged} removed to join tiny islands; worst stretch {worst:.2f}; "
               f"texture used {auv * 100:.0f}%; {d:,.0f} px/m at {size[0]}px")
        tex_warn = _texture_warning(obj)
        msg += tex_warn
        if collapsed:
            msg += (f". {collapsed} faces could not be unwrapped (squashed flat): usually parts that "
                    "pass through each other; clean those up or remesh first")
        if notes:
            msg += " (" + "; ".join(notes) + ")"
        self.report({'WARNING'} if worst > self.stretch_limit or collapsed or tex_warn else {'INFO'}, msg)
        return {'FINISHED'}


# ─────────────────────────────────────────────────────────────────────────────
# Drape: two big islands, as if a sheet were laid over the top and the bottom
# ─────────────────────────────────────────────────────────────────────────────

def _raster_overlap(tris_uv, res=512):
    """Share of covered texture pixels that more than one triangle uses."""
    import numpy as np
    count = np.zeros((res, res), dtype=np.uint16)
    for a, b, c in tris_uv:
        xs = [p[0] * res for p in (a, b, c)]
        ys = [p[1] * res for p in (a, b, c)]
        x0, x1 = max(0, int(min(xs))), min(res - 1, int(max(xs)) + 1)
        y0, y1 = max(0, int(min(ys))), min(res - 1, int(max(ys)) + 1)
        if x1 < x0 or y1 < y0:
            continue
        gx, gy = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        (ax, ay), (bx, by), (cx, cy) = [(p[0] * res, p[1] * res) for p in (a, b, c)]
        d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(d) < 1e-12:
            continue
        l1 = ((by - cy) * (gx - cx) + (cx - bx) * (gy - cy)) / d
        l2 = ((cy - ay) * (gx - cx) + (ax - cx) * (gy - cy)) / d
        inside = (l1 >= 0) & (l2 >= 0) & (l1 + l2 <= 1)
        count[y0:y1 + 1, x0:x1 + 1] += inside.astype(np.uint16)
    covered = (count > 0).sum()
    return float((count > 1).sum()) / covered if covered else 0.0


_DRAPE_AXES = {
    # axis: (index, (u index, v index), the side seen unmirrored, label)
    'Z': (2, (0, 1), +1, "top / bottom"),
    'Y': (1, (0, 2), -1, "front / back"),
    'X': (0, (1, 2), +1, "right / left"),
}


def _drape_layout(bm, mw, axis, margin, v_lo=0.0, v_hi=1.0, skip=None):
    """UVs for a two-sheet drape along *axis* inside the band v_lo..v_hi:
    (per-loop UVs, steep share, triangles in UV space). Faces in *skip* get
    no drape UVs (they are unwrapped separately)."""
    a, (iu, iv), primary, _ = _DRAPE_AXES[axis]
    rot = mw.to_3x3().normalized()
    co = {v: mw @ v.co for v in bm.verts}
    us = [p[iu] for p in co.values()]
    vs = [p[iv] for p in co.values()]
    u0, v0 = min(us), min(vs)
    span = max(max(us) - u0, max(vs) - v0) or 1.0
    width = (max(us) - u0) / span
    half = 0.5 - 1.5 * margin
    steep = total = 0.0
    out, tris = {}, []
    for f in bm.faces:
        n = rot @ f.normal
        front = (n[a] >= 0.0) == (primary > 0)
        area = f.calc_area()
        total += area
        if abs(n[a]) < 0.5:
            steep += area
        pts = []
        for l in f.loops:
            p = co[l.vert]
            u = (p[iu] - u0) / span
            v = (p[iv] - v0) / span
            if not front:
                u = width - u          # mirrored: seen from the other side
            uv = ((margin + u * half) if front else (0.5 + 0.5 * margin + u * half),
                  v_lo + margin + v * (v_hi - v_lo - 2 * margin))
            if skip is None or f not in skip:
                out[l] = uv
            pts.append(uv)
        if skip is None or f not in skip:
            for i in range(1, len(pts) - 1):
                tris.append((pts[0], pts[i], pts[i + 1]))
    return out, (steep / total if total else 0.0), tris


def _drape_problem_faces(bm, mw, axis, steep_limit=0.5):
    """Faces a drape along *axis* handles badly: turned sideways (they
    streak) or hidden behind other geometry from their own sheet's side
    (they would share texture). Returns (sideways set, hidden set)."""
    from mathutils.bvhtree import BVHTree
    a, _, primary, _ = _DRAPE_AXES[axis]
    rot = mw.to_3x3().normalized()
    wbm = bm.copy()
    wbm.transform(mw)
    bvh = BVHTree.FromBMesh(wbm)
    wbm.free()
    bm.verts.ensure_lookup_table()
    size = max((mw @ v.co - mw @ bm.verts[0].co).length for v in bm.verts[:200]) or 1.0
    eps = size * 1e-4
    side, hidden = set(), set()
    for f in bm.faces:
        n = rot @ f.normal
        if abs(n[a]) < steep_limit:
            side.add(f)
            continue
        d = [0.0, 0.0, 0.0]
        d[a] = 1.0 if n[a] >= 0 else -1.0       # toward this face's own viewer
        from mathutils import Vector
        dv = Vector(d)
        c = mw @ f.calc_center_median()
        hit = bvh.ray_cast(c + dv * eps, dv)
        if hit[0] is not None:
            hidden.add(f)
    return side, hidden


def _shelf_pack(islands, box, target_scale, margin):
    """Place UV islands (lists of loops with UVs at any position) side by
    side in rows inside *box* (u0, v0, u1, v1), at *target_scale* or smaller
    if they do not fit. Returns the scale used."""
    u0, v0, u1, v1 = box
    info = []
    for isl in islands:
        us = [l.uv.x for l in isl]; vs = [l.uv.y for l in isl]
        info.append((isl, min(us), min(vs), max(us) - min(us), max(vs) - min(vs)))
    info.sort(key=lambda r: -r[4])
    scale = target_scale
    for _ in range(40):
        x, y, row_h, ok = u0, v0, 0.0, True
        for _isl, _a, _b, w, h in info:
            w, h = w * scale, h * scale
            if x + w > u1 and x > u0:
                x, y, row_h = u0, y + row_h + margin, 0.0
            if y + h > v1 or w > u1 - u0:
                ok = False
                break
            x += w + margin
            row_h = max(row_h, h)
        if ok:
            break
        scale *= 0.85
    x, y, row_h = u0, v0, 0.0
    for isl, mu, mv, w, h in info:
        sw, sh = w * scale, h * scale
        if x + sw > u1 and x > u0:
            x, y, row_h = u0, y + row_h + margin, 0.0
        for l in isl:
            l.uv = (x + (l.uv.x - mu) * scale, y + (l.uv.y - mv) * scale)
        x += sw + margin
        row_h = max(row_h, sh)
    return scale


class FO4_OT_UVKitDrape(Operator):
    """Give the mesh two big UV islands, as if one sheet were draped over one
    side and another over the opposite side: top / bottom, front / back or
    right / left. Works on any mesh, even a broken one, because nothing is
    flattened. Where parts lie behind each other they share texture, and
    surfaces facing sideways stretch; the report says how much. Auto tries
    all three directions and keeps the one with the least of both"""
    bl_idname = "fo4.uvkit_drape"
    bl_label = "Drape Unwrap"
    bl_options = {'REGISTER', 'UNDO'}

    axis: EnumProperty(name="Sheets", default='AUTO', items=[
        ('AUTO', "Auto", "Try all three and keep the one with the least shared texture and stretch"),
        ('Z', "Top / Bottom", "For flat-lying meshes (roots, rugs, rubble)"),
        ('Y', "Front / Back", "For tall meshes seen mostly from the front"),
        ('X', "Right / Left", "For tall meshes seen mostly from the side"),
    ])
    margin_px: IntProperty(name="Margin (px)", default=16, min=0, max=128)
    fix_problems: bpy.props.BoolProperty(
        name="Touch Up Problem Areas", default=True,
        description="Take faces that would streak (turned sideways) or share texture (hidden "
                    "behind other parts) out of the two sheets and unwrap them on their own "
                    "in a strip along the bottom of the texture")
    min_region: IntProperty(
        name="Smallest Touch-Up", default=40, min=1, max=10000,
        description="Only touch up problem areas of at least this many connected faces; smaller "
                    "spots stay in the sheets (avoids hundreds of tiny islands)")

    @classmethod
    def poll(cls, context):
        o = context.active_object
        return o is not None and o.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        was_edit = obj.mode == 'EDIT'
        if was_edit:
            bpy.ops.object.mode_set(mode='OBJECT')
        try:
            from . import fo4_mesh_shaper as _ms
            _ms.create_stash(obj, "before Drape Unwrap")
        except Exception:
            pass
        if not obj.data.uv_layers:
            obj.data.uv_layers.new(name="UVMap")
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        uv = bm.loops.layers.uv.verify()
        size = material_texture_size(obj) or _scene_tex_size(context)
        m = self.margin_px / float(max(size))
        axes = ['Z', 'Y', 'X'] if self.axis == 'AUTO' else [self.axis]
        results = []
        for ax in axes:
            layout_uv, steep, tris = _drape_layout(bm, obj.matrix_world, ax, m)
            overlap = _raster_overlap(tris, res=384 if len(axes) > 1 else 512)
            results.append((overlap + steep, ax, layout_uv, steep, overlap))
        _, ax, layout_uv, steep, overlap = min(results, key=lambda r: r[0])
        problems, touch = set(), ""
        if self.fix_problems:
            side, hidden = _drape_problem_faces(bm, obj.matrix_world, ax)
            problems = side | hidden
            # only pull out problem regions big enough to paint: scattered
            # single faces become thousands of one-triangle islands (an AI
            # vine tangle, 2026-11-05), so small regions stay in the sheets
            kept, seen = set(), set()
            for f in problems:
                if f in seen:
                    continue
                region, stack = [], [f]
                seen.add(f)
                while stack:
                    g = stack.pop()
                    region.append(g)
                    for e in g.edges:
                        for h in e.link_faces:
                            if h in problems and h not in seen:
                                seen.add(h)
                                stack.append(h)
                if len(region) >= self.min_region:
                    kept.update(region)
            side &= kept
            hidden &= kept
            problems = kept
            area_all = sum(f.calc_area() for f in bm.faces) or 1.0
            share = sum(f.calc_area() for f in problems) / area_all
            # share the texture by surface area, so the touched-up parts end
            # up as sharp as the sheets (a tangle can be mostly touch-ups)
            strip = min(0.85, max(0.12, share))
            layout_uv, _s, tris = _drape_layout(bm, obj.matrix_world, ax, m, v_lo=strip, v_hi=1.0,
                                                skip=problems)
            overlap = _raster_overlap(tris, res=512)
            touch = (f" | touched up {len(problems)} faces ({len(side)} sideways, {len(hidden)} hidden; "
                     f"{share * 100:.0f}% of the surface) into the bottom {strip * 100:.0f}% of the texture")
        for l, val in layout_uv.items():
            l[uv].uv = val
        a = _DRAPE_AXES[ax][0]
        rot = obj.matrix_world.to_3x3().normalized()
        primary = _DRAPE_AXES[ax][2]
        seams = 0
        for e in bm.edges:
            if len(e.link_faces) == 2:
                # touched-up faces are unwrapped on their own: a front/back
                # seam inside them (sideways faces flip sides constantly)
                # chopped them into hundreds of tiny islands (a flower,
                # 2026-11-05), so this seam only goes between sheet faces
                if problems and any(f in problems for f in e.link_faces):
                    continue
                s1, s2 = (((rot @ f.normal)[a] >= 0.0) == (primary > 0) for f in e.link_faces)
                if s1 != s2 and not e.seam:
                    e.seam = True
                    seams += 1
        if problems:
            for e in bm.edges:
                if len(e.link_faces) == 2 and ((e.link_faces[0] in problems) != (e.link_faces[1] in problems)):
                    e.seam = True
                    seams += 1
            seams += _cut_graph_seams(bm, only=problems)
            for f in bm.faces:
                f.select = f in problems
        bm.to_mesh(obj.data)
        bm.free()
        obj.data.update()
        if problems:
            # unwrap only the problem faces, then fit them into the strip at
            # the same sharpness as the sheets
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.uv.unwrap(method='ANGLE_BASED', margin=0.0)
            ebm = bmesh.from_edit_mesh(obj.data)
            euv = ebm.loops.layers.uv.active
            pf = [f for f in ebm.faces if f.select]
            good = [f for f in ebm.faces if not f.select]
            a3g, auvg = _areas(good, euv, obj.matrix_world)
            a3p, auvp = _areas(pf, euv, obj.matrix_world)
            target = math.sqrt((auvg / a3g) / (auvp / a3p)) if a3g and a3p and auvp else 1.0
            isl_loops = [[l[euv] for f in isl for l in f.loops] for isl in uv_islands(ebm, pf, euv)]
            used = _shelf_pack(isl_loops, (m, m, 1.0 - m, strip - m), target, m)
            for f in ebm.faces:
                f.select = False
            bmesh.update_edit_mesh(obj.data)
            bpy.ops.object.mode_set(mode='OBJECT')
            if used < target * 0.99:
                touch += f" (packed at {used / target * 100:.0f}% of the sheets' sharpness to fit)"
        if was_edit:
            bpy.ops.object.mode_set(mode='EDIT')
        tried = ""
        if len(results) > 1:
            tried = " | tried: " + ", ".join(
                f"{_DRAPE_AXES[r[1]][3]} {r[4] * 100:.0f}% shared / {r[3] * 100:.0f}% steep"
                for r in sorted(results, key=lambda r: r[0]))
        tex_warn = _texture_warning(obj)
        self.report({'WARNING'} if overlap > 0.15 or (steep > 0.4 and not problems) or tex_warn else {'INFO'},
                    f"Drape Unwrap ({_DRAPE_AXES[ax][3]}): 2 islands, {seams} seam edges | "
                    f"{overlap * 100:.0f}% of the used texture is shared by parts behind each other | "
                    f"{steep * 100:.0f}% of the surface faces sideways{' (touched up)' if problems else ' and will stretch'}"
                    f"{touch}{tried}{tex_warn}")
        return {'FINISHED'}


# ─────────────────────────────────────────────────────────────────────────────
# Panel
# ─────────────────────────────────────────────────────────────────────────────

class FO4_PT_UVKitPanel(_FO4SubPanel):
    """UV and texture tools"""
    bl_label = "UV & Texture Kit"
    bl_idname = "FO4_PT_uv_kit_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Fallout 4'
    bl_parent_id = "FO4_PT_texture_panel"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        scn = context.scene
        box = layout.box()
        box.label(text="Texel density (sharpness vs vanilla)", icon='TEXTURE')
        box.prop(scn, "fo4_uvkit_category", text="Match")
        box.prop(scn, "fo4_uvkit_tex_size", text="Texture size if no image")
        row = box.row(align=True)
        row.operator("fo4.uvkit_texel_get", icon='DRIVER_DISTANCE')
        row.operator("fo4.uvkit_texel_set", icon='FULLSCREEN_ENTER').target = 0.0
        if scn.fo4_uvkit_measured:
            box.label(text=f"Last measured: {scn.fo4_uvkit_measured:,.0f} px/m")

        box = layout.box()
        box.label(text="Unwrap", icon='UV')
        row = box.row(align=True)
        row.operator("fo4.uvkit_auto_unwrap", text="Auto Unwrap", icon='UV_DATA').style = 'HARD_SURFACE'
        row.operator("fo4.uvkit_auto_unwrap", text="Fewest Islands", icon='UV_ISLANDSEL').style = 'ORGANIC'
        box.operator("fo4.uvkit_drape", text="Drape (2 big islands)", icon='MOD_CLOTH').axis = 'AUTO'
        box.operator("fo4.rebake_from_source", text="Re-bake from Source (after new UVs)", icon='RENDER_STILL')

        box = layout.box()
        box.label(text="Preview", icon='UV')
        box.operator("fo4.uvkit_checker", icon='TEXTURE_DATA')

        box = layout.box()
        box.label(text="Bake maps for texture painting", icon='RENDER_STILL')
        box.operator("fo4.uvkit_bake_curvature", icon='MOD_SMOOTH')
        row = box.row(align=True)
        row.operator("fo4.uvkit_bake_id", text="ID by Material").source = 'MATERIAL'
        row.operator("fo4.uvkit_bake_id", text="ID by Part").source = 'PART'


classes = (
    FO4_OT_UVKitTexelGet,
    FO4_OT_UVKitTexelSet,
    FO4_OT_UVKitChecker,
    FO4_OT_UVKitBakeCurvature,
    FO4_OT_UVKitBakeID,
    FO4_OT_UVKitAutoUnwrap,
    FO4_OT_UVKitDrape,
    FO4_PT_UVKitPanel,
)


def register():
    bpy.types.Scene.fo4_uvkit_category = EnumProperty(
        name="Vanilla Category", items=_density_items_static(),
        default="PROPS", description="Vanilla category to match")
    bpy.types.Scene.fo4_uvkit_tex_size = EnumProperty(
        name="Texture Size", default='2048',
        items=[(s, s, "") for s in ('256', '512', '1024', '2048', '4096')])
    bpy.types.Scene.fo4_uvkit_measured = FloatProperty(default=0.0)
    for cls in classes:
        try:
            bpy.utils.register_class(cls)
        except Exception:
            existing = getattr(bpy.types, cls.__name__, None)
            if existing is not None:
                bpy.utils.unregister_class(existing)
            bpy.utils.register_class(cls)


def _density_items_static():
    return [(k, f"{v[0]} ({v[1]:,} px/m)", f"Vanilla median {v[1]:,} px/m "
             f"(middle half of vanilla meshes: {v[2]:,} to {v[3]:,})")
            for k, v in VANILLA_TEXEL_DENSITY.items()]


def unregister():
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
    for p in ("fo4_uvkit_category", "fo4_uvkit_tex_size", "fo4_uvkit_measured"):
        try:
            delattr(bpy.types.Scene, p)
        except Exception:
            pass
