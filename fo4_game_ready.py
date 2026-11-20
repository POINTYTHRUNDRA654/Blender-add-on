"""
fo4_game_ready.py
Build Game-Ready Mesh (Vanilla Style): turns a dense AI model (Meshy, Hunyuan3D, scans) into a Fallout 4 style
low-poly mesh with baked colour and normal maps, in one click.

How vanilla does it (scan of every vanilla mesh, 2026-11-19: 41,777 NIFs, 136,721 shapes): simple shapes at a
small triangle budget, every fine detail in the textures (normal map + colour), a few large UV islands. The
budgets per kind of object below are the 10th / 50th / 90th percentile of triangles per NIF in that category.

Steps (all on copies; the selected originals are only hidden, so Ctrl+Z or unhiding brings everything back):
  1. join copies of the selected meshes in world space (the bake source)
  2. rebuild a closed, even skin (voxel remesh) - AI models are full of holes and loose scraps, which made
     QuadriFlow give up and the old fallback produce cone-shaped caps and needle triangles (Meshy mushroom test)
  3. reduce the skin to the vanilla budget for the category, raising the budget only while the shape is off by
     more than 0.2% of the model's size on average (or 0.9% at the worst 1%)
  4. unwrap with the UV & Texture Kit (Fewest Islands / Hard Surface), smooth shading
  5. bake colour and a DirectX tangent-space normal map from the original onto the new mesh
  6. save the maps next to the .blend (PNG, and DDS when NVTT/texconv is available), report against vanilla
This is a separate button: the existing Retopologize & Bake is unchanged.
"""

import os
import time
import bpy
import bmesh
import numpy as np
from mathutils.bvhtree import BVHTree
from bpy.props import BoolProperty, EnumProperty, IntProperty

try:
    from . import texture_helpers
except Exception:  # pragma: no cover
    texture_helpers = None
try:
    from . import nvtt_helpers
except Exception:  # pragma: no cover
    nvtt_helpers = None

# label, p10, p50, p90 triangles per NIF, diffuse size, unwrap style  (FO4_Mesh_Geometry_Catalog, 2026-11-19)
VANILLA = {
    'PLANT':    ("Plant / fungus / bush", 66, 490, 2054, 2048, 'ORGANIC'),
    'TREE':     ("Tree (trunk and solid parts)", 199, 868, 1668, 4096, 'ORGANIC'),
    'ROCK':     ("Rock / cliff / rubble", 199, 1712, 4639, 2048, 'ORGANIC'),
    'CREATURE': ("Creature or statue (static)", 170, 1100, 7200, 2048, 'ORGANIC'),
    'PROP':     ("Clutter / prop", 28, 360, 1988, 2048, 'HARD_SURFACE'),
    'WEAPON':   ("Weapon", 180, 600, 2000, 4096, 'HARD_SURFACE'),
    'ARMOR':    ("Armor / outfit piece", 650, 1500, 4500, 4096, 'HARD_SURFACE'),
    'ARCH':     ("Building / architecture piece", 40, 560, 2800, 2048, 'HARD_SURFACE'),
}
ERR_MEAN_MAX = 0.20   # % of model size
ERR_P99_MAX = 0.90


def _category_items(self, context):
    return [(k, v[0], f"Vanilla {v[0].lower()}: typical {v[2]} triangles (10-90% range {v[1]}-{v[3]}), "
                      f"{v[4]} px textures") for k, v in VANILLA.items()]


def _world_join_copy(context, objs):
    """Copies of objs merged into one new object in world space (originals untouched)."""
    me = bpy.data.meshes.new("FO4GR_source")
    bm = bmesh.new()
    mats = []
    for o in objs:
        tmp = bmesh.new()
        tmp.from_mesh(o.data)
        tmp.transform(o.matrix_world)
        offset = len(mats)
        for m in o.data.materials:
            mats.append(m)
        for f in tmp.faces:
            f.material_index += offset
        tm = bpy.data.meshes.new("FO4GR_tmp")
        tmp.to_mesh(tm)
        tmp.free()
        bm.from_mesh(tm)
        bpy.data.meshes.remove(tm)
    bm.to_mesh(me)
    bm.free()
    for m in mats:
        me.materials.append(m)
    ob = bpy.data.objects.new("FO4GR_source", me)
    context.scene.collection.objects.link(ob)
    return ob


