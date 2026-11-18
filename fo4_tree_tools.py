"""Tree tools: tiling-texture projection onto UVs, flat leaf cards for AI trees, vanilla tree wind.

Built from GSFungusStock04 (Glowing Sea AI tree, 2026-11-13/14), checked against Bethesda's pre-war trees
(Meshes\\Landscape\\Trees\\Chargen):

* Project Tiling Texture: a single tiling image (bark) cannot be lined up by hand on a layout of hundreds
  of UV islands. It is projected onto the mesh in 3D (box projection, blended, at a real-world tile size)
  and baked into the active UVs, with a normal map made from its brightness and a low specular map in the
  vanilla bark range (pre-war atlases: specular 14-16, smoothness 11-40 of 255; an AI set measured 152/152).
* Leaf Cards: AI leaves are thin closed solids (top skin, bottom skin, edges, several pieces); the wind
  shader moves and times every vertex by its own alpha, so they "breathe". They are replaced by flat
  alpha-cut cards from a card sheet (one card per leaf cluster, facing like the old leaves, base at the
  nearest twig).
* Vanilla tree wind: on the pre-war trees the 3D wood is alpha 0 everywhere; each leaf cluster ramps 0 at
  the branch to 1.0 at its tip.
"""
import math
import os

import bpy
import bmesh
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import Operator

try:
    from .ui_panels import _FO4SubPanel
except Exception:                                   # pragma: no cover
    from bpy.types import Panel as _FO4SubPanel

GAME_UNITS_PER_METRE = 69.99125
OUT_ROOT = os.path.join(os.path.expanduser("~"), "Downloads", "FO4 Add-on Textures")


def _game_units_per_bu(obj):
    """Game units per Blender unit for *obj*: the add-on's own import tag, else a mesh under 100 units
    tall is taken as imported in metres (PyNifly's scale correction), else game units."""
    tag = obj.get("fo4_unit_scale")
    if tag:
        return float(tag), "import tag"
    if max(obj.dimensions) < 100.0:
        return GAME_UNITS_PER_METRE, "metres (mesh under 100 units)"
    return 1.0, "game units"


def _wind_write(obj, values):
    """Per-vertex wind (0..1) into the Wind vertex group and the VERTEX_ALPHA attribute PyNifly exports."""
    me = obj.data
    vg = obj.vertex_groups.get("Wind") or obj.vertex_groups.new(name="Wind")
    for i, w in enumerate(values):
        vg.add([i], max(0.001, float(w)), 'REPLACE')
    va = me.color_attributes.get("VERTEX_ALPHA")
    if va is None or va.domain != 'POINT':
        if va is not None:
            me.color_attributes.remove(va)
        va = me.color_attributes.new("VERTEX_ALPHA", 'FLOAT_COLOR', 'POINT')
    for i, w in enumerate(values):
        va.data[i].color = (float(w), float(w), float(w), 1.0)
    col = me.color_attributes.get("Col") or me.color_attributes.new("Col", 'FLOAT_COLOR', 'POINT')
    if col.domain == 'POINT':
        for d in col.data:
            r, g, b, _a = d.color
            d.color = (r, g, b, 1.0)
    obj["fo4_wind_type"] = "TREE"
    # Added 2026-11-17 (GSFungusTree04: bark vanished in game after Tree Wind). The general wind button marks
    # the material as a Tree material and the object as vegetation; these buttons did not, so the export saw
    # no wind (no BSLeafAnimNode root, no Tree_Anim) and kept the shared .bgsm with Tree off -- where vertex
    # alpha is transparency, so the still (alpha 0) bark was cut away. With the mark, export gives a Tree-off
    # material its own <name>_Wind.bgsm with Tree on (bgsm_helpers.ensure_wind_material).
    obj["fo4_object_type"] = "VEGETATION"
    if "fo4_no_wind" in obj:
        del obj["fo4_no_wind"]
    for _slot in getattr(obj, "material_slots", []):
        if _slot.material is not None:
            _slot.material["fo4_bgsm_tree"] = True


