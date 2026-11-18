"""Vanilla-style LODs for trees with leaves: a solid low-poly trunk plus leaf cards,
and a crossed billboard for the farthest level.

Measured 2026-11-13 on Bethesda's pre-war trees (Meshes\\LOD\\Landscape\\Trees\\Chargen,
TreeMaplePW01Gr / TreeElmFreePW01Gr / TreeElmFrstPW01Gr against their full trees):

* _LOD_0 and _LOD_1 are TWO shapes. The trunk is one closed low-poly piece (2-8% open
  edges): 50-158 triangles at _LOD_0, 25-53 at _LOD_1, material
  Materials\\LOD\\PreWarTreeTrunksLOD.BGSM (opaque, one-sided, 512px DXT1). The canopy
  is 20-61 separate leaf cards at _LOD_0 and 13-31 at _LOD_1 (61-150 / 41-80
  triangles, 71-77% open edges as cards are), one material shared by both levels
  (PreWarMapleGrLOD.BGSM: alpha test 105, two-sided, 512px DXT5 + BC5 normal) with an
  NiAlphaProperty (flags 4844).
* _LOD_2 is a crossed billboard: 8 vertices, 4 triangles, a picture of the whole tree
  (256px DXT5, alpha test 100-127).

Decimating a leafy tree instead (the add-on's other LOD modes) cut the leaves into
loose triangles with no solid trunk under them: GSFungusStock04's LODs had 77% / 85% /
90% open edges and looked shattered (2026-11-13).
"""
import os
import re
import math
import tempfile

import numpy as np
import bpy
import bmesh
from mathutils import Vector, Matrix

# Per add-on LOD level (LOD1 -> _LOD_0, LOD2 -> _LOD_1); trunk triangles and card
# counts sit inside the vanilla ranges above.
TREE_LOD_LEVELS = {
    "LOD1": {"trunk_tris": 160, "cards": 28, "voxel_div": 50, "trunk_tex": 512},
    "LOD2": {"trunk_tris": 60, "cards": 14, "voxel_div": 35, "trunk_tex": 256},
}
# Vanilla pre-war canopy cards (TreeMaplePW01/03, TreeElmFreePW01, TreeElmFrstPW01
# _LOD_0): 23-45% upright, 37-60% tilted, 0-28% flat; only 13-41% face straight out
# from the trunk; median card 285-394 units on trees 989-1,850 tall.
CARD_FACE_BLEND = 0.5          # card normal: half the leaves' own facing, half outward
CANOPY_ALPHA_REF = 105         # vanilla PreWarMapleGrLOD / PreWarElmGrLOD: 105 / 90
BILLBOARD_ALPHA_REF = 127      # vanilla lv2 billboards: 100-127
ALPHA_PROPERTY_FLAGS = 4844    # every vanilla LOD canopy / billboard shape
WOOD_THICK = 0.012             # of the tree height: thicker = trunk / limb / crown
ROOT_ZONE = 0.12               # thin parts below this share of the height are ground roots
CARD_TILE = 128                # pixels per card in the canopy atlas
CARD_MARGIN = 1.06             # card size over the cluster's extent


# ── analysis ──────────────────────────────────────────────────────────────────

def looks_like_tree(obj):
    """A tree or tree-sized plant: wind/collision type Tree, 'tree' in the name, or a
    model / material under a Trees folder (GSFungusStock04's object is named
    L2_gsfungusstock04:1 but lives in Meshes\\Landscape\\Trees)."""
    for key in ("fo4_wind_type",):
        if str(obj.get(key, "")).upper() == 'TREE':
            return True
    if str(getattr(obj, "fo4_collision_type", "")).upper() == 'TREE':
        return True
    # "tree" as a word, not inside another one (Street, Streetlight)
    if re.search(r"(^|[^a-z])trees?([^a-z]|$)", obj.name.lower()):
        return True
    paths = [str(obj.get("fo4_source_nif") or "")]
    for slot in getattr(obj, "material_slots", []):
        if slot.material is not None:
            paths.append(str(slot.material.get("BSLSP_Shader_Name") or ""))
    return any(re.search(r"[\\/]trees[\\/]", p.lower()) for p in paths)

def _world_mesh(obj):
    me = obj.data
    mw = obj.matrix_world
    V = np.array([tuple(mw @ v.co) for v in me.vertices], dtype=float)
    faces = [tuple(p.vertices) for p in me.polygons]
    return V, faces


def split_wood_foliage(obj):
    """Per face: 'wood' (trunk, thick limbs, crown, thick roots), 'leaf' (thin parts
    above the root zone) or 'drop' (thin ground roots, not visible at LOD distance).
    Returns (labels, info)."""
    try:
        from . import tree_structure as ts
    except ImportError:
        import tree_structure as ts
    V, faces = _world_mesh(obj)
    z0, z1 = V[:, 2].min(), V[:, 2].max()
    H = float(z1 - z0) or 1.0
    th = ts.face_thickness([tuple(v) for v in V], faces, H) / H
    cz = np.array([(V[list(f), 2].mean() - z0) / H for f in faces])
    area = np.array([_face_area(V, f) for f in faces])
    labels = np.where(th >= WOOD_THICK, "wood", np.where(cz < ROOT_ZONE, "drop", "leaf")).astype(object)
    # Tiny islands of "wood" inside the canopy (a clump of leaves the ray measured
    # as thick) belong to the canopy; keep only wood parts with a real share.
    wood_idx = np.nonzero(labels == "wood")[0]
    if len(wood_idx):
        comp = _face_components(V, faces, wood_idx, H)
        tot = area[wood_idx].sum() or 1.0
        for members in comp:
            if area[members].sum() < 0.02 * tot:
                for i in members:
                    labels[i] = "leaf" if cz[i] >= ROOT_ZONE else "drop"
    info = {"H": H, "z0": z0, "wood": int((labels == "wood").sum()),
            "leaf": int((labels == "leaf").sum()), "drop": int((labels == "drop").sum())}
    return labels, info


def _face_area(V, f):
    p = V[list(f)]
    a = 0.0
    for k in range(1, len(p) - 1):
        a += 0.5 * np.linalg.norm(np.cross(p[k] - p[0], p[k + 1] - p[0]))
    return a


def _face_components(V, faces, idx, H):
    """Connected groups of the faces *idx* (vertices at one spot count as shared)."""
    key = np.round(V / max(1e-5 * H, 1e-9)).astype(np.int64)
    _, g = np.unique(key, axis=0, return_inverse=True)
    g = g.ravel()
    par = {int(i): int(i) for i in idx}

    def find(a):
        while par[a] != a:
            par[a] = par[par[a]]
            a = par[a]
        return a
    owner = {}
    for i in idx:
        for v in faces[i]:
            j = owner.setdefault(int(g[v]), int(i))
            if j != i:
                ra, rb = find(int(i)), find(j)
                if ra != rb:
                    par[ra] = rb
    groups = {}
    for i in idx:
        groups.setdefault(find(int(i)), []).append(int(i))
    return [np.array(m) for m in groups.values()]


def part_object(src, keep_faces, name):
    """A copy of *src* holding only the faces in *keep_faces* (same transform and
    materials)."""
    me = src.data.copy()
    me.name = name
    ob = src.copy()
    ob.data = me
    ob.name = name
    ob.modifiers.clear()
    for k in ("fo4_source_nif", "fo4_lod_basename"):
        if k in ob:
            del ob[k]
    bpy.context.scene.collection.objects.link(ob)
    keep = set(int(i) for i in keep_faces)
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.faces.ensure_lookup_table()
    bmesh.ops.delete(bm, geom=[f for f in bm.faces if f.index not in keep], context='FACES')
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context='VERTS')
    bm.to_mesh(me)
    bm.free()
    me.update()
    if ob.parent is not None:
        mw = ob.matrix_world.copy()
        ob.parent = None
        ob.matrix_world = mw
    return ob