def _evaluated_copy(context, ob, name, modifier_setup):
    """Apply one modifier to a copy via the depsgraph (no operator context needed)."""
    src = ob.copy()
    src.data = ob.data
    context.scene.collection.objects.link(src)
    mod = modifier_setup(src)
    dg = context.evaluated_depsgraph_get()
    dg.update()
    me = bpy.data.meshes.new_from_object(src.evaluated_get(dg))
    src.modifiers.remove(mod)
    bpy.data.objects.remove(src, do_unlink=True)
    out = bpy.data.objects.new(name, me)
    context.scene.collection.objects.link(out)
    return out


def _tri_count(me):
    return sum(len(p.vertices) - 2 for p in me.polygons)


def _tri_quality(me):
    me.calc_loop_triangles()
    V = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get('co', V)
    V = V.reshape(-1, 3)
    T = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
    me.loop_triangles.foreach_get('vertices', T)
    T = T.reshape(-1, 3)
    a, b, c = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
    area = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
    E = np.stack([np.linalg.norm(b - a, axis=1), np.linalg.norm(c - b, axis=1), np.linalg.norm(a - c, axis=1)], 1)
    lmax = E.max(1)
    h = 2 * area / np.maximum(lmax, 1e-12)
    nz = area[area > 1e-12]
    spread = float(np.percentile(nz, 95) / max(np.percentile(nz, 5), 1e-12)) if len(nz) > 10 else 0.0
    sliver = 100.0 * float(((lmax / np.maximum(h, 1e-12)) > 10).mean()) if len(T) else 0.0
    return spread, sliver


def _bvh(me):
    bm = bmesh.new()
    bm.from_mesh(me)
    t = BVHTree.FromBMesh(bm)
    bm.free()
    return t


def _shape_error(samples, size, me):
    """Mean and 99th-percentile distance from the original's surface points to the new mesh, as % of size."""
    t = _bvh(me)
    d = np.array([(t.find_nearest(p)[3] or 0.0) for p in samples])
    return 100.0 * float(d.mean()) / size, 100.0 * float(np.percentile(d, 99)) / size


def _reduce_to(context, skin, tris):
    skin_tris = max(1, _tri_count(skin.data))
    ratio = min(1.0, tris / skin_tris)

    def setup(o, r=ratio):
        m = o.modifiers.new("FO4GR_dec", 'DECIMATE')
        m.ratio = r
        return m
    lo = _evaluated_copy(context, skin, "FO4GR_low", setup)
    got = _tri_count(lo.data)
    if got and abs(got - tris) > 0.15 * tris and ratio < 1.0:      # one correction pass
        r2 = min(1.0, ratio * tris / got)
        bpy.data.objects.remove(lo, do_unlink=True)
        lo = _evaluated_copy(context, skin, "FO4GR_low", lambda o: _set_dec(o, r2))
    return lo


def _set_dec(o, r):
    m = o.modifiers.new("FO4GR_dec", 'DECIMATE')
    m.ratio = r
    return m


def _uv_islands(me):
    """Connected faces sharing UVs (same rule as the vanilla catalog: per-vertex connectivity after a UV split)."""
    if not me.uv_layers:
        return 0
    bm = bmesh.new()
    bm.from_mesh(me)
    uvl = bm.loops.layers.uv.active
    par = list(range(len(bm.faces)))

    def f(x):
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x
    bm.faces.ensure_lookup_table()
    for e in bm.edges:
        if len(e.link_faces) != 2:
            continue
        fa, fb = e.link_faces
        same = True
        for v in e.verts:
            ua = next(l[uvl].uv for l in fa.loops if l.vert == v)
            ub = next(l[uvl].uv for l in fb.loops if l.vert == v)
            if (ua - ub).length > 1e-5:
                same = False
                break
        if same:
            ra, rb = f(fa.index), f(fb.index)
            if ra != rb:
                par[ra] = rb
    n = len({f(i) for i in range(len(bm.faces))})
    bm.free()
    return n