# ── Project Tiling Texture ───────────────────────────────────────────────────────────────────────────

def _blur3(a):
    p = np.pad(a, 1, mode='edge')
    return (p[:-2, :-2] + 2 * p[:-2, 1:-1] + p[:-2, 2:] + 2 * p[1:-1, :-2] + 4 * p[1:-1, 1:-1]
            + 2 * p[1:-1, 2:] + p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:]) / 16.0


def _sobel(a):
    p = np.pad(a, 1, mode='edge')
    gx = (p[:-2, 2:] + 2 * p[1:-1, 2:] + p[2:, 2:] - p[:-2, :-2] - 2 * p[1:-1, :-2] - p[2:, :-2]) / 8.0
    gy = (p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:] - p[:-2, :-2] - 2 * p[:-2, 1:-1] - p[:-2, 2:]) / 8.0
    return gx, gy


def _img_array(img):
    w, h = img.size
    a = np.empty(w * h * 4, np.float32)
    img.pixels.foreach_get(a)
    return a.reshape(h, w, 4)[::-1]                  # top row first (DDS orientation)


def _save_rgba(px_top_first, path):
    h, w = px_top_first.shape[:2]
    img = bpy.data.images.new(os.path.splitext(os.path.basename(path))[0], w, h, alpha=True)
    img.colorspace_settings.name = 'Non-Color'
    img.pixels.foreach_set(np.ascontiguousarray(px_top_first[::-1]).astype(np.float32).ravel())
    img.filepath_raw = path
    img.file_format = 'PNG'
    img.save()
    bpy.data.images.remove(img)


def _to_dds(png, dds, fmt):
    from . import nvtt_helpers as _nv
    os.makedirs(os.path.dirname(dds), exist_ok=True)
    try:
        ok, msg = _nv.NVTTHelpers.convert_to_dds(png, dds, compression_format=fmt)
    except Exception as exc:
        ok, msg = False, str(exc)
    return bool(ok and os.path.isfile(dds)), msg