# ── trunk ─────────────────────────────────────────────────────────────────────

def trunk_shell(wood_obj, name, target_tris, voxel):
    """Closed low-poly trunk from the solid wood: join the seams, voxel-remesh WITHOUT
    the thickening the general LOD shell uses (that pass is for thin open sheets; on a
    trunk it added ~1.5 voxels all round: GSFungusStock04's trunk LOD came out 498
    wide), keep the big pieces, reduce to *target_tris*, fresh UVs for the bake."""
    me = wood_obj.data.copy()
    me.name = name
    ob = wood_obj.copy()
    ob.data = me
    ob.name = name
    ob.modifiers.clear()
    ob.vertex_groups.clear()
    bpy.context.scene.collection.objects.link(ob)
    bm = bmesh.new()
    bm.from_mesh(me)
    ext = max(ob.dimensions) / max(max(ob.scale), 1e-9) if ob.dimensions else 1.0
    bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=max(ext * 1e-5, 1e-6))
    bm.to_mesh(me)
    bm.free()

    def _apply(mod):
        bpy.ops.object.select_all(action='DESELECT')
        bpy.context.view_layer.objects.active = ob
        ob.select_set(True)
        return bpy.ops.object.modifier_apply(modifier=mod.name)
    rm = ob.modifiers.new("TreeLOD_Remesh", 'REMESH')
    rm.mode = 'VOXEL'
    # voxel is in world units; the modifier works in object space
    rm.voxel_size = voxel / max(max(ob.matrix_world.to_scale()), 1e-9)
    if 'FINISHED' not in _apply(rm) or not me.polygons:
        bpy.data.objects.remove(ob, do_unlink=True)
        return None, "voxel remesh did not run"
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.faces.ensure_lookup_table()
    islands, seen = [], set()
    for f in bm.faces:
        if f.index in seen:
            continue
        stack, isle = [f], []
        seen.add(f.index)
        while stack:
            cur = stack.pop()
            isle.append(cur)
            for e in cur.edges:
                for nf in e.link_faces:
                    if nf.index not in seen:
                        seen.add(nf.index)
                        stack.append(nf)
        islands.append(isle)
    total = len(bm.faces)
    small = [f for isle in islands if len(isle) < 0.02 * total for f in isle]
    if small and len(small) < total:
        bmesh.ops.delete(bm, geom=small, context='FACES')
    bm.to_mesh(me)
    bm.free()
    tris_now = len(me.polygons) * 2
    if tris_now > target_tris:
        dm = ob.modifiers.new("TreeLOD_Decimate", 'DECIMATE')
        dm.decimate_type = 'COLLAPSE'
        dm.ratio = max(0.001, target_tris / tris_now)
        _apply(dm)
    bpy.ops.object.select_all(action='DESELECT')
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.quads_convert_to_tris()
    bpy.ops.uv.smart_project(angle_limit=math.radians(66), island_margin=0.003)
    bpy.ops.object.mode_set(mode='OBJECT')
    for p in me.polygons:
        p.use_smooth = True
    ob["fo4_lod_voxel"] = voxel
    ob["fo4_lod_shell"] = True
    return ob, f"LOD '{name}': {len(me.polygons)} triangles (closed trunk, voxel {voxel:.3g})"


def _surface_samples(obj, n=3000, seed=1):
    V, faces = _world_mesh(obj)
    tris, areas = [], []
    for f in faces:
        for k in range(1, len(f) - 1):
            a, b, c = V[f[0]], V[f[k]], V[f[k + 1]]
            tris.append((a, b, c))
            areas.append(0.5 * np.linalg.norm(np.cross(b - a, c - a)))
    areas = np.asarray(areas)
    if not len(areas) or areas.sum() <= 0:
        return np.zeros((0, 3))
    rng = np.random.default_rng(seed)
    pick = rng.choice(len(areas), n, p=areas / areas.sum())
    r = rng.random((n, 2))
    flip = r.sum(axis=1) > 1
    r[flip] = 1 - r[flip]
    T = np.array(tris)[pick]
    return T[:, 0] + r[:, :1] * (T[:, 1] - T[:, 0]) + r[:, 1:] * (T[:, 2] - T[:, 0])


def profile_error(src_obj, lod_obj):
    """Surface distance between *lod_obj* and *src_obj*, both ways (a trunk that is too
    fat and one that lost a limb both count), as a share of the tree height."""
    from mathutils.bvhtree import BVHTree

    def bvh(o):
        V, faces = _world_mesh(o)
        return BVHTree.FromPolygons([Vector(v) for v in V], [list(f) for f in faces])
    A = _surface_samples(src_obj)
    B = _surface_samples(lod_obj)
    ta, tb = bvh(src_obj), bvh(lod_obj)
    Va, _ = _world_mesh(src_obj)
    H = float(np.ptp(Va[:, 2])) or 1.0

    def mean_dist(P, tree):
        d = []
        for p in P:
            hit = tree.find_nearest(Vector(p))
            d.append(hit[3] if hit[0] is not None else H)
        return float(np.mean(d)) if d else H
    return 0.5 * (mean_dist(A, tb) + mean_dist(B, ta)) / H


# ── canopy cards ──────────────────────────────────────────────────────────────

def cluster_faces(centres, weights, k, seed_idx=None):
    """Area-weighted k-means of face centres (deterministic farthest-point start).
    Returns a label per face."""
    n = len(centres)
    k = max(1, min(k, n))
    first = int(np.argmax(weights)) if seed_idx is None else seed_idx
    cs = [centres[first]]
    d = np.linalg.norm(centres - cs[0], axis=1)
    for _ in range(1, k):
        j = int(np.argmax(d * np.sqrt(weights)))
        cs.append(centres[j])
        d = np.minimum(d, np.linalg.norm(centres - centres[j], axis=1))
    C = np.array(cs)
    lab = np.zeros(n, int)
    for _ in range(30):
        dist = np.linalg.norm(centres[:, None, :] - C[None, :, :], axis=2)
        lab = np.argmin(dist, axis=1)
        newC = np.array([np.average(centres[lab == j], axis=0, weights=weights[lab == j])
                         if np.any(lab == j) else C[j] for j in range(k)])
        if np.allclose(newC, C):
            break
        C = newC
    return lab


def card_frames(V, faces, face_ids, lab, axis_xy, H):
    """One square card per cluster: (centre, u, v, n, size)."""
    up = np.array([0.0, 0.0, 1.0])
    frames = []
    for j in sorted(set(lab.tolist())):
        fids = face_ids[lab == j]
        vid = sorted({v for i in fids for v in faces[i]})
        P = V[vid]
        if len(P) < 3:
            continue
        c = P.mean(axis=0)
        # the leaves' own facing: the cluster's thinnest direction
        _w, vec = np.linalg.eigh(np.cov((P - c).T))
        leaf_n = vec[:, 0]
        out = np.array([c[0] - axis_xy[0], c[1] - axis_xy[1], 0.0])
        if np.linalg.norm(out) > 0.06 * H:
            out = out / np.linalg.norm(out)
            if np.dot(leaf_n, out) < 0:
                leaf_n = -leaf_n
            n = CARD_FACE_BLEND * leaf_n + (1.0 - CARD_FACE_BLEND) * out
            n /= np.linalg.norm(n)
        else:
            # over the trunk (the top of the crown): face the way the leaves do
            n = leaf_n if leaf_n[2] >= 0 else -leaf_n
        u = np.cross(up, n)
        if np.linalg.norm(u) < 1e-6:
            u = np.array([1.0, 0.0, 0.0])
        u /= np.linalg.norm(u)
        v = np.cross(n, u)
        pu, pv = (P - c) @ u, (P - c) @ v
        lo_u, hi_u = np.percentile(pu, [1, 99])
        lo_v, hi_v = np.percentile(pv, [1, 99])
        size = max(hi_u - lo_u, hi_v - lo_v, 1e-3) * CARD_MARGIN
        centre = c + u * (lo_u + hi_u) / 2 + v * (lo_v + hi_v) / 2
        frames.append((centre, u, v, n, size, j))
    return frames


