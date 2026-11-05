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