class FO4_OT_ProjectTilingTexture(Operator):
    """Project a tiling texture (bark, rock, ...) onto the mesh in 3D and bake it into its UVs, with a
    matching normal map and a low specular map, saved as DDS"""
    bl_idname = "fo4.project_tiling_texture"
    bl_label = "Project Tiling Texture onto UVs"
    bl_options = {'REGISTER', 'UNDO'}

    filepath: StringProperty(name="Tiling Image", subtype='FILE_PATH',
                             description="A seamless tiling image, e.g. bark")
    tile_size: FloatProperty(name="Tile Size (game units)", default=120.0, min=1.0, max=10000.0,
                             description="How big one copy of the image is on the mesh. 120 = about 1.7 m, "
                                         "bark on a 14 m tree")
    resolution: EnumProperty(name="Texture Size", default='4096',
                             items=[(s, s, "") for s in ('1024', '2048', '4096')])
    normal_spread: FloatProperty(name="Bump Strength", default=45.0, min=0.0, max=90.0,
                                 description="Normal map strength as its red-channel spread (vanilla pre-war "
                                             "bark 50-55; 0 = flat)")
    base_name: StringProperty(name="File Name", default="",
                              description="Name for the _d/_n/_s files (default: the object's name)")

    @classmethod
    def poll(cls, context):
        o = context.active_object
        return o is not None and o.type == 'MESH' and len(o.data.uv_layers) > 0

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        obj = context.active_object
        if not self.filepath or not os.path.isfile(bpy.path.abspath(self.filepath)):
            self.report({'ERROR'}, "Pick the tiling image first")
            return {'CANCELLED'}
        if obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        size = int(self.resolution)
        gu, how = _game_units_per_bu(obj)
        tile_bu = self.tile_size / gu
        name = bpy.path.clean_name(self.base_name or obj.name.split(":")[0])
        out_dir = os.path.join(OUT_ROOT, "Textures", name)
        os.makedirs(out_dir, exist_ok=True)
        sc = context.scene
        saved = (sc.render.engine, list(obj.data.materials))
        src = bpy.data.images.load(bpy.path.abspath(self.filepath), check_existing=True)
        results = {}
        for x in context.view_layer.objects:
            x.select_set(x is obj)
        context.view_layer.objects.active = obj
        try:
            sc.render.engine = 'CYCLES'
            sc.cycles.samples = 1
            for kind in ('color', 'height'):
                mat = bpy.data.materials.new(f"_fo4_project_{kind}")
                mat.use_nodes = True
                nt = mat.node_tree
                nt.nodes.clear()
                tc = nt.nodes.new('ShaderNodeTexCoord')
                mp = nt.nodes.new('ShaderNodeMapping')
                s = max(obj.matrix_world.to_scale()) / tile_bu
                mp.inputs['Scale'].default_value = (s, s, s)
                tx = nt.nodes.new('ShaderNodeTexImage')
                tx.image = src
                tx.projection = 'BOX'
                tx.projection_blend = 0.3
                nt.links.new(tc.outputs['Object'], mp.inputs['Vector'])
                nt.links.new(mp.outputs['Vector'], tx.inputs['Vector'])
                em = nt.nodes.new('ShaderNodeEmission')
                out = nt.nodes.new('ShaderNodeOutputMaterial')
                if kind == 'height':
                    bw = nt.nodes.new('ShaderNodeRGBToBW')
                    nt.links.new(tx.outputs['Color'], bw.inputs['Color'])
                    nt.links.new(bw.outputs['Val'], em.inputs['Color'])
                else:
                    nt.links.new(tx.outputs['Color'], em.inputs['Color'])
                nt.links.new(em.outputs['Emission'], out.inputs['Surface'])
                img = bpy.data.images.new(f"_fo4_project_{kind}", size, size, alpha=False)
                img.colorspace_settings.name = 'sRGB' if kind == 'color' else 'Non-Color'
                tgt = nt.nodes.new('ShaderNodeTexImage')
                tgt.image = img
                for n in nt.nodes:
                    n.select = False
                tgt.select = True
                nt.nodes.active = tgt
                obj.data.materials.clear()
                obj.data.materials.append(mat)
                with context.temp_override(active_object=obj, object=obj, selected_objects=[obj],
                                           selected_editable_objects=[obj]):
                    bpy.ops.object.bake(type='EMIT', margin=24, margin_type='EXTEND', use_clear=True)
                results[kind] = img
                bpy.data.materials.remove(mat)
        finally:
            sc.render.engine = saved[0]
            obj.data.materials.clear()
            for m in saved[1]:
                obj.data.materials.append(m)
        notes = []
        col = _img_array(results['color'])
        png = os.path.join(out_dir, f"{name}_d.png")
        results['color'].filepath_raw = png
        results['color'].file_format = 'PNG'
        results['color'].save()
        h = _img_array(results['height'])[..., 0].astype(np.float32)
        h = _blur3(h)
        gx, gy = _sobel(h)
        # strength picked so the red channel spreads like vanilla bark (fixed search, smallest first)
        n = None
        for k in (2, 4, 6, 8, 12, 16, 24, 32, 48, 64):
            nx, ny = -gx * k * size / 512.0, -gy * k * size / 512.0
            ln = np.sqrt(nx * nx + ny * ny + 1.0)
            spread = float(np.std((nx / ln + 1) * 127.5))
            n = (nx / ln, ny / ln, 1.0 / ln)
            if spread >= self.normal_spread:
                break
        nrm = np.zeros(h.shape + (4,), np.float32)
        nrm[..., 0] = (n[0] + 1) * 0.5
        nrm[..., 1] = (n[1] + 1) * 0.5              # DirectX: green follows the image rows downward
        nrm[..., 2] = (n[2] + 1) * 0.5
        nrm[..., 3] = 1.0
        if self.normal_spread <= 0:
            nrm[..., 0] = nrm[..., 1] = 0.5
            nrm[..., 2] = 1.0
        _save_rgba(nrm, os.path.join(out_dir, f"{name}_n.png"))
        spec = np.zeros(h.shape + (4,), np.float32)
        spec[..., 0] = (9 + 14 * (1 - h)) / 255.0   # specular: low like vanilla bark
        spec[..., 1] = (14 + 30 * (1 - h)) / 255.0  # smoothness
        spec[..., 3] = 1.0
        _save_rgba(spec, os.path.join(out_dir, f"{name}_s.png"))
        for img in results.values():
            bpy.data.images.remove(img)
        made = []
        for suf, fmt in (("_d", 'bc7'), ("_n", 'bc5'), ("_s", 'bc5')):
            ok, msg = _to_dds(os.path.join(out_dir, f"{name}{suf}.png"), os.path.join(out_dir, f"{name}{suf}.dds"), fmt)
            (made if ok else notes).append(f"{name}{suf}.dds" if ok else f"{name}{suf}: {msg}")
        self.report({'INFO'} if not notes else {'WARNING'},
                    f"Projected {os.path.basename(self.filepath)} ({self.tile_size:g} game units per tile, mesh in "
                    f"{how}) into {len(made)} DDS in {out_dir}" + (f"; {'; '.join(notes)}" if notes else ""))
        obj["fo4_projected_texture_dir"] = out_dir
        return {'FINISHED'}