def _render_setup(scene):
    s = {"camera": scene.camera, "engine": scene.render.engine,
         "rx": scene.render.resolution_x, "ry": scene.render.resolution_y,
         "pct": scene.render.resolution_percentage,
         "film": scene.render.film_transparent, "path": scene.render.filepath,
         "view": scene.view_settings.view_transform, "look": scene.view_settings.look,
         "fmt": scene.render.image_settings.file_format,
         "mode": scene.render.image_settings.color_mode}
    for eng in ('BLENDER_EEVEE_NEXT', 'BLENDER_EEVEE', 'CYCLES'):
        try:
            scene.render.engine = eng
            break
        except Exception:
            continue
    scene.render.film_transparent = True
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGBA'
    try:
        scene.view_settings.view_transform = 'Standard'   # true colours, no filmic curve
        scene.view_settings.look = 'None'
    except Exception:
        pass
    return s


def _render_restore(scene, s):
    scene.camera = s["camera"]
    scene.render.engine = s["engine"]
    scene.render.resolution_x, scene.render.resolution_y = s["rx"], s["ry"]
    scene.render.resolution_percentage = s["pct"]
    scene.render.film_transparent = s["film"]
    scene.render.filepath = s["path"]
    scene.render.image_settings.file_format = s["fmt"]
    scene.render.image_settings.color_mode = s["mode"]
    try:
        scene.view_settings.view_transform = s["view"]
        scene.view_settings.look = s["look"]
    except Exception:
        pass


def _look(ob, direction, up):
    """Point *ob*'s -Z along *direction* with +Y toward *up*."""
    z = -np.asarray(direction, float)
    z /= np.linalg.norm(z)
    x = np.cross(up, z)
    if np.linalg.norm(x) < 1e-6:
        x = np.array([1.0, 0.0, 0.0])
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    m = Matrix(((x[0], y[0], z[0]), (x[1], y[1], z[1]), (x[2], y[2], z[2])))
    ob.rotation_euler = m.to_euler()


def matte_stand_ins(src):
    """Plain stand-ins for *src*'s materials: the texture's own colour and cut-out
    alpha, fully rough, no specular. Rendering through the imported FO4 materials
    made the cards silver-grey (grazing-angle reflections, 2026-11-13); vanilla LOD
    canopy textures carry the leaf colour."""
    try:
        from . import fo4_lod_generator as _lg
        from . import bgsm_helpers as _bh
    except ImportError:
        import fo4_lod_generator as _lg
        import bgsm_helpers as _bh
    out = []
    for slot in src.material_slots:
        m = slot.material
        sm = bpy.data.materials.new(f"{(m.name if m else 'mat')}_lodcard")
        sm.use_nodes = True
        nt = sm.node_tree
        bsdf = next(n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED')
        for name, val in (("Roughness", 1.0), ("Metallic", 0.0), ("Specular IOR Level", 0.0),
                          ("Specular", 0.0), ("Coat Weight", 0.0), ("Sheen Weight", 0.0)):
            if bsdf.inputs.get(name) is not None:
                bsdf.inputs[name].default_value = val
        img = _lg.base_color_image(m) if m else None
        ref = 0.5
        if m is not None:
            try:
                pa = _bh._pynifly_alpha_settings(m)
                if pa and pa[0]:
                    ref = pa[1] / 255.0
            except Exception:
                pass
        if img is not None:
            tn = nt.nodes.new('ShaderNodeTexImage')
            tn.image = img
            nt.links.new(tn.outputs['Color'], bsdf.inputs['Base Color'])
            if bsdf.inputs.get('Alpha') is not None:
                nt.links.new(tn.outputs['Alpha'], bsdf.inputs['Alpha'])
        sm.blend_method = 'CLIP'
        try:
            sm.alpha_threshold = ref
        except Exception:
            pass
        sm.use_backface_culling = False
        out.append(sm)
    return out


def _use_stand_ins(obj, stand_ins):
    for slot, sm in zip(obj.material_slots, stand_ins):
        slot.link = 'OBJECT'
        slot.material = sm


def render_views(src, faces_by_view, frames, tile, scene_name_tag):
    """Render the given faces of *src* (all faces when faces_by_view[j] is None), seen
    from the front of each frame, into a tile each. Returns {j: RGBA (tile, tile, 4)}."""
    scene = bpy.context.scene
    saved = _render_setup(scene)
    hidden = {}
    temp_objs, lights = [], []
    cam = None
    tiles = {}
    tmpdir = tempfile.mkdtemp(prefix="fo4treelod_")
    stand_ins = matte_stand_ins(src)
    world_prev = scene.world
    try:
        # soft, even sky light so shaded sides are not black
        w = bpy.data.worlds.new(f"{scene_name_tag}_CardSky")
        w.use_nodes = True
        bg = next((n for n in w.node_tree.nodes if n.type == 'BACKGROUND'), None)
        if bg is not None:
            bg.inputs['Color'].default_value = (0.55, 0.6, 0.65, 1.0)
            bg.inputs['Strength'].default_value = 0.6
        scene.world = w
        for o in scene.objects:
            if not o.hide_render:
                hidden[o] = True
                o.hide_render = True
        cam_data = bpy.data.cameras.new(f"{scene_name_tag}_CardCam")
        cam_data.type = 'ORTHO'
        cam = bpy.data.objects.new(f"{scene_name_tag}_CardCam", cam_data)
        scene.collection.objects.link(cam)
        scene.camera = cam
        for label, power in (("Key", 2.2), ("Top", 1.0), ("Rim", 0.6)):
            ld = bpy.data.lights.new(f"{scene_name_tag}_Card{label}", type='SUN')
            ld.energy = power
            ld.angle = 0.3
            lo = bpy.data.objects.new(f"{scene_name_tag}_Card{label}", ld)
            scene.collection.objects.link(lo)
            lights.append((label, lo))
        scene.render.resolution_x = scene.render.resolution_y = tile
        for centre, u, v, n, size, j in frames:
            keep = faces_by_view[j] if faces_by_view.get(j) is not None else range(len(src.data.polygons))
            part = part_object(src, keep, f"{scene_name_tag}_cl{j}")
            _use_stand_ins(part, stand_ins)
            part.hide_render = False
            temp_objs.append(part)
            d = size * 4.0 + 10.0
            cam.location = Vector(tuple(centre + n * d))
            _look(cam, -n, v)
            cam_data.ortho_scale = size
            cam_data.clip_start = 0.01
            cam_data.clip_end = d * 2 + size * 4
            for label, lo in lights:
                if label == "Key":
                    _look(lo, -(n * 0.8 + np.array([0, 0, 0.6])), np.array([0, 0, 1.0]))
                elif label == "Top":
                    _look(lo, np.array([0.0, 0.0, -1.0]), np.array([0, 1.0, 0]))
                else:
                    _look(lo, n * 0.9 - np.array([0, 0, 0.3]), np.array([0, 0, 1.0]))
            path = os.path.join(tmpdir, f"card_{j}.png")
            scene.render.filepath = path
            bpy.ops.render.render(write_still=True)
            img = bpy.data.images.load(path, check_existing=False)
            px = np.array(img.pixels[:], dtype=np.float32).reshape(tile, tile, 4)
            bpy.data.images.remove(img)
            tiles[j] = px
            bpy.data.objects.remove(part, do_unlink=True)
            temp_objs.remove(part)
    finally:
        for o in temp_objs:
            try:
                bpy.data.objects.remove(o, do_unlink=True)
            except Exception:
                pass
        for _l, lo in lights:
            ld = lo.data
            bpy.data.objects.remove(lo, do_unlink=True)
            if ld and ld.users == 0:
                bpy.data.lights.remove(ld)
        if cam is not None:
            cd = cam.data
            bpy.data.objects.remove(cam, do_unlink=True)
            if cd and cd.users == 0:
                bpy.data.cameras.remove(cd)
        for o in hidden:
            try:
                o.hide_render = False
            except Exception:
                pass
        _sky = scene.world
        scene.world = world_prev
        if _sky is not None and _sky is not world_prev and _sky.users == 0:
            bpy.data.worlds.remove(_sky)
        for sm in stand_ins:
            try:
                bpy.data.materials.remove(sm)
            except Exception:
                pass
        _render_restore(scene, saved)
    return tiles


def billboard_frames(V):
    """Front (looking +Y) and side (looking -X) views of the whole tree for the two
    crossed billboard cards, sized to the larger of width and height."""
    mn, mx = V.min(axis=0), V.max(axis=0)
    c = (mn + mx) / 2
    size = float(max(mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2])) * 1.02
    up = np.array([0.0, 0.0, 1.0])
    front = (c, np.array([1.0, 0, 0]), up, np.array([0, -1.0, 0]), size, 0)
    side = (c, np.array([0, 1.0, 0]), up, np.array([1.0, 0, 0]), size, 1)
    return [front, side]