class FO4_OT_BuildGameReady(bpy.types.Operator):
    """Build a Fallout 4 style low-poly copy of the selected AI model with baked colour and normal maps.
    The originals are only hidden: undo (Ctrl+Z) or unhide them to go back"""
    bl_idname = "fo4.build_game_ready"
    bl_label = "Build Game-Ready Mesh (Vanilla Style)"
    bl_options = {'REGISTER', 'UNDO'}

    category: EnumProperty(name="Kind of object", items=_category_items,
                           description="Sets the triangle budget, texture size and unwrap style from vanilla meshes of the same kind")
    detail: EnumProperty(name="Detail", default='AUTO',
                         items=[('AUTO', "Auto (vanilla budget, more only if the shape needs it)", ""),
                                ('VANILLA', "Vanilla typical (fewest triangles)", ""),
                                ('HIGH', "High (vanilla upper range)", ""),
                                ('CUSTOM', "Custom triangle count", "")])
    custom_tris: IntProperty(name="Triangles", default=2000, min=50, max=60000)
    skin_detail: IntProperty(name="Skin resolution", default=220, min=80, max=600,
                             description="How finely the closed skin follows the original (voxels across the model). "
                                         "Raise it for thin parts; it does not change the final triangle count")
    texture_size: EnumProperty(name="Texture size", default='CATEGORY',
                               items=[('CATEGORY', "As vanilla for this kind", ""), ('1024', "1024", ""),
                                      ('2048', "2048", ""), ('4096', "4096", "")])
    convert_to_dds: BoolProperty(name="Convert to DDS", default=True,
                                 description="Also write DDS files when NVTT or texconv is available")

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        lay = self.layout
        lay.prop(self, "category")
        v = VANILLA[self.category]
        lay.label(text=f"Vanilla {v[0].lower()}: about {v[2]} triangles ({v[1]}-{v[3]}), {v[4]} px", icon='INFO')
        lay.prop(self, "detail")
        if self.detail == 'CUSTOM':
            lay.prop(self, "custom_tris")
        lay.prop(self, "texture_size")
        lay.prop(self, "skin_detail")
        lay.prop(self, "convert_to_dds")
        lay.label(text="Your selected model is kept (hidden). Ctrl+Z undoes everything.", icon='LOOP_BACK')

    # ------------------------------------------------------------------ steps
    def _candidates(self):
        label, p10, p50, p90, _tex, _uw = VANILLA[self.category]
        if self.detail == 'CUSTOM':
            return [self.custom_tris], False
        if self.detail == 'VANILLA':
            return [p50], False
        if self.detail == 'HIGH':
            return [p90], False
        c = [p50, (p50 + p90) // 2, p90, int(p90 * 1.5), int(p90 * 2.5)]
        return sorted(set(max(p10, x) for x in c)), True

    def _unwrap(self, context, lo, style):
        bpy.ops.object.select_all(action='DESELECT')
        lo.select_set(True)
        context.view_layer.objects.active = lo
        try:
            with context.temp_override(active_object=lo, object=lo, selected_objects=[lo]):
                bpy.ops.fo4.uvkit_auto_unwrap('EXEC_DEFAULT', apply_scale=False, style=style)
            return "UV & Texture Kit"
        except Exception as e:
            print(f"[FO4 Game-Ready] UV Kit unwrap failed, using Smart UV Project: {e}")
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.uv.smart_project(angle_limit=66.0, island_margin=0.02)
            bpy.ops.object.mode_set(mode='OBJECT')
            return "Smart UV Project"

    def _bake(self, context, lo, src, size, res):
        sc = context.scene
        lo.data.materials.clear()          # the reduced skin inherits the AI model's materials; start fresh
        mat = texture_helpers.TextureHelpers.setup_fo4_material(lo) if texture_helpers else None
        if mat is None:
            mat = bpy.data.materials.new(f"{lo.name}_Material")
            mat.use_nodes = True
            lo.data.materials.append(mat)
        nodes = mat.node_tree.nodes
        dn = nodes.get("Diffuse")
        nn = nodes.get("Normal")
        if dn is None or dn.type != 'TEX_IMAGE':
            dn = nodes.new('ShaderNodeTexImage')
            dn.name = "Diffuse"
        if nn is None or nn.type != 'TEX_IMAGE':
            nn = nodes.new('ShaderNodeTexImage')
            nn.name = "Normal"
        imgs = {}
        for key, node, noncolor in (("Diffuse", dn, False), ("Normal", nn, True)):
            nm = f"{lo.name}_{key}"
            if nm in bpy.data.images:
                bpy.data.images.remove(bpy.data.images[nm])
            img = bpy.data.images.new(nm, width=res, height=res, alpha=False)
            if noncolor:
                img.colorspace_settings.name = 'Non-Color'
            node.image = img
            imgs[key] = img
        bk = sc.render.bake
        saved = {k: getattr(bk, k) for k in ("use_selected_to_active", "cage_extrusion", "max_ray_distance",
                                              "use_pass_direct", "use_pass_indirect", "use_pass_color",
                                              "normal_space", "normal_r", "normal_g", "normal_b", "margin")}
        saved_engine = sc.render.engine
        saved_samples = sc.cycles.samples
        stand_ins, slot_saved, src_img = [], [], None
        try:
            from . import fo4_lod_generator as lodg
        except Exception:
            lodg = None
        try:
            sc.render.engine = 'CYCLES'
            sc.cycles.samples = 1
            bk.use_selected_to_active = True
            # same reach as the Skin Builder bake (works on thin AI detail without grabbing the wrong surface)
            bk.cage_extrusion = 0.012 * size
            bk.max_ray_distance = 0.035 * size
            bk.margin = 16
            # plain stand-in source materials (the FO4 node group bakes black), as Retopologize & Bake does
            for slot in src.material_slots:
                slot_saved.append((slot, slot.material))
                img = lodg.base_color_image(slot.material) if (lodg and slot.material) else None
                src_img = src_img or img
                sm = bpy.data.materials.new("FO4GR_BakeSource")
                sm.use_nodes = True
                bs = next(n for n in sm.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
                if img is not None:
                    tn = sm.node_tree.nodes.new('ShaderNodeTexImage')
                    tn.image = img
                    sm.node_tree.links.new(tn.outputs['Color'], bs.inputs['Base Color'])
                slot.material = sm
                stand_ins.append(sm)
            if src_img is not None and lodg is not None:
                try:
                    fill = lodg._average_color(src_img)
                    imgs["Diffuse"].pixels.foreach_set(list(fill) * (res * res))
                except Exception as e:
                    print(f"[FO4 Game-Ready] diffuse pre-fill skipped: {e}")
            bpy.ops.object.select_all(action='DESELECT')
            src.select_set(True)
            lo.select_set(True)
            context.view_layer.objects.active = lo
            nodes.active = dn
            bk.use_pass_direct = False
            bk.use_pass_indirect = False
            bk.use_pass_color = True
            bpy.ops.object.bake(type='DIFFUSE', pass_filter={'COLOR'})
            nodes.active = nn
            bk.normal_space = 'TANGENT'
            bk.normal_r, bk.normal_g, bk.normal_b = 'POS_X', 'NEG_Y', 'POS_Z'   # DirectX, as the rest of the add-on
            bpy.ops.object.bake(type='NORMAL')
        finally:
            for slot, m in slot_saved:
                slot.material = m
            for sm in stand_ins:
                bpy.data.materials.remove(sm)
            sc.render.engine = saved_engine
            sc.cycles.samples = saved_samples
            for k, v in saved.items():
                setattr(bk, k, v)
        return imgs

    def _save_maps(self, lo, imgs):
        out = []
        blend_dir = os.path.dirname(bpy.data.filepath) if bpy.data.filepath else None
        for key, tex_type in (("Diffuse", 'DIFFUSE'), ("Normal", 'NORMAL')):
            img = imgs[key]
            if not blend_dir:
                img.pack()
                out.append(f"{key.lower()} packed into the .blend (save the .blend to get files)")
                continue
            png = os.path.join(blend_dir, f"{img.name}.png")
            img.filepath_raw = png
            img.file_format = 'PNG'
            img.save()
            note = f"{key.lower()} -> {os.path.basename(png)}"
            if self.convert_to_dds and nvtt_helpers and (nvtt_helpers.NVTTHelpers.is_nvtt_available()
                                                         or nvtt_helpers.NVTTHelpers.is_texconv_available()):
                dds = os.path.splitext(png)[0] + '.dds'
                ok, msg = nvtt_helpers.NVTTHelpers.convert_to_dds(png, dds, slot=tex_type)
                if ok and texture_helpers:
                    texture_helpers.TextureHelpers.install_texture(lo, dds, texture_type=tex_type)
                    note += " + DDS"
                elif not ok:
                    note += f" (DDS failed: {msg})"
            out.append(note)
        return out

    def execute(self, context):
        t0 = time.time()
        sel = [o for o in context.selected_objects if o.type == 'MESH']
        if not sel:
            self.report({'ERROR'}, "Select the AI model (one or more mesh objects) first")
            return {'CANCELLED'}
        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        label, p10, p50, p90, tex, style = VANILLA[self.category]
        base = sel[0].name.split('.')[0]
        made = []
        try:
            src = _world_join_copy(context, sel)
            made.append(src)
            V = np.empty(len(src.data.vertices) * 3)
            src.data.vertices.foreach_get('co', V)
            V = V.reshape(-1, 3)
            size = float(np.ptp(V, 0).max()) or 1.0
            rng = np.random.default_rng(1)
            samples = V[rng.choice(len(V), min(20000, len(V)), replace=False)]
            src_tris = _tri_count(src.data)

            def vox(o):
                m = o.modifiers.new("FO4GR_vox", 'REMESH')
                m.mode = 'VOXEL'
                m.voxel_size = size / self.skin_detail
                return m
            skin = _evaluated_copy(context, src, "FO4GR_skin", vox)
            made.append(skin)
            if _tri_count(skin.data) > 120000:          # keep the budget search quick
                pre = _reduce_to(context, skin, 120000)
                bpy.data.objects.remove(skin, do_unlink=True)
                made.remove(skin)
                skin = pre
                made.append(skin)
            cands, auto = self._candidates()
            chosen = None
            tried = []
            for tris in cands:
                lo = _reduce_to(context, skin, tris)
                em, e99 = _shape_error(samples, size, lo.data)
                tried.append((_tri_count(lo.data), em, e99))
                if chosen is not None:
                    bpy.data.objects.remove(chosen[0], do_unlink=True)
                chosen = (lo, em, e99)
                if not auto or (em <= ERR_MEAN_MAX and e99 <= ERR_P99_MAX):
                    break
            lo, em, e99 = chosen
            bpy.data.objects.remove(skin, do_unlink=True)
            made.remove(skin)
            lo.name = f"{base}_GameReady"
            lo.data.name = lo.name
            for p in lo.data.polygons:
                p.use_smooth = True
            unwrap = self._unwrap(context, lo, style)
            res = tex if self.texture_size == 'CATEGORY' else int(self.texture_size)
            imgs = self._bake(context, lo, src, size, res)
            saved = self._save_maps(lo, imgs)
            # done: originals hidden (kept), bake source removed
            bpy.data.objects.remove(src, do_unlink=True)
            made.remove(src)
            for o in sel:
                o.hide_set(True)
            bpy.ops.object.select_all(action='DESELECT')
            lo.select_set(True)
            context.view_layer.objects.active = lo
            tris = _tri_count(lo.data)
            spread, sliver = _tri_quality(lo.data)
            islands = _uv_islands(lo.data)
            for k, v in (("fo4_game_ready_category", self.category), ("fo4_game_ready_tris", tris),
                         ("fo4_game_ready_shape_err_pct", round(em, 3)), ("fo4_game_ready_source_tris", src_tris)):
                lo[k] = v
            in_range = "inside" if p10 <= tris <= p90 else ("above" if tris > p90 else "below")
            lines = [f"{lo.name}: {tris:,} triangles from {src_tris:,} ({in_range} the vanilla {label.lower()} range "
                     f"{p10}-{p90}, typical {p50})",
                     f"shape kept within {em:.2f}% of the model's size on average ({e99:.2f}% at the worst 1%)",
                     f"triangle evenness {spread:.0f} (vanilla about 8-18), needle triangles {sliver:.1f}%",
                     f"{islands} UV islands ({unwrap}), {res}px colour + normal maps: " + "; ".join(saved),
                     f"budgets tried: " + ", ".join(f"{t:,} tris {m:.2f}%" for t, m, _ in tried),
                     f"original kept and hidden; {time.time() - t0:.0f} s"]
            for ln in lines:
                print("[FO4 Game-Ready] " + ln)
            if auto and (em > ERR_MEAN_MAX or e99 > ERR_P99_MAX):
                self.report({'WARNING'}, lines[0] + " - the shape still differs more than usual; try Detail: Custom "
                                                    "with more triangles, or a higher Skin resolution for thin parts")
            else:
                self.report({'INFO'}, lines[0] + "; " + lines[1])
            return {'FINISHED'}
        except Exception as e:
            for o in made:
                try:
                    bpy.data.objects.remove(o, do_unlink=True)
                except Exception:
                    pass
            for o in sel:
                o.hide_set(False)
            self.report({'ERROR'}, f"Game-ready build failed: {e}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}


classes = (FO4_OT_BuildGameReady,)


def register():
    for cls in classes:
        try:
            bpy.utils.register_class(cls)
        except Exception:
            existing = getattr(bpy.types, cls.__name__, None)
            if existing is not None:
                bpy.utils.unregister_class(existing)
            bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