# ── Leaf cards ───────────────────────────────────────────────────────────────────────────────────────

def _card_rects(mat):
    """Sprite rectangles (DDS uv, v down) of the card sheet in *mat*'s colour image, from its alpha."""
    try:
        from . import fo4_lod_generator as _lg
        img = _lg.base_color_image(mat)
    except Exception:
        img = None
    if img is None or not img.size[0]:
        return [dict(u0=0.0, v0=0.0, u1=1.0, v1=1.0)]
    a = _img_array(img)[..., 3]
    G = 256
    hh, ww = a.shape
    a = a[:hh - hh % G, :ww - ww % G].reshape(G, hh // G, G, ww // G).max(axis=(1, 3)) >= 0.5
    lab = np.zeros(a.shape, np.int32)
    rects, cur = [], 0
    for y in range(G):
        for x in range(G):
            if a[y, x] and not lab[y, x]:
                cur += 1
                st = [(y, x)]
                lab[y, x] = cur
                ys, xs = [], []
                while st:
                    cy, cx = st.pop()
                    ys.append(cy); xs.append(cx)
                    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < G and 0 <= nx < G and a[ny, nx] and not lab[ny, nx]:
                            lab[ny, nx] = cur
                            st.append((ny, nx))
                if len(ys) < 0.01 * G * G:
                    continue
                y0, y1, x0, x1 = min(ys), max(ys) + 1, min(xs), max(xs) + 1
                if (y1 - y0) > 3 * (x1 - x0):
                    continue                          # stems on the sheet, not leaves
                rects.append(dict(u0=x0 / G, v0=y0 / G, u1=x1 / G, v1=y1 / G))
    return rects or [dict(u0=0.0, v0=0.0, u1=1.0, v1=1.0)]


class FO4_OT_TreeLeafCards(Operator):
    """Replace a one-piece tree's thick leaves with flat leaf cards from a card sheet material, and give it
    vanilla tree wind (wood still, each card 0 at the branch to 1 at its tip)"""
    bl_idname = "fo4.tree_leaf_cards"
    bl_label = "Replace Leaves with Leaf Cards"
    bl_options = {'REGISTER', 'UNDO'}

    card_material: StringProperty(name="Card Material",
                                  description="A material whose colour texture is a cut-out leaf sheet "
                                              "(e.g. GSBush06 .1.bgsm imported with a bush)")
    card_length: FloatProperty(name="Card Length (game units)", default=62.0, min=5.0, max=1000.0)
    density: FloatProperty(name="Fullness", default=2.0, min=0.3, max=6.0,
                           description="How much of the old leaf area the cards cover (2 = full canopy)")
    seed: IntProperty(name="Seed", default=7)

    @classmethod
    def poll(cls, context):
        o = context.active_object
        return o is not None and o.type == 'MESH'

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        lay = self.layout
        lay.prop_search(self, "card_material", bpy.data, "materials")
        lay.prop(self, "card_length")
        lay.prop(self, "density")
        lay.prop(self, "seed")

    def execute(self, context):
        from . import tree_structure as ts
        from mathutils import Vector
        from mathutils.bvhtree import BVHTree
        obj = context.active_object
        mat = bpy.data.materials.get(self.card_material)
        if mat is None:
            self.report({'ERROR'}, "Pick the card material (a cut-out leaf sheet)")
            return {'CANCELLED'}
        if obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        try:
            from . import fo4_mesh_shaper as _ms
            _ms.create_stash(obj, "before Leaf Cards")
        except Exception:
            pass
        rng = np.random.default_rng(self.seed)
        me = obj.data
        M = obj.matrix_world
        V = np.array([tuple(M @ v.co) for v in me.vertices])
        F = [tuple(p.vertices) for p in me.polygons]
        if any(len(f) != 3 for f in F):
            bm = bmesh.new(); bm.from_mesh(me); bmesh.ops.triangulate(bm, faces=bm.faces[:]); bm.to_mesh(me); bm.free()
            F = [tuple(p.vertices) for p in me.polygons]
        Fa = np.array(F)
        H = float(np.ptp(V[:, 2])) or 1.0
        z0 = V[:, 2].min()
        gu, how = _game_units_per_bu(obj)
        L_bu = self.card_length / gu
        # old wind for the cards (the twig's value where they grow) is not used: vanilla cards ramp 0..1
        fth = ts.face_thickness([tuple(v) for v in V], F, H) / H
        _c, blade, g = ts.leaf_blades(V, F, H, fth, coherence=0.6)
        blade &= V[Fa].mean(1)[:, 2] >= z0 + 0.15 * H           # flat root tips stay roots
        FN = np.cross(V[Fa[:, 1]] - V[Fa[:, 0]], V[Fa[:, 2]] - V[Fa[:, 0]])
        FA = np.linalg.norm(FN, axis=1) / 2
        FN /= np.maximum(2 * FA, 1e-12)[:, None]
        remove = blade.copy()
        # loose bits left after the blades go (thick leaf bases too)
        keep = np.nonzero(~remove)[0]
        par = {}

        def fnd(x):
            while par[x] != x:
                par[x] = par[par[x]]; x = par[x]
            return x
        for fi in keep:
            gs = [int(g[v]) for v in Fa[fi]]
            for k in gs:
                par.setdefault(k, k)
            for k in gs[1:]:
                ra, rb = fnd(gs[0]), fnd(k)
                if ra != rb:
                    par[ra] = rb
        comp = {}
        for fi in keep:
            comp.setdefault(fnd(int(g[Fa[fi][0]])), []).append(fi)
        tot = FA[keep].sum()
        for fs in comp.values():
            if FA[fs].sum() < 0.01 * tot:
                remove[fs] = True
        leaf_f = np.nonzero(remove)[0]
        if len(leaf_f) < 10:
            self.report({'WARNING'}, "No thin leaf geometry found on this mesh")
            return {'CANCELLED'}
        area1 = FA[leaf_f].sum() / 2
        K = int(np.clip(round(area1 * self.density / (L_bu * L_bu * 0.55)), 20, 1500))
        ns = max(20 * K, 4000)
        pick = rng.choice(leaf_f, size=ns, p=FA[leaf_f] / FA[leaf_f].sum())
        r1, r2 = rng.random(ns), rng.random(ns); sq = np.sqrt(r1)
        P = (1 - sq)[:, None] * V[Fa[pick, 0]] + (sq * (1 - r2))[:, None] * V[Fa[pick, 1]] + (sq * r2)[:, None] * V[Fa[pick, 2]]
        C = P[rng.choice(ns, K, replace=False)]
        for _it in range(20):
            lab = np.empty(ns, int)
            for s0 in range(0, ns, 4000):
                lab[s0:s0 + 4000] = ((P[s0:s0 + 4000, None, :] - C[None]) ** 2).sum(-1).argmin(1)
            for k in range(K):
                m = lab == k
                if m.any():
                    C[k] = P[m].mean(0)
        fc = V[Fa[leaf_f]].mean(1)
        flab = np.empty(len(leaf_f), int)
        for s0 in range(0, len(fc), 4000):
            flab[s0:s0 + 4000] = ((fc[s0:s0 + 4000, None, :] - C[None]) ** 2).sum(-1).argmin(1)
        kept_tris = Fa[~remove]
        bvh = BVHTree.FromPolygons([Vector(v) for v in V], kept_tris.tolist())
        rects = _card_rects(mat)
        trunk_xy = V[V[:, 2] < z0 + 0.3 * H][:, :2].mean(0)
        cv, cuv, ct = [], [], []
        for k in range(K):
            fs = leaf_f[flab == k]
            if not len(fs):
                continue
            acc = (FN[fs].T * FA[fs]) @ FN[fs]
            _w, vec = np.linalg.eigh(acc)
            N = vec[:, -1]
            if N[2] < 0:
                N = -N
            c = C[k]
            loc, _n, _i, dist = bvh.find_nearest(Vector(c))
            grow = c - np.array(loc) if loc is not None and dist < 2.5 * L_bu else np.r_[c[:2] - trunk_xy, 0.0]
            grow = grow - (grow @ N) * N
            if np.linalg.norm(grow) < 1e-9:
                grow = np.cross(N, [0, 0, 1.0]) if abs(N[2]) < 0.99 else np.array([1.0, 0, 0])
            grow /= np.linalg.norm(grow)
            rot = math.radians(rng.uniform(-20, 20))
            side = np.cross(N, grow)
            grow, side = math.cos(rot) * grow + math.sin(rot) * side, -math.sin(rot) * grow + math.cos(rot) * side
            q = rects[int(rng.integers(len(rects)))]
            asp = (q['u1'] - q['u0']) / max(q['v1'] - q['v0'], 1e-6)
            L = L_bu * rng.uniform(0.8, 1.2)
            W = L * asp
            base = c - grow * L * 0.35
            fold = math.radians(rng.uniform(10, 22))
            o = len(cv)
            for t in (0.0, 0.5, 1.0):
                for x in (-0.5, 0.0, 0.5):
                    cv.append(base + grow * (t * L) + side * (x * W) - N * abs(x) * W * math.tan(fold))
                    cuv.append((q['u0'] + (x + 0.5) * (q['u1'] - q['u0']), 1.0 - (q['v1'] - t * (q['v1'] - q['v0']))))
            for rr in range(2):
                for cc in range(2):
                    i0 = o + rr * 3 + cc
                    ct += [(i0, i0 + 1, i0 + 4), (i0, i0 + 4, i0 + 3)]
        # remove the old leaves from the tree, give the wood vanilla wind (still)
        bm = bmesh.new(); bm.from_mesh(me); bm.faces.ensure_lookup_table()
        bmesh.ops.delete(bm, geom=[bm.faces[i] for i in leaf_f], context='FACES_ONLY')
        bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context='VERTS')
        bm.to_mesh(me); bm.free()
        _wind_write(obj, [0.0] * len(me.vertices))
        # the cards: one object, world placement, card material, wind 0 base / 0.5 middle / 1 tip
        cme = bpy.data.meshes.new(f"{obj.name}_LeafCards")
        cme.from_pydata([tuple(p) for p in cv], [], ct)
        uvl = cme.uv_layers.new(name="UVMap")
        for poly in cme.polygons:
            for li in poly.loop_indices:
                uvl.data[li].uv = cuv[cme.loops[li].vertex_index]
        cme.materials.append(mat)
        cobj = bpy.data.objects.new(f"{obj.name}_LeafCards", cme)
        for coll in obj.users_collection:
            coll.objects.link(cobj)
        _wind_write(cobj, [(i % 9) // 3 / 2.0 for i in range(len(cv))])
        cobj["fo4_leaf_cards"] = True
        cobj["PYN_GAME"] = "FO4"
        self.report({'INFO'}, f"{len(leaf_f)} leaf faces removed; {len(cv) // 9} leaf cards ({len(ct)} triangles) "
                              f"in '{cobj.name}' using {mat.name} ({len(rects)} leaf shapes on the sheet); "
                              f"wood wind 0, cards 0 at the branch to 1 at the tip. Mesh in {how}")
        return {'FINISHED'}


class FO4_OT_TreeWindVanilla(Operator):
    """Vanilla tree wind: the active mesh (trunk and branches) stays still (0); every other selected mesh
    is leaf cards, each card ramping 0 where it is nearest the trunk mesh to 1 at its far end"""
    bl_idname = "fo4.tree_wind_vanilla"
    bl_label = "Tree Wind: Wood Still, Cards Sway"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        o = context.active_object
        return o is not None and o.type == 'MESH'

    def execute(self, context):
        from mathutils import Vector
        from mathutils.bvhtree import BVHTree
        wood = context.active_object
        cards = [o for o in context.selected_objects if o.type == 'MESH' and o is not wood]
        _wind_write(wood, [0.0] * len(wood.data.vertices))
        dg = context.evaluated_depsgraph_get()
        bvh = BVHTree.FromObject(wood, dg)
        inv = wood.matrix_world.inverted()
        n_cards = 0
        for co in cards:
            me = co.data
            W = np.array([tuple(co.matrix_world @ v.co) for v in me.vertices])
            par = list(range(len(W)))

            def fnd(x):
                while par[x] != x:
                    par[x] = par[par[x]]; x = par[x]
                return x
            for e in me.edges:
                a, b = fnd(e.vertices[0]), fnd(e.vertices[1])
                if a != b:
                    par[a] = b
            groups = {}
            for i in range(len(W)):
                groups.setdefault(fnd(i), []).append(i)
            vals = np.zeros(len(W))
            for ids in groups.values():
                ids = np.array(ids)
                dist = []
                for i in ids:
                    loc, _n, _x, _d = bvh.find_nearest(inv @ Vector(W[i]))
                    dist.append((wood.matrix_world @ loc - Vector(W[i])).length if loc is not None else 0.0)
                base = W[ids[int(np.argmin(dist))]]
                d = np.linalg.norm(W[ids] - base, axis=1)
                vals[ids] = d / d.max() if d.max() > 0 else 0.0
                n_cards += 1
            _wind_write(co, vals.tolist())
        self.report({'INFO'}, f"'{wood.name}' wind 0 (still); {n_cards} card(s) in {len(cards)} mesh(es) ramp 0 "
                              f"at the trunk side to 1 at the tip (vanilla pre-war trees)")
        return {'FINISHED'}


class FO4_PT_TreeToolsPanel(_FO4SubPanel):
    """Leaf cards, vanilla tree wind and bark projection"""
    bl_label = "Tree Tools (leaf cards, wind, bark)"
    bl_idname = "FO4_PT_tree_tools_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Fallout 4'
    bl_parent_id = "FO4_PT_vegetation_panel"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        lay = self.layout
        box = lay.box()
        box.label(text="AI tree with thick leaves", icon='OUTLINER_OB_FORCE_FIELD')
        box.operator("fo4.tree_leaf_cards", icon='MOD_PARTICLE_INSTANCE')
        box.operator("fo4.tree_wind_vanilla", icon='FORCE_WIND')
        box = lay.box()
        box.label(text="Bark (after Auto Unwrap)", icon='TEXTURE')
        box.operator("fo4.project_tiling_texture", icon='IMAGE_DATA')


classes = (FO4_OT_ProjectTilingTexture, FO4_OT_TreeLeafCards, FO4_OT_TreeWindVanilla, FO4_PT_TreeToolsPanel)


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