def bleed_colour(px, cut=0.1, rounds=12):
    """Spread the colour of covered pixels into the transparent ones around them, so
    mip levels of an alpha-tested atlas do not fade to a dark outline."""
    rgb = px[..., :3].copy()
    known = px[..., 3] > cut
    for _ in range(rounds):
        if known.all():
            break
        acc = np.zeros_like(rgb)
        cnt = np.zeros(known.shape, np.float32)
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            k = np.roll(np.roll(known, dy, 0), dx, 1)
            c = np.roll(np.roll(rgb, dy, 0), dx, 1)
            acc += c * k[..., None]
            cnt += k
        grow = (~known) & (cnt > 0)
        rgb[grow] = acc[grow] / cnt[grow][:, None]
        known = known | grow
    out = px.copy()
    out[..., :3] = rgb
    return out


def build_atlas(tile_sets, tile):
    """Pack every level's tiles into one square atlas. tile_sets = [(key, {j: px})].
    Returns (atlas array, {(key, j): (col, row)}, atlas size)."""
    total = sum(len(t) for _k, t in tile_sets)
    grid = max(1, int(math.ceil(math.sqrt(total))))
    size = 1
    while size < grid * tile:
        size *= 2
    grid = size // tile
    atlas = np.zeros((size, size, 4), np.float32)
    where = {}
    i = 0
    for key, tiles in tile_sets:
        for j in sorted(tiles):
            col, row = i % grid, i // grid
            atlas[row * tile:(row + 1) * tile, col * tile:(col + 1) * tile] = tiles[j]
            where[(key, j)] = (col, row)
            i += 1
    return bleed_colour(atlas), where, size


def build_card_mesh(name, frames, where_key, where, tile, atlas_size):
    """One quad per card, UVs on its atlas tile. Built in world space."""
    verts, faces, uvs = [], [], []
    for centre, u, v, n, size, j in frames:
        if (where_key, j) not in where:
            continue
        col, row = where[(where_key, j)]
        h = size / 2
        base = len(verts)
        for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            verts.append(tuple(centre + u * su * h + v * sv * h))
        faces.append((base, base + 1, base + 2, base + 3))
        u0 = (col * tile + 0.5) / atlas_size
        u1 = ((col + 1) * tile - 0.5) / atlas_size
        v0 = (row * tile + 0.5) / atlas_size
        v1 = ((row + 1) * tile - 0.5) / atlas_size
        uvs.append(((u0, v0), (u1, v0), (u1, v1), (u0, v1)))
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    uvl = me.uv_layers.new(name="UVMap")
    for poly, quad in zip(me.polygons, uvs):
        for corner, li in enumerate(poly.loop_indices):
            uvl.data[li].uv = quad[corner]
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    return ob


# ── materials, textures, files ────────────────────────────────────────────────

def _save_png(px, path):
    h, w = px.shape[:2]
    img = bpy.data.images.new(os.path.splitext(os.path.basename(path))[0], w, h, alpha=True)
    img.pixels.foreach_set(px.astype(np.float32).ravel())
    img.filepath_raw = path
    img.file_format = 'PNG'
    img.save()
    bpy.data.images.remove(img)


def _to_dds(png, dds, **kw):
    from . import nvtt_helpers as _nv
    os.makedirs(os.path.dirname(dds), exist_ok=True)
    try:
        ok, msg = _nv.NVTTHelpers.convert_to_dds(png, dds, **kw)
    except Exception as exc:
        ok, msg = False, str(exc)
    return bool(ok and os.path.isfile(dds)), msg


def write_flat_maps(out_dir, base, tmp):
    """Flat normal (BC5) and dull specular for cards/billboards, like vanilla's _n/_s."""
    paths, notes = {}, []
    for slot, suffix, rgba in (("Normal", "_n", (0.5, 0.5, 1.0, 1.0)),
                               ("Specular", "_s", (0.08, 0.12, 0.0, 1.0))):
        png = os.path.join(tmp, f"{base}{suffix}.png")
        _save_png(np.tile(np.array(rgba, np.float32), (64, 64, 1)), png)
        dds = os.path.join(out_dir, f"{base}{suffix}.dds")
        ok, msg = _to_dds(png, dds, slot=slot)
        if ok:
            paths[slot] = dds
        else:
            notes.append(f"{base}{suffix}: DDS conversion failed ({msg})")
    return paths, notes


def alpha_material(name, bgsm_rel, tex_paths, alpha_ref):
    """Alpha-tested, two-sided LOD material (cards, billboard)."""
    try:
        from . import fo4_lod_generator as _lg
    except ImportError:
        import fo4_lod_generator as _lg
    holder = bpy.data.meshes.new(name + "_tmp")
    tmp_obj = bpy.data.objects.new(name + "_tmp", holder)
    mat = _lg.make_lod_material(None, tmp_obj, tex_paths, bgsm_rel, opaque=False)
    bpy.data.objects.remove(tmp_obj, do_unlink=True)
    bpy.data.meshes.remove(holder)
    mat.blend_method = 'CLIP'
    try:
        mat.alpha_threshold = alpha_ref / 255.0
    except Exception:
        pass
    mat.use_backface_culling = False
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
    dif = next((n for n in nodes if n.type == 'TEX_IMAGE' and "diffuse" in (n.label or n.name).lower()), None)
    if bsdf is not None and dif is not None:
        links.new(dif.outputs['Color'], bsdf.inputs['Base Color'])
        if bsdf.inputs.get('Alpha') is not None:
            links.new(dif.outputs['Alpha'], bsdf.inputs['Alpha'])
    mat["fo4_lod_material"] = True
    mat["fo4_lod_alpha_ref"] = int(alpha_ref)
    return mat


def write_bgsm(mat, mod_folder, bgsm_rel, *, alpha_ref=None, two_sided=False, subsurface=False):
    """Write the LOD .bgsm for *mat* (overwrites: it is this LOD's own file)."""
    try:
        from . import bgsm_helpers as _bh
    except ImportError:
        import bgsm_helpers as _bh
    d = _bh.blender_mat_to_bgsm(mat, is_lod=True)
    d.tree = False
    d.glowmap = False
    d.emit_enabled = False
    if alpha_ref is None:
        d.alpha_test = False
    else:
        d.alpha_test = True
        d.alpha_test_ref = int(alpha_ref)
    d.two_sided = bool(two_sided)
    d.subsurface_lighting = bool(subsurface)
    d.cast_shadows = True
    path = os.path.join(mod_folder, *[p for p in bgsm_rel.replace("/", "\\").split("\\") if p])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(_bh.write_bgsm(d))
    return path


def finish_lod_nif(nif_path, alpha_shapes):
    """Vanilla LOD shape details PyNifly does not write: NiAlphaProperty (4844 +
    threshold) on the canopy/billboard shapes, shape flags 14 instead of 0."""
    notes = []
    try:
        from io_scene_nifly.pyn.pynifly import NifFile
        nif = NifFile(nif_path)
        changed = False
        for s in nif.shapes:
            ref = alpha_shapes.get(s.name)
            if ref is None:
                for key, val in alpha_shapes.items():
                    if s.name.startswith(key):
                        ref = val
            if ref is None:
                continue
            if not s.has_alpha_property:
                s.has_alpha_property = True
            s.alpha_property.properties.flags = ALPHA_PROPERTY_FLAGS
            s.alpha_property.properties.threshold = int(ref)
            s.save_alpha_property()
            changed = True
        if changed:
            nif.save()
    except Exception as exc:
        notes.append(f"alpha property not set on {os.path.basename(nif_path)} ({exc})")
    try:
        from . import nif_roundtrip as _nrt
        _nrt.patch_zero_shape_flags(nif_path)
    except Exception as exc:
        notes.append(f"shape flags not checked ({exc})")
    return notes


# ── trees that already have leaf cards: the tree's own textures, shrunk ───────
#
# Bethesda's pre-war tree LODs (Meshes\LOD\Landscape\Trees\Chargen, 54 files checked
# 2026-11-14) never bake a new picture for _LOD_0 / _LOD_1: the trunk shape uses
# Textures\LOD\PreWarTreeTrunksLOD (the bark strip, shrunk to 512) and the canopy
# shape uses PreWarMapleGrLOD / PreWarElmGrLOD (the leaf-branch picture of the full
# tree's atlas, shrunk to 512). Only _LOD_2 is a picture of the whole tree. A tree
# that already carries leaf cards (a card-sheet material) gets exactly that here:
# the real trunk, reduced but keeping its UVs, and a selection of its real cards,
# grown to cover the same canopy, both on their own textures shrunk.

CARD_LOD_LEVELS = {
    # vanilla _LOD_0 canopy 61-150 triangles; _LOD_1 41-80. The trunk keeps its own
    # UVs, so it is reduced (not remeshed) to the smallest size whose surface stays
    # within wood_err of the tree height. GSFungusStock04's welded trunk (4,805
    # triangles, 2026-11-14): 15% -> 719 tris, 0.43% off; 10% -> 480, 0.93%;
    # 6% and below stop at 320 tris, 2.73% off (spiky).
    # At 10% (0.93%) the roots flattened into a stretched slab; 0.6% is the most a
    # level may drift.
    "LOD1": {"cards": 80, "wood_err": 0.004},
    "LOD2": {"cards": 40, "wood_err": 0.006},
}
WOOD_RATIOS = (0.04, 0.06, 0.08, 0.1, 0.15, 0.2, 0.3)


def _drop_unused_slots(ob):
    used = {p.material_index for p in ob.data.polygons}
    for i in reversed(range(len(ob.material_slots))):
        if i not in used:
            bpy.ops.object.select_all(action='DESELECT')
            bpy.context.view_layer.objects.active = ob
            ob.select_set(True)
            ob.active_material_index = i
            bpy.ops.object.material_slot_remove()
CARD_MAX_GROW = 4.0            # a kept card never grows past 4x its own size
# island thickness / length: GSFungusStock04's curved GSBush06 fronds measure 0.10 /
# 0.14 / 0.19 (10th / 50th / 90th percentile), its trunk 0.34; median below this = cards
CARD_FLAT = 0.25
CARD_MIN_SHARE = 0.05          # card sets under 5% of the card area are too small for a LOD


def card_material_slots(obj):
    """Material slots of *obj* whose faces are leaf cards: at least 10 separate flat
    pieces, each small against the tree. Returns (set of slot indices, info)."""
    V, faces = _world_mesh(obj)
    if not faces:
        return set(), {}
    H = float(np.ptp(V[:, 2])) or 1.0
    mi = np.zeros(len(faces), int)
    obj.data.polygons.foreach_get("material_index", mi)
    slots, info = set(), {}
    for s in sorted(set(mi.tolist())):
        ids = np.nonzero(mi == s)[0]
        comps = _face_components(V, faces, ids, H)
        if len(comps) < 10:
            continue
        ratios, lengths = [], []
        for c in comps:
            P = V[sorted({v for i in c for v in faces[i]})]
            if len(P) < 3:
                continue
            ev = np.linalg.eigvalsh(np.cov((P - P.mean(axis=0)).T))
            ratios.append(math.sqrt(max(ev[0], 0.0) / max(ev[2], 1e-12)))
            lengths.append(float(np.ptp(P, axis=0).max()))
        if not ratios:
            continue
        med_r, med_l = float(np.median(ratios)), float(np.median(lengths))
        info[s] = {"islands": len(comps), "flat": med_r, "length": med_l}
        if med_r < CARD_FLAT and med_l < 0.25 * H:
            slots.add(s)
    return slots, info


def _vertex_alpha(obj):
    """Per-vertex wind alpha (0 at the branch, 1 at the tip) or None."""
    me = obj.data
    ca = me.color_attributes.get("VERTEX_ALPHA") or me.color_attributes.get("Col")
    if ca is None:
        return None
    vals = np.array([d.color[0] if ca.name == "VERTEX_ALPHA" else d.color[3] for d in ca.data], float)
    if ca.domain == 'POINT':
        return vals
    out = np.zeros(len(me.vertices))
    cnt = np.zeros(len(me.vertices))
    for li, loop in enumerate(me.loops):
        out[loop.vertex_index] += vals[li]
        cnt[loop.vertex_index] += 1
    return out / np.maximum(cnt, 1)


def source_card_quads(obj, slots, axis_xy):
    """One quad per leaf card of *obj* in *slots*: its four UV-rectangle corners
    (world position + UV), the point where it meets the twig, its area and slot."""
    V, faces = _world_mesh(obj)
    H = float(np.ptp(V[:, 2])) or 1.0
    me = obj.data
    uvl = me.uv_layers.active
    if uvl is None:
        return []
    mi = np.zeros(len(faces), int)
    me.polygons.foreach_get("material_index", mi)
    alpha = _vertex_alpha(obj)
    quads = []
    for s in slots:
        for comp in _face_components(V, faces, np.nonzero(mi == s)[0], H):
            loops = [(me.loops[li].vertex_index, tuple(uvl.data[li].uv))
                     for i in comp for li in me.polygons[int(i)].loop_indices]
            U = np.array([uv for _v, uv in loops])
            u0, v0 = U.min(axis=0)
            u1, v1 = U.max(axis=0)
            if u1 - u0 < 1e-6 or v1 - v0 < 1e-6:
                continue
            corners = []
            for cu, cv in ((u0, v0), (u1, v0), (u1, v1), (u0, v1)):
                k = int(np.argmin((U[:, 0] - cu) ** 2 + (U[:, 1] - cv) ** 2))
                corners.append((loops[k][0], (cu, cv)))
            P = np.array([V[v] for v, _uv in corners])
            # where the card meets the twig: the edge with the lowest wind alpha, else
            # the edge nearest the trunk
            edges = [(0, 1), (1, 2), (2, 3), (3, 0)]
            if alpha is not None and np.ptp([alpha[v] for v, _uv in corners]) > 0.05:
                score = [alpha[corners[a][0]] + alpha[corners[b][0]] for a, b in edges]
            else:
                score = [np.linalg.norm(((P[a] + P[b]) / 2)[:2] - axis_xy) for a, b in edges]
            a, b = edges[int(np.argmin(score))]
            base = (P[a] + P[b]) / 2
            area = 0.5 * np.linalg.norm(np.cross(P[1] - P[0], P[3] - P[0])) + \
                0.5 * np.linalg.norm(np.cross(P[1] - P[2], P[3] - P[2]))
            quads.append({"P": P, "uv": [uv for _v, uv in corners], "base": base,
                          "centre": P.mean(axis=0), "area": float(area), "slot": int(s)})
    return quads


def pick_cards(quads, n):
    """*n* cards spread over the canopy (area-weighted k-means, the card nearest each
    centre) and the growth that keeps the canopy's leaf area."""
    if not quads:
        return [], 1.0
    C = np.array([q["centre"] for q in quads])
    A = np.array([q["area"] for q in quads]) + 1e-9
    n = min(n, len(quads))
    lab = cluster_faces(C, A, n)
    keep = []
    for j in sorted(set(lab.tolist())):
        idx = np.nonzero(lab == j)[0]
        m = np.average(C[idx], axis=0, weights=A[idx])
        keep.append(int(idx[np.argmin(np.linalg.norm(C[idx] - m, axis=1))]))
    grow = math.sqrt(A.sum() / A[keep].sum())
    return [quads[i] for i in keep], float(min(max(grow, 1.0), CARD_MAX_GROW))


def build_card_lod_mesh(name, picked, grow, src):
    """The canopy LOD shape: the picked cards, grown about where they meet the twig,
    keeping their own UVs and the source's card material."""
    verts, polys, uvs, mats = [], [], [], []
    for q in picked:
        b = len(verts)
        g = q.get("grow", grow)
        for p in q["P"]:
            verts.append(tuple(q["base"] + g * (p - q["base"])))
        polys.append((b, b + 1, b + 2, b + 3))
        uvs.append(q["uv"])
        mats.append(q["slot"])
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], polys)
    me.update()
    uvl = me.uv_layers.new(name="UVMap")
    for poly, quad in zip(me.polygons, uvs):
        for corner, li in enumerate(poly.loop_indices):
            uvl.data[li].uv = quad[corner]
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    used = sorted(set(mats))
    for s in used:
        me.materials.append(src.material_slots[s].material)
    remap = {s: i for i, s in enumerate(used)}
    me.polygons.foreach_set("material_index", [remap[s] for s in mats])
    me.update()
    return ob


def _welded_copy(obj, name):
    ob = obj.copy()
    ob.data = obj.data.copy()
    ob.name = ob.data.name = name
    ob.modifiers.clear()
    bpy.context.scene.collection.objects.link(ob)
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    ext = max(ob.dimensions) / max(max(ob.scale), 1e-9) if ob.dimensions else 1.0
    bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=max(ext * 1e-4, 1e-6))
    bm.to_mesh(ob.data)
    bm.free()
    return ob


def _card_alpha_ref(mat, default=CANOPY_ALPHA_REF):
    try:
        from . import bgsm_helpers as _bh
        pa = _bh._pynifly_alpha_settings(mat)
        if pa and pa[0]:
            return int(pa[1])
    except Exception:
        pass
    return default


def card_tree_levels(src, card_slots, card_levels, base_name, safe, rel_dir, textures_root,
                     mod_folder, nif_path_for, export, collection, tex_size, axis_xy):
    """_LOD_0 / _LOD_1 for a tree that has leaf cards, built like vanilla: the real
    trunk reduced (own UVs) + a selection of the real cards, on the tree's own
    textures shrunk to *tex_size* and shared by both levels. Returns (objects, steps)."""
    try:
        from . import fo4_lod_generator as lg
        from . import export_helpers as eh
    except ImportError:
        import fo4_lod_generator as lg
        import export_helpers as eh
    steps, made, parts_by_level = [], [], {}
    n_faces = len(src.data.polygons)
    mi = np.zeros(n_faces, int)
    src.data.polygons.foreach_get("material_index", mi)
    wood_ids = np.nonzero(~np.isin(mi, list(card_slots)))[0]
    quads = source_card_quads(src, card_slots, axis_xy)
    card_names = ", ".join(sorted({src.material_slots[s].material.name for s in card_slots
                                   if src.material_slots[s].material}))
    steps.append(f"{base_name}: {len(quads)} leaf cards found ({card_names}); LODs use the tree's own "
                 f"textures shrunk to {tex_size}, like Bethesda's pre-war trees")
    wood_src = None
    if len(wood_ids):
        wood_part = part_object(src, wood_ids, f"{safe}_CardLOD_woodsrc")
        wood_src = _welded_copy(wood_part, f"{safe}_CardLOD_wood")
        m_ = wood_part.data
        bpy.data.objects.remove(wood_part, do_unlink=True)
        if m_.users == 0:
            bpy.data.meshes.remove(m_)
    try:
        for key in card_levels:
            cfg = CARD_LOD_LEVELS[key]
            parts = []
            if wood_src is not None:
                trunk, msg, err = None, "", None
                for r in WOOD_RATIOS:
                    cand, cmsg = lg.generate_lod_mesh(wood_src, r, f"{base_name}_{key}_Trunk_try")
                    if cand.parent is not None:
                        mw = cand.matrix_world.copy()
                        cand.parent = None
                        cand.matrix_world = mw
                    e = profile_error(wood_src, cand)
                    if trunk is not None:
                        m_ = trunk.data
                        bpy.data.objects.remove(trunk, do_unlink=True)
                        if m_.users == 0:
                            bpy.data.meshes.remove(m_)
                    trunk, msg, err = cand, cmsg, e
                    if e <= cfg["wood_err"]:
                        break
                trunk.name = trunk.data.name = f"{base_name}_{key}_Trunk"
                msg = msg.replace("_Trunk_try", "_Trunk") + f", surface off by {err * 100:.2f}% of the height"
                _drop_unused_slots(trunk)
                for ca in list(trunk.data.color_attributes):
                    trunk.data.color_attributes.remove(ca)
                wg = trunk.vertex_groups.get("Wind")
                if wg is not None:
                    trunk.vertex_groups.remove(wg)
                steps.append(f"{key} trunk: {msg} (keeps its own UVs and bark texture)")
                parts.append(trunk)
            if quads:
                # each card material gets its share of the budget; tiny sets (a few
                # small flat bits at the roots) are not visible at LOD distance
                by_slot = {}
                for q in quads:
                    by_slot.setdefault(q["slot"], []).append(q)
                total = sum(q["area"] for q in quads) or 1.0
                picked, notes = [], []
                for s, qs in sorted(by_slot.items(), key=lambda kv: -sum(q["area"] for q in kv[1])):
                    share = sum(q["area"] for q in qs) / total
                    mname = src.material_slots[s].material.name if src.material_slots[s].material else str(s)
                    if share < CARD_MIN_SHARE:
                        if key == card_levels[0]:
                            steps.append(f"{mname}: {len(qs)} small flat pieces ({share * 100:.1f}% of the card "
                                         "area) left out of the LODs, too small to see at LOD distance")
                        continue
                    sel, grow = pick_cards(qs, max(3, int(round(cfg["cards"] * share))))
                    for q in sel:
                        picked.append(dict(q, grow=grow))
                    notes.append(f"{len(sel)} of {len(qs)} {mname} cards grown {grow:.2f}x")
                canopy = build_card_lod_mesh(f"{base_name}_{key}_Canopy", picked, 1.0, src)
                steps.append(f"{key} canopy: " + "; ".join(notes) +
                             f" to keep the canopy's leaf area ({2 * len(picked)} triangles)")
                parts.append(canopy)
            for p in parts:
                p["fo4_lod_level"] = key
                p["fo4_lod_source"] = src.name
                p["fo4_no_wind"] = True
                p["PYN_GAME"] = "FO4"
                for k in ("fo4_unit_scale", "fo4_scale_applied"):
                    if k in src:
                        p[k] = src[k]
                for k in ("fo4_lod_merged_tmp", "fo4_lod_basename", "fo4_source_nif"):
                    if k in p:
                        del p[k]
            parts_by_level[key] = parts
        all_parts = [p for ps in parts_by_level.values() for p in ps]
        ok_t, tex_msg = lg.create_lod_textures(all_parts, textures_root=textures_root, max_size=int(tex_size))
        steps.append(tex_msg)
        lg.name_original_lod_materials(all_parts, rel_dir, safe)
        for key in card_levels:
            parts = parts_by_level.get(key, [])
            shapes = []
            for p in parts:
                if len([s for s in p.material_slots if s.material]) > 1:
                    shapes.extend(lg.split_lod_by_material(p))
                else:
                    shapes.append(p)
            alpha_shapes = {}
            canopy = next((p for p in parts if p.name.endswith("_Canopy")), None)
            if canopy is not None and canopy.active_material is not None:
                canopy.active_material["fo4_lod_card"] = True
                alpha_shapes[canopy.name] = _card_alpha_ref(canopy.active_material)
            for sh in shapes:
                if collection is not None:
                    for c in list(sh.users_collection):
                        c.objects.unlink(sh)
                    collection.objects.link(sh)
                made.append(sh)
            if export and mod_folder and shapes:
                path = nif_path_for(int(key[-1]) - 1)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                bpy.ops.object.select_all(action='DESELECT')
                for sh in shapes:
                    sh.select_set(True)
                bpy.context.view_layer.objects.active = shapes[0]
                res = eh.ExportHelpers.export_scene_as_single_nif(bpy.context.scene, path, objects=shapes)
                ok = res[0] if isinstance(res, tuple) else res in ({'FINISHED'}, 'FINISHED')
                if ok:
                    steps.extend(finish_lod_nif(path, alpha_shapes))
                    steps.append(f"Exported {os.path.basename(path)} ({len(shapes)} shapes: trunk + leaf cards, "
                                 f"own textures)")
                else:
                    steps.append(f"⚠ Export failed for {os.path.basename(path)}: "
                                 f"{res[1] if isinstance(res, tuple) else res}")
    finally:
        if wood_src is not None:
            m_ = wood_src.data
            bpy.data.objects.remove(wood_src, do_unlink=True)
            if m_.users == 0:
                bpy.data.meshes.remove(m_)
    return made, steps


# ── the whole tree LOD set ────────────────────────────────────────────────────

def generate_tree_lods(src, base_name, levels, mod_folder, mesh_subpath, export=True,
                       billboard=True, collection=None, tex_size=512):
    """Build (and export when *export* and *mod_folder*) the tree LOD set for *src*.

    *levels*: add-on LOD keys to build, e.g. ("LOD1", "LOD2", "LOD3"); LOD1/LOD2 get a
    trunk + leaf cards, LOD3 the crossed billboard. A tree with a leaf-card material
    gets vanilla's way (its own textures shrunk, see card_tree_levels); a one-piece
    tree without cards gets rendered cards. Returns (objects, steps)."""
    try:
        from . import fo4_lod_generator as lg
        from . import export_helpers as eh
    except ImportError:
        import fo4_lod_generator as lg
        import export_helpers as eh
    steps = []
    made = []
    # File-safe: an object name like "L2_gsfungusstock04:1" used as a file name wrote
    # the LOD into an NTFS stream of a file called "L2_gsfungusstock04" (2026-11-13).
    base_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(base_name)).strip(" .") or "Tree"
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", base_name)
    src_mat = src.active_material
    rel_dir = lg._lod_rel_dir(src_mat, mesh_subpath)
    textures_root = os.path.join(mod_folder, "Textures") if mod_folder else tempfile.mkdtemp(prefix="fo4treelod_tex_")
    tex_dir = os.path.join(textures_root, "LOD", *[p for p in rel_dir.split("\\") if p])
    mat_dir_rel = "materials\\LOD\\" + (rel_dir + "\\" if rel_dir else "")
    tmp = tempfile.mkdtemp(prefix="fo4treelod_")
    sub = mesh_subpath

    def nif_path_for(index):
        parts = [mod_folder, "Meshes", "LOD"] + ([p for p in re.split(r"[\\/]+", sub) if p] if sub else [])
        return os.path.normpath(os.path.join(*parts, f"{base_name}_LOD_{index}.nif"))

    labels, info = split_wood_foliage(src)
    H = info["H"]
    steps.append(f"{base_name}: tree LOD from {info['wood']} trunk/limb faces and {info['leaf']} leaf faces "
                 f"({info['drop']} thin ground-root faces left out)")
    V, faces = _world_mesh(src)
    wood_ids = np.nonzero(labels == "wood")[0]
    leaf_ids = np.nonzero(labels == "leaf")[0]
    wood_obj = part_object(src, wood_ids, f"{safe}_TreeLOD_wood") if len(wood_ids) else None
    if wood_obj is not None:
        wood_obj.hide_render = True
    axis_xy = (V[np.unique([v for i in wood_ids for v in faces[i]])][:, :2].mean(axis=0)
               if len(wood_ids) else V[:, :2].mean(axis=0))

    level_parts = {}
    card_levels = [k for k in ("LOD1", "LOD2") if k in levels]
    card_slots, _cinfo = card_material_slots(src)
    if card_slots and card_levels:
        _m, _s = card_tree_levels(src, card_slots, card_levels, base_name, safe, rel_dir, textures_root,
                                  mod_folder, nif_path_for, export, collection, tex_size, axis_xy)
        made.extend(_m)
        steps.extend(_s)
        card_levels = []          # done: skip the rendered-card path below
    elif card_levels:
        steps.append(f"{base_name}: no leaf-card material found (leaves are part of the tree mesh), so "
                     "the LOD trunk and cards are baked from renders. Use Tree Tools > Replace Leaves "
                     "with Leaf Cards first to get LODs on the tree's own textures, like vanilla")
    try:
        # trunks
        last_shell = None
        for key in card_levels:
            cfg = TREE_LOD_LEVELS[key]
            trunk = None
            if wood_obj is not None:
                name = f"{base_name}_{key}_Trunk"
                if last_shell is not None:
                    trunk, msg = lg.reduce_lod_shell(last_shell, name, cfg["trunk_tris"])
                else:
                    # Candidates: the trunk remesh (no thickening) at two voxel
                    # sizes and the general shell (a finer voxel can lose a thin
                    # part a coarser one keeps: GSFungusStock04 voxel 29 failed,
                    # 34 kept it). Keep the one whose outline follows the wood.
                    cands = []
                    for frac in (1 / 60.0, 1 / 45.0, 1 / 30.0):
                        o2, m2 = trunk_shell(wood_obj, f"{name}_c{len(cands)}", cfg["trunk_tris"], H * frac)
                        if o2 is not None:
                            cands.append((profile_error(wood_obj, o2), o2, m2))
                    for div in (cfg["voxel_div"], 35, 25):
                        o2, m2 = lg.generate_lod_shell(wood_obj, f"{name}_c{len(cands)}", cfg["trunk_tris"], div)
                        if o2 is not None:
                            cands.append((profile_error(wood_obj, o2), o2, m2))
                            break
                    trunk, msg = None, "no closed trunk could be built"
                    if cands:
                        steps.append(f"{key} trunk candidates: " + "; ".join(
                            f"{m2.split('(')[-1].rstrip(')')} -> surface off {e * 100:.2f}% of height"
                            for e, _o, m2 in cands))
                        cands.sort(key=lambda c: c[0])
                        err, trunk, msg = cands[0]
                        trunk.name = name
                        trunk.data.name = name
                        msg = msg.replace(f"{name}_c", f"{name} (candidate ") + \
                            f"; surface off by {err * 100:.2f}% of the height (best of {len(cands)})"
                        for _e, o2, _m in cands[1:]:
                            m_ = o2.data
                            bpy.data.objects.remove(o2, do_unlink=True)
                            if m_.users == 0:
                                bpy.data.meshes.remove(m_)
                if trunk is None:
                    steps.append(f"{key} trunk: {msg}; reduced the trunk mesh instead")
                    n_w = sum(len(p.vertices) - 2 for p in wood_obj.data.polygons) or 1
                    trunk, msg = lg.generate_lod_mesh(wood_obj, min(1.0, cfg["trunk_tris"] / n_w), name)
                    lg._reuv_for_bake(trunk)
                else:
                    last_shell = trunk
                steps.append(f"{key} trunk: {msg}")
                trunk_base = f"{safe}TrunkLOD{int(key[-1]) - 1}"
                tex_paths, notes = lg.bake_lod_textures(wood_obj, trunk, cfg["trunk_tex"], textures_root,
                                                        rel_dir, trunk_base)
                steps.extend(notes)
                tmat = lg.make_lod_material(src_mat, trunk, tex_paths, mat_dir_rel + trunk_base + ".bgsm",
                                            opaque=True)
                tmat.use_backface_culling = True
                if export and mod_folder:
                    write_bgsm(tmat, mod_folder, mat_dir_rel + trunk_base + ".bgsm")
            level_parts[key] = {"trunk": trunk}

        # canopy cards: every level's tiles in ONE shared atlas, like vanilla
        tile_sets, frames_by_level = [], {}
        if len(leaf_ids) and card_levels:
            centres = np.array([V[list(faces[i])].mean(axis=0) for i in leaf_ids])
            weights = np.array([_face_area(V, faces[i]) for i in leaf_ids]) + 1e-9
            for key in card_levels:
                lab = cluster_faces(centres, weights, TREE_LOD_LEVELS[key]["cards"])
                frames = card_frames(V, faces, leaf_ids, lab, axis_xy, H)
                by_cluster = {j: leaf_ids[lab == j] for j in set(lab.tolist())}
                tiles = render_views(src, by_cluster, frames, CARD_TILE, f"{safe}_{key}")
                tile_sets.append((key, tiles))
                frames_by_level[key] = frames
            atlas, where, asize = build_atlas(tile_sets, CARD_TILE)
            canopy_base = f"{safe}CanopyLOD"
            png = os.path.join(tmp, f"{canopy_base}_d.png")
            _save_png(atlas, png)
            dds = os.path.join(tex_dir, f"{canopy_base}_d.dds")
            ok, msg = _to_dds(png, dds, compression_format='bc3')
            cpaths = {"Diffuse": dds if ok else png}
            if not ok:
                steps.append(f"⚠ canopy atlas not converted to DDS ({msg}); FO4 cannot load PNG")
            flat, notes = write_flat_maps(tex_dir, canopy_base, tmp)
            cpaths.update(flat)
            steps.extend(notes)
            cbgsm = mat_dir_rel + canopy_base + ".bgsm"
            cmat = alpha_material(canopy_base, cbgsm, cpaths, CANOPY_ALPHA_REF)
            if export and mod_folder:
                write_bgsm(cmat, mod_folder, cbgsm, alpha_ref=CANOPY_ALPHA_REF, two_sided=True, subsurface=True)
            steps.append(f"canopy atlas {asize}x{asize} ({sum(len(t) for _k, t in tile_sets)} cards, "
                         f"{CARD_TILE}px each) -> {cpaths['Diffuse']}")
            for key in card_levels:
                cards = build_card_mesh(f"{base_name}_{key}_Canopy", frames_by_level[key], key, where,
                                        CARD_TILE, asize)
                cards.data.materials.append(cmat)
                level_parts.setdefault(key, {})["canopy"] = cards
                steps.append(f"{key} canopy: {len(cards.data.polygons)} leaf cards "
                             f"({2 * len(cards.data.polygons)} triangles)")

        # tag, collect, export
        for key in card_levels:
            parts = [p for p in (level_parts.get(key, {}).get("trunk"), level_parts.get(key, {}).get("canopy")) if p]
            for p in parts:
                p["fo4_lod_level"] = key
                p["fo4_lod_source"] = src.name
                p["fo4_no_wind"] = True
                p["PYN_GAME"] = "FO4"
                for k in ("fo4_unit_scale", "fo4_scale_applied"):
                    if k in src:
                        p[k] = src[k]
                if collection is not None:
                    for c in list(p.users_collection):
                        c.objects.unlink(p)
                    collection.objects.link(p)
                made.append(p)
            if export and mod_folder and parts:
                path = nif_path_for(int(key[-1]) - 1)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                bpy.ops.object.select_all(action='DESELECT')
                for p in parts:
                    p.select_set(True)
                bpy.context.view_layer.objects.active = parts[0]
                res = eh.ExportHelpers.export_scene_as_single_nif(bpy.context.scene, path, objects=parts)
                ok = res[0] if isinstance(res, tuple) else res in ({'FINISHED'}, 'FINISHED')
                if ok:
                    canopy = level_parts.get(key, {}).get("canopy")
                    steps.extend(finish_lod_nif(path, {canopy.name: CANOPY_ALPHA_REF} if canopy else {}))
                    steps.append(f"Exported {os.path.basename(path)} (trunk + leaf cards)")
                else:
                    steps.append(f"⚠ Export failed for {os.path.basename(path)}: {res[1] if isinstance(res, tuple) else res}")

        # farthest level: crossed billboard as _LOD_2
        if "LOD3" in levels and billboard:
            bb_name = f"{base_name}_LOD3_Billboard"
            bb_base = f"{safe}BillboardLOD"
            # two crossed cards, each with its own view (front / side) of the tree
            bframes = billboard_frames(V)
            btiles = render_views(src, {0: None, 1: None}, bframes, 256, f"{safe}_bb")
            batlas, bwhere, bsize = build_atlas([("BB", btiles)], 256)
            bb = build_card_mesh(bb_name, bframes, "BB", bwhere, 256, bsize)
            steps.append(f"Billboard: 2 crossed cards (front and side views), 4 triangles, {bsize}x{bsize} texture")
            named = os.path.join(tmp, f"{bb_base}_d.png")
            _save_png(batlas, named)
            bdds = os.path.join(tex_dir, f"{bb_base}_d.dds")
            ok, msg2 = _to_dds(named, bdds, compression_format='bc3')
            bpaths = {"Diffuse": bdds if ok else named}
            flat, notes = write_flat_maps(tex_dir, bb_base, tmp)
            bpaths.update(flat)
            steps.extend(notes)
            bbgsm = mat_dir_rel + bb_base + ".bgsm"
            bmat = alpha_material(bb_base, bbgsm, bpaths, BILLBOARD_ALPHA_REF)
            bb.data.materials.clear()
            bb.data.materials.append(bmat)
            bb["fo4_lod_level"] = "LOD3"
            bb["fo4_lod_source"] = src.name
            bb["fo4_no_wind"] = True
            bb["PYN_GAME"] = "FO4"
            for k in ("fo4_unit_scale", "fo4_scale_applied"):
                if k in src:
                    bb[k] = src[k]
            if collection is not None:
                for c in list(bb.users_collection):
                    c.objects.unlink(bb)
                collection.objects.link(bb)
            made.append(bb)
            if export and mod_folder:
                write_bgsm(bmat, mod_folder, bbgsm, alpha_ref=BILLBOARD_ALPHA_REF, two_sided=True)
                path = nif_path_for(2)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                bpy.ops.object.select_all(action='DESELECT')
                bb.select_set(True)
                bpy.context.view_layer.objects.active = bb
                res = eh.ExportHelpers.export_mesh_to_nif(bb, path)
                ok = res[0] if isinstance(res, tuple) else res in ({'FINISHED'}, 'FINISHED')
                if ok:
                    steps.extend(finish_lod_nif(path, {bb.name: BILLBOARD_ALPHA_REF, "": BILLBOARD_ALPHA_REF}))
                    steps.append(f"Exported {os.path.basename(path)} (crossed billboard)")
                else:
                    steps.append(f"⚠ Billboard export failed: {res[1] if isinstance(res, tuple) else res}")
    finally:
        if wood_obj is not None:
            me = wood_obj.data
            bpy.data.objects.remove(wood_obj, do_unlink=True)
            if me.users == 0:
                bpy.data.meshes.remove(me)
    return made, steps
