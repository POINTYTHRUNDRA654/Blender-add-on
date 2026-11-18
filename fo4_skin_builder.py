"""Skin Builder: an AI-made skin (Meshy and similar) over a Fallout 4 building, the way it was done for
GSShackMetalPrefabCompleteLg01 (Thornmaw skin, 2026-11-16, confirmed in game: "it came out amazing").

The user makes the shape (reduces the AI model, places the copies, cuts what shows inside the building);
these buttons do the rest:

* Bake Original onto My Remesh: the reduced skin still carries the AI model's own texture layout, so each
  point of it says where on the original it came from. Every separate copy in the skin (the Thornmaw shack
  has three) is lined up with the full original from those points (a fitted move/turn/stretch, tolerant of
  wrong matches), then the original's colour, bumps, shine and coverage are baked onto that copy alone (with
  the copies baked together, an overlapping copy steals its neighbour's texture). The copies share one texture
  layout, so the results are merged by coverage. Shine is levelled like the user's Materializer maps (wet,
  living look), glow is limited to the eyes, and a material is written (two-sided, glow map, wet template).
* Check Doors: an independent walk-out test on what is in the scene now -- the building's own walls give
  the doorways, then every piece of collision (walls, skin hulls) must leave a person-sized way out at
  knee, waist and head height.

Collision for the skin itself is the existing Generate Collision button (skin over a building: small hulls
outside the walls, doors kept clear, the building's collision kept exactly).
"""
import math
import os
import time
from collections import Counter, deque

import bpy
import bmesh
import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree
from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty, StringProperty
from bpy.types import Operator, PropertyGroup

try:
    from .ui_panels import _FO4SubPanel
except Exception:                                   # pragma: no cover
    from bpy.types import Panel as _FO4SubPanel

GAME_UNITS_PER_METRE = 69.99125
OUT_ROOT = os.path.join(os.path.expanduser("~"), "Downloads", "FO4 Add-on Textures")

# Measured on the Thornmaw shack (2026-11-16): Billy's Materializer shine map on the skin it replaced had
# p10 0.28 / mean 0.52 / p90 0.79 (red = green); the in-game result he approved used these levels.
SHINE_P10, SHINE_P90 = 0.28, 0.79
# Glow: amber eyes were warm (hue 15-70 deg), bright (value > 150/255), a little saturated (> 40/255),
# and eye-sized (> 3000 texels at 4K, counted on texture that is really used -- the filled gaps around
# islands made pale thorn tips look eye-sized, 2026-11-16).
GLOW_HUE, GLOW_VAL, GLOW_SAT, GLOW_MIN_AREA_4K = (15.0, 70.0), 150.0, 40.0, 3000


# ----------------------------------------------------------------------------------------------- helpers
def _log(lines, msg):
    print(f"[Skin Builder] {msg}")
    lines.append(msg)


def _import_original(path):
    """Import the original AI model; returns one mesh object (joined) in world space."""
    before = set(bpy.data.objects)
    ext = os.path.splitext(path)[1].lower()
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    else:
        raise ValueError(f"{os.path.basename(path)}: use the original .glb, .gltf, .fbx or .obj")
    new = [o for o in bpy.data.objects if o not in before]
    meshes = [o for o in new if o.type == 'MESH']
    if not meshes:
        for o in new:
            bpy.data.objects.remove(o, do_unlink=True)
        raise ValueError(f"{os.path.basename(path)} has no mesh in it")
    for o in meshes:
        mw = o.matrix_world.copy()
        o.parent = None
        o.matrix_world = mw
    for o in new:
        if o.type != 'MESH':
            bpy.data.objects.remove(o, do_unlink=True)
    if len(meshes) > 1:
        with bpy.context.temp_override(active_object=meshes[0], selected_editable_objects=meshes,
                                       selected_objects=meshes):
            bpy.ops.object.join()
    hi = meshes[0]
    hi.name = "SkinBuilder_ORIGINAL"
    return hi


def _principled(mat):
    if mat is None or not mat.use_nodes:
        return None
    return next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)


def _emission_copy(mat, input_name, name):
    """A copy of *mat* whose surface is an emission of whatever feeds *input_name* (e.g. Roughness),
    or None when nothing is linked there (then the input's own value is used)."""
    m = mat.copy()
    m.name = name
    nt = m.node_tree
    b = _principled(m)
    out = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'), None)
    if b is None or out is None:
        return None
    em = nt.nodes.new('ShaderNodeEmission')
    inp = b.inputs[input_name]
    if inp.links:
        nt.links.new(inp.links[0].from_socket, em.inputs['Color'])
    else:
        v = float(inp.default_value)
        em.inputs['Color'].default_value = (v, v, v, 1.0)
    nt.links.new(em.outputs['Emission'], out.inputs['Surface'])
    return m


def _flat_emission(name, rgba):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    for n in list(nt.nodes):
        if n.type != 'OUTPUT_MATERIAL':
            nt.nodes.remove(n)
    em = nt.nodes.new('ShaderNodeEmission')
    em.inputs['Color'].default_value = rgba
    nt.links.new(em.outputs['Emission'], next(n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL').inputs['Surface'])
    return m


def _components(me):
    """Connected piece per vertex (union-find over edges)."""
    par = list(range(len(me.vertices)))

    def f(x):
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x
    for e in me.edges:
        a, b = f(e.vertices[0]), f(e.vertices[1])
        if a != b:
            par[a] = b
    return np.array([f(i) for i in range(len(me.vertices))])


def _uv_correspondences(lo, hi, uv_name):
    """For each vertex of *lo* whose face corners agree on one UV: the point of *hi* with that UV."""
    def world_verts(o):
        co = np.empty(len(o.data.vertices) * 3, np.float64)
        o.data.vertices.foreach_get('co', co)
        mw = np.array(o.matrix_world)
        return co.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3]
    hm = hi.data
    hm.calc_loop_triangles()
    nt = len(hm.loop_triangles)
    tl = np.empty(nt * 3, np.int64)
    hm.loop_triangles.foreach_get('loops', tl)
    tv = np.empty(nt * 3, np.int64)
    hm.loop_triangles.foreach_get('vertices', tv)
    huv = np.empty(len(hm.loops) * 2, np.float64)
    hm.uv_layers.active.data.foreach_get('uv', huv)
    huv = huv.reshape(-1, 2)
    HV = world_verts(hi)
    UVV = np.c_[huv[tl], np.zeros(nt * 3)]
    T3 = tv.reshape(-1, 3)
    bvh = BVHTree.FromPolygons(UVV.tolist(), np.arange(nt * 3).reshape(-1, 3).tolist(), all_triangles=True)
    lm = lo.data
    LV = world_verts(lo)
    luv = np.empty(len(lm.loops) * 2, np.float64)
    lm.uv_layers[uv_name].data.foreach_get('uv', luv)
    luv = luv.reshape(-1, 2)
    lvi = np.empty(len(lm.loops), np.int64)
    lm.loops.foreach_get('vertex_index', lvi)
    first = np.full((len(lm.vertices), 2), np.nan)
    first[lvi[::-1]] = luv[::-1]                      # the first corner of each vertex wins
    spread = np.zeros(len(lm.vertices))
    np.maximum.at(spread, lvi, np.abs(luv - first[lvi]).max(1))
    P_hi, ids = [], []
    for i in np.nonzero((spread < 1e-4) & ~np.isnan(first[:, 0]))[0].tolist():
        u, v = first[i]
        if False:
            continue
        hit = bvh.ray_cast(Vector((u, v, 1.0)), Vector((0, 0, -1.0)), 2.0)
        if hit[0] is None:
            continue
        k = hit[2]
        A, B, C = UVV[3 * k], UVV[3 * k + 1], UVV[3 * k + 2]
        v0, v1, v2 = B[:2] - A[:2], C[:2] - A[:2], np.array([u, v]) - A[:2]
        d00, d01, d11, d20, d21 = v0 @ v0, v0 @ v1, v1 @ v1, v2 @ v0, v2 @ v1
        den = d00 * d11 - d01 * d01
        if abs(den) < 1e-14:
            continue
        bb = (d11 * d20 - d01 * d21) / den
        cc = (d00 * d21 - d01 * d20) / den
        tv = T3[k]
        P_hi.append((1 - bb - cc) * HV[tv[0]] + bb * HV[tv[1]] + cc * HV[tv[2]])
        ids.append(i)
    return np.array(P_hi), np.array(ids, dtype=int), LV


def _fit_affine(P_hi, P_lo, tol, rng, tries=4000):
    """Move/turn/stretch (affine) taking the original onto one copy, tolerant of wrong matches."""
    H = np.c_[P_hi, np.ones(len(P_hi))]
    best_w = None
    for _ in range(tries):
        q = rng.choice(len(H), 4, replace=False)
        try:
            M = np.linalg.solve(H[q], P_lo[q])
        except np.linalg.LinAlgError:
            continue
        w = np.linalg.norm(H @ M - P_lo, axis=1) < tol
        if best_w is None or w.sum() > best_w.sum():
            best_w = w
    if best_w is None or best_w.sum() < 8:
        return None, None, None
    w = best_w
    for _ in range(2):
        M, *_ = np.linalg.lstsq(H[w], P_lo[w], rcond=None)
        r = np.linalg.norm(H @ M - P_lo, axis=1)
        w = r < tol
    return M, r, w


def _to_matrix(M):
    return Matrix(((M[0][0], M[1][0], M[2][0], M[3][0]), (M[0][1], M[1][1], M[2][1], M[3][1]),
                   (M[0][2], M[1][2], M[2][2], M[3][2]), (0, 0, 0, 1)))


def _pixels(img):
    a = np.empty(img.size[0] * img.size[1] * 4, dtype=np.float32)
    img.pixels.foreach_get(a)
    return a.reshape(img.size[1], img.size[0], 4)


def _save_png(arr, path, colorspace):
    """arr: H x W x 3 or 4, values 0..1 (stored values, no colour transform)."""
    h, w = arr.shape[:2]
    if arr.shape[2] == 3:
        arr = np.concatenate([arr, np.ones((h, w, 1), np.float32)], axis=2)
    img = bpy.data.images.new("SkinBuilder_tmp", w, h, alpha=True, float_buffer=False)
    img.colorspace_settings.name = colorspace
    img.pixels.foreach_set(np.ascontiguousarray(arr, dtype=np.float32).ravel())
    img.filepath_raw = path
    img.file_format = 'PNG'
    img.save()
    bpy.data.images.remove(img)


def _hsv255(rgb):
    """PIL-style HSV on 0..255: hue in degrees, saturation and value 0..255."""
    mx = rgb.max(-1)
    mn = rgb.min(-1)
    d = mx - mn
    s = np.where(mx > 0, d / np.maximum(mx, 1e-9) * 255.0, 0.0)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    dd = np.maximum(d, 1e-9)
    h = np.where(mx == r, ((g - b) / dd) % 6, np.where(mx == g, (b - r) / dd + 2, (r - g) / dd + 4)) * 60.0
    h = np.where(d > 0, h, 0.0)
    return h, s, mx


def _shift_or(m, r):
    out = m.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy <= r * r:
                out |= np.roll(np.roll(m, dy, 0), dx, 1)
    return out


def _shift_and(m, r):
    out = m.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy <= r * r:
                out &= np.roll(np.roll(m, dy, 0), dx, 1)
    return out


def _blob_sizes(mask):
    """Label 4-connected blobs of a sparse mask (python BFS over the set texels only)."""
    H, W = mask.shape
    lab = np.zeros(mask.shape, np.int32)
    sizes = [0]
    ys, xs = np.nonzero(mask)
    for y0, x0 in zip(ys.tolist(), xs.tolist()):
        if lab[y0, x0]:
            continue
        n = len(sizes)
        lab[y0, x0] = n
        dq = deque([(y0, x0)])
        c = 0
        while dq:
            y, x = dq.popleft()
            c += 1
            for yy, xx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= yy < H and 0 <= xx < W and mask[yy, xx] and not lab[yy, xx]:
                    lab[yy, xx] = n
                    dq.append((yy, xx))
        sizes.append(c)
    return lab, np.array(sizes)


def _box_blur(a, r):
    if r <= 0:
        return a
    c = np.cumsum(np.pad(a, ((r + 1, r), (0, 0)), mode='edge'), axis=0)
    a = (c[2 * r + 1:] - c[:-2 * r - 1]) / (2 * r + 1)
    c = np.cumsum(np.pad(a, ((0, 0), (r + 1, r)), mode='edge'), axis=1)
    return (c[:, 2 * r + 1:] - c[:, :-2 * r - 1]) / (2 * r + 1)


# ------------------------------------------------------------------------------------------------- bake
def bake_original_onto_remesh(lo, src_path, out_root, game_folder, name, size=4096, colour='AGX',
                              shine='WET', glow=True, glow_strength=0.8, two_sided=True,
                              template='Template/LeafTemplate_Wet.bgsm'):
    """Returns (ok, report lines, files written)."""
    lines, files = [], []
    if lo is None or lo.type != 'MESH':
        return False, ["Select your reduced skin (a mesh) first."], files
    if not lo.data.uv_layers:
        return False, [f"'{lo.name}' has no UV map. The bake reads the original's texture layout from the skin's "
                       "own UVs: reduce a copy of the original model (Decimate keeps them) instead of rebuilding it."], files
    uv_name = lo.data.uv_layers.active.name
    if not os.path.isfile(src_path):
        return False, [f"Original model not found: {src_path}"], files
    t0 = time.time()
    sc = bpy.context.scene
    saved = dict(engine=sc.render.engine, vt=sc.view_settings.view_transform, look=sc.view_settings.look,
                 sel=[o for o in bpy.context.selected_objects], active=bpy.context.view_layer.objects.active)
    made_objs, made_mats, made_imgs = [], [], []
    hi = None
    try:
        hi = _import_original(src_path)
        made_objs.append(hi)
        mat = hi.data.materials[0] if hi.data.materials else None
        if _principled(mat) is None:
            return False, ["The original has no standard material (Principled BSDF) to bake from."], files
        _log(lines, f"Original: {len(hi.data.polygons):,} faces; your skin: {len(lo.data.polygons):,} faces")

        # --- line the original up with every copy in the skin
        P_hi, ids, LV = _uv_correspondences(lo, hi, uv_name)
        if len(ids) < 50:
            return False, ["Too few points of your skin still match the original's texture layout "
                           f"({len(ids)}). Was the skin re-unwrapped? Reduce a copy of the original instead."], files
        roots = _components(lo.data)
        cnt = Counter(roots.tolist())
        largest = cnt.most_common(1)[0][1]
        copies = [r for r, n in cnt.most_common() if n >= 0.2 * largest]
        rng = np.random.default_rng(1)
        fits = []
        for k, rt in enumerate(copies):
            s = roots[ids] == rt
            Pl = LV[ids[s]]
            size_k = float(np.ptp(LV[roots == rt], axis=0).max()) or 1.0
            if s.sum() < 8:
                return False, [f"Copy {k + 1} ({cnt[rt]:,} vertices) has too few matching points ({int(s.sum())})."], files
            M, r, w = _fit_affine(P_hi[s], Pl, 0.008 * size_k, rng)
            if M is None or w.mean() < 0.5:
                return False, [f"Copy {k + 1} could not be lined up with the original (fewer than half its points "
                               "agree). Was it sculpted or re-unwrapped after reducing?"], files
            med = float(np.median(r[w]))
            _log(lines, f"Copy {k + 1}: {cnt[rt]:,} vertices, {w.mean() * 100:.0f}% of matches agree, "
                        f"lined up within {med / size_k * 100:.2f}% of its size (median)")
            fits.append((rt, M, size_k))
        # small loose bits go with the nearest copy
        V_world = LV
        vcopy = np.full(len(V_world), -1)
        for k, (rt, _M, _s) in enumerate(fits):
            vcopy[roots == rt] = k
        cents = [V_world[roots == rt] for rt, _M, _s in fits]
        for rt in set(roots.tolist()) - {f[0] for f in fits}:
            P = V_world[roots == rt].mean(0)
            vcopy[roots == rt] = int(np.argmin([np.min(np.linalg.norm(c - P, axis=1)) for c in cents]))

        # --- bake each copy alone
        mat_rough = _emission_copy(mat, 'Roughness', 'SkinBuilder_rough')
        mat_white = _flat_emission('SkinBuilder_white', (1, 1, 1, 1))
        made_mats += [m for m in (mat_rough, mat_white) if m]
        sc.render.engine = 'CYCLES'
        sc.cycles.samples = 1
        bk = sc.render.bake
        acc = {n: None for n in ('diffuse', 'normal', 'rough')}
        have = np.zeros((size, size), bool)
        first_cover = None
        order = []
        per_copy = []
        for k, (rt, M, size_k) in enumerate(fits):
            low = lo.copy()
            low.data = lo.data.copy()
            sc.collection.objects.link(low)
            made_objs.append(low)
            bm = bmesh.new()
            bm.from_mesh(low.data)
            bm.verts.ensure_lookup_table()
            bmesh.ops.delete(bm, geom=[v for v in bm.verts if vcopy[v.index] != k], context='VERTS')
            bm.to_mesh(low.data)
            bm.free()
            hk = hi.copy()
            hk.data = hi.data.copy()
            sc.collection.objects.link(hk)
            made_objs.append(hk)
            hk.matrix_world = _to_matrix(M) @ hi.matrix_world
            bk.use_selected_to_active = True
            bk.cage_extrusion = 0.012 * size_k
            bk.max_ray_distance = 0.035 * size_k
            for o in bpy.context.view_layer.objects:
                o.select_set(o in (low, hk))
            bpy.context.view_layer.objects.active = low
            res = {}
            for nm, typ, hmat, cs, margin in (('diffuse', 'DIFFUSE', mat, 'sRGB', 16),
                                              ('normal', 'NORMAL', mat, 'Non-Color', 16),
                                              ('rough', 'EMIT', mat_rough, 'Non-Color', 16),
                                              ('cover', 'EMIT', mat_white, 'Non-Color', 0)):
                if hmat is None:
                    continue
                img = bpy.data.images.new(f"SkinBuilder_{nm}{k}", size, size, alpha=False,
                                          float_buffer=(nm != 'diffuse'))
                img.colorspace_settings.name = cs
                made_imgs.append(img)
                tm = bpy.data.materials.new("SkinBuilder_target")
                made_mats.append(tm)
                tm.use_nodes = True
                tn = tm.node_tree.nodes.new('ShaderNodeTexImage')
                tn.image = img
                tm.node_tree.nodes.active = tn
                low.data.materials.clear()
                low.data.materials.append(tm)
                hk.data.materials.clear()
                hk.data.materials.append(hmat)
                bk.margin = margin
                if typ == 'DIFFUSE':
                    bk.use_pass_direct = False
                    bk.use_pass_indirect = False
                    bk.use_pass_color = True
                    bpy.ops.object.bake(type='DIFFUSE', pass_filter={'COLOR'})
                elif typ == 'NORMAL':
                    bk.normal_space = 'TANGENT'
                    bk.normal_r, bk.normal_g, bk.normal_b = 'POS_X', 'NEG_Y', 'POS_Z'   # DirectX, as FO4
                    bpy.ops.object.bake(type='NORMAL')
                else:
                    bpy.ops.object.bake(type='EMIT')
                if nm == 'diffuse' and colour == 'AGX':
                    # The approved Thornmaw look (2026-11-16): the colour map went through AgX when saved.
                    p = os.path.join(bpy.app.tempdir or os.path.expanduser("~"), f"skinbuilder_d{k}.png")
                    sc.view_settings.view_transform = 'AgX'
                    sc.view_settings.look = 'None'
                    sc.render.image_settings.file_format = 'PNG'
                    sc.render.image_settings.color_depth = '8'
                    img.save_render(p)
                    re = bpy.data.images.load(p)
                    re.colorspace_settings.name = 'sRGB'
                    res[nm] = _pixels(re)[..., :3].copy()
                    bpy.data.images.remove(re)
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                else:
                    res[nm] = _pixels(img)[..., :3].copy()
                bpy.data.images.remove(img)
                made_imgs.remove(img)
            cover = res.pop('cover')[..., 0] > 0.5
            per_copy.append((cover, {n: a.astype(np.float16) for n, a in res.items()}))
            made_objs.remove(hk)
            made_objs.remove(low)
            bpy.data.objects.remove(hk, do_unlink=True)
            bpy.data.objects.remove(low, do_unlink=True)
            _log(lines, f"Copy {k + 1} baked ({cover.mean() * 100:.1f}% of the texture)")
        # merge: the copy covering most first, gaps from the next; everything else from the first (its margin)
        per_copy.sort(key=lambda x: -x[0].sum())
        base_cover, base = per_copy[0]
        out = {n: base[n].copy() for n in base}
        have = base_cover.copy()
        for cov, res in per_copy[1:]:
            m = cov & ~have
            for n in out:
                out[n][m] = res[n][m]
            have |= cov
        for a_ in range(len(per_copy)):
            for b_ in range(a_ + 1, len(per_copy)):
                both = per_copy[a_][0] & per_copy[b_][0]
                if both.any():
                    d = np.abs(per_copy[a_][1]['diffuse'][both] - per_copy[b_][1]['diffuse'][both]).mean() * 255
                    _log(lines, f"Copies sharing texture agree on colour within {d:.1f}/255")
                    break
            break
        del per_copy

        # --- shine, glow
        out = {n: a.astype(np.float32) for n, a in out.items()}
        dif = np.clip(out['diffuse'], 0, 1)
        smooth = 1.0 - out['rough'][..., 1]
        h, s, v = _hsv255(dif * 255.0)
        soft = np.zeros(have.shape, np.float32)
        n_eyes = 0
        if glow:
            m = (v > GLOW_VAL) & (h > GLOW_HUE[0]) & (h < GLOW_HUE[1]) & (s > GLOW_SAT) & have
            m = _shift_and(_shift_or(m, 3), 3) & have
            lab, sizes = _blob_sizes(m)
            keep = sizes > GLOW_MIN_AREA_4K * (size / 4096.0) ** 2
            keep[0] = False
            eyes = keep[lab]
            n_eyes = int(keep.sum())
            soft = _box_blur(_box_blur(eyes.astype(np.float32), 2), 2)
            lum = dif.mean(-1, keepdims=True)
            amber = np.array([1.0, 0.62, 0.18], np.float32)
            g_img = np.clip(soft[..., None] * (0.35 + 0.9 * lum) * amber * 1.25, 0, 1)
            _log(lines, f"Glow: {n_eyes} eye-sized bright warm spots ({eyes.mean() * 100:.2f}% of the texture)")
        if shine == 'WET':
            p10, p90 = np.percentile(smooth[have], [10, 90])
            spec = np.clip(SHINE_P10 + (smooth - p10) * (SHINE_P90 - SHINE_P10) / max(p90 - p10, 1e-4), 0, 1)
        else:
            spec = np.clip(smooth, 0, 1)
        spec = np.maximum(spec, soft * 0.85)
        _log(lines, f"Shine: mean {spec[have].mean():.2f} (your Materializer maps: 0.52)")

        # --- files
        tex_dir = os.path.join(out_root, "Textures", game_folder)
        mat_dir = os.path.join(out_root, "Materials", game_folder)
        os.makedirs(tex_dir, exist_ok=True)
        os.makedirs(mat_dir, exist_ok=True)
        S = np.zeros(dif.shape, np.float32)
        S[..., 0] = S[..., 1] = spec
        maps = [('_d', dif, 'sRGB', 'bc7'), ('_n', np.clip(out['normal'], 0, 1), 'Non-Color', 'bc5'),
                ('_s', S, 'Non-Color', 'bc5')]
        if glow and n_eyes:
            maps.append(('_g', g_img, 'sRGB', 'bc7'))
        # stored top row first in Blender pixel order: images are saved as baked (Blender handles the flip)
        try:
            from . import nvtt_helpers as _nv
        except Exception:
            _nv = None
        for suf, arr, cs, fmt in maps:
            png = os.path.join(tex_dir, f"{name}{suf}.png")
            _save_png(arr.astype(np.float32), png, cs)
            dds = os.path.join(tex_dir, f"{name}{suf}.dds")
            ok_d, msg = (False, "texture converter unavailable")
            if _nv is not None:
                ok_d, msg = _nv.NVTTHelpers.convert_to_dds(png, dds, compression_format=fmt)
            if ok_d and os.path.isfile(dds):
                os.remove(png)
                files.append(dds)
            else:
                files.append(png)
                _log(lines, f"{name}{suf}: DDS conversion failed ({msg}); the PNG is left for you to convert")
        from . import bgsm_helpers as _bh
        rel = game_folder.strip("\\/").replace("/", "\\")
        d = _bh.BGSMData()
        d.diffuse_texture = f"{rel}\\{name}_d.dds"
        d.normal_texture = f"{rel}\\{name}_n.dds"
        d.smooth_spec_texture = f"{rel}\\{name}_s.dds"
        d.root_material_path = template
        d.two_sided = bool(two_sided)
        if glow and n_eyes:
            d.glow_texture = f"{rel}\\{name}_g.dds"
            d.glowmap = True
            d.emit_enabled = True
            d.emittance_color = (1.0, 1.0, 1.0)
            d.emittance_mult = float(glow_strength)
        bgsm = os.path.join(mat_dir, f"{name}.BGSM")
        with open(bgsm, "wb") as fh:
            fh.write(_bh.write_bgsm(d))
        files.append(bgsm)

        # --- the skin's own material for export (paths the NIF needs) and a viewport preview
        m = bpy.data.materials.new(f"{name}.Mat")
        m.use_nodes = True
        m["BS_Shader_Block_Name"] = "BSLightingShaderProperty"
        m["BSLSP_Shader_Name"] = f"Materials\\{rel}\\{name}.BGSM"
        tl = rel.lower()
        m["BSShaderTextureSet_Diffuse"] = f"textures\\{tl}\\{name.lower()}_d.dds"
        m["BSShaderTextureSet_Normal"] = f"textures\\{tl}\\{name.lower()}_n.dds"
        m["BSShaderTextureSet_Specular"] = f"textures\\{tl}\\{name.lower()}_s.dds"
        if glow and n_eyes:
            m["BSShaderTextureSet_Glow"] = f"textures\\{tl}\\{name.lower()}_g.dds"
        b = _principled(m)
        dpath = next((f for f in files if f.lower().endswith(("_d.dds", "_d.png"))), None)
        if b is not None and dpath:
            tn = m.node_tree.nodes.new('ShaderNodeTexImage')
            tn.image = bpy.data.images.load(dpath, check_existing=True)
            m.node_tree.links.new(tn.outputs['Color'], b.inputs['Base Color'])
        lo.data.materials.clear()
        lo.data.materials.append(m)
        _log(lines, f"Done in {time.time() - t0:.0f} s: {len(files)} files in {out_root}")
        return True, lines, files
    finally:
        for o in made_objs:
            try:
                bpy.data.objects.remove(o, do_unlink=True)
            except Exception:
                pass
        for mm in made_mats:
            try:
                bpy.data.materials.remove(mm)
            except Exception:
                pass
        for im in made_imgs:
            try:
                bpy.data.images.remove(im)
            except Exception:
                pass
        sc.render.engine = saved['engine']
        sc.view_settings.view_transform = saved['vt']
        sc.view_settings.look = saved['look']
        for o in bpy.context.view_layer.objects:
            o.select_set(o in saved['sel'])
        if saved['active'] is not None and saved['active'].name in bpy.data.objects:
            bpy.context.view_layer.objects.active = saved['active']


# -------------------------------------------------------------------------------------------- door check
def _is_navcut_box(o):
    """Imported NavCut boxes are standalone 8-corner boxes (not under the building's compound). The player
    walks through them; they only cut the navmesh."""
    return o.parent is None and len(o.data.vertices) == 8


def check_doors(context):
    """Doorways from the building's own walls (collision under a bhkCompound), then a person-sized walk
    out of each one against ALL collision in the scene. Returns (ok, lines)."""
    objs = [o for o in context.scene.objects if o.type == 'MESH']

    def under_compound(o):
        p = o.parent
        while p is not None:
            if p.name.lower().startswith('bhkcompound'):
                return True
            p = p.parent
        return False
    walls = [o for o in objs if under_compound(o)]
    if not walls:
        return False, ["No building collision found (a compound under bhkPhysicsSystem): import the building "
                       "NIF with its collision first."]
    blockers = list(walls)
    for o in objs:
        if o in walls:
            continue
        n = o.name.lower()
        is_coll = n.startswith(('bhk', 'ucx_')) or o.get('fo4_collision')
        if is_coll and not (n.startswith('bhk') and _is_navcut_box(o)):
            blockers.append(o)

    def tris(os_):
        V, T = [], []
        for o in os_:
            b = len(V)
            V += [tuple(o.matrix_world @ v.co) for v in o.data.vertices]
            T += [(b + p.vertices[0], b + p.vertices[k], b + p.vertices[k + 1]) for p in o.data.polygons
                  for k in range(1, len(p.vertices) - 1)]
        return np.array(V), np.array(T, dtype=int).reshape(-1, 3)
    WV, WT = tris(walls)
    BV, BT = tris(blockers)
    height = float(np.ptp(WV[:, 2])) or 1.0
    u = 1.0 if height >= 20.0 else 1.0 / GAME_UNITS_PER_METRE
    CELL, CAP, CLOSE = 4 * u, 18 * u, 72 * u
    HEIGHTS = (40 * u, 90 * u, 150 * u)
    floor = float(WV[:, 2].min())
    lo = np.minimum(WV.min(0), BV.min(0)) - 250 * u
    hi = np.maximum(WV.max(0), BV.max(0)) + 250 * u
    nx, ny = int((hi[0] - lo[0]) / CELL) + 1, int((hi[1] - lo[1]) / CELL) + 1
    if nx * ny > 6_000_000:
        return False, ["Too large for the door check (grid over 6 million cells)."]

    def section(V, T, z):
        g = np.zeros((nx, ny), bool)
        for t in T:
            p = V[t]
            pts = []
            for i in range(3):
                a_, b_ = p[i], p[(i + 1) % 3]
                if (a_[2] - z) * (b_[2] - z) < 0:
                    s = (z - a_[2]) / (b_[2] - a_[2])
                    pts.append(a_[:2] + (b_[:2] - a_[:2]) * s)
            if len(pts) == 2:
                n = int(np.linalg.norm(pts[1] - pts[0]) / (CELL * 0.5)) + 1
                for s in np.linspace(0, 1, n):
                    q = pts[0] + (pts[1] - pts[0]) * s
                    i, j = int((q[0] - lo[0]) / CELL), int((q[1] - lo[1]) / CELL)
                    if 0 <= i < nx and 0 <= j < ny:
                        g[i, j] = True
        return g

    def dilate(g, r):
        k = int(math.ceil(r / CELL))
        out = g.copy()
        ii, jj = np.nonzero(g)
        for di in range(-k, k + 1):
            for dj in range(-k, k + 1):
                if di * di + dj * dj <= k * k:
                    out[np.clip(ii + di, 0, nx - 1), np.clip(jj + dj, 0, ny - 1)] = True
        return out

    def flood(free, seeds):
        seen = np.zeros_like(free)
        dq = deque()
        for s in seeds:
            if free[s] and not seen[s]:
                seen[s] = True
                dq.append(s)
        while dq:
            i, j = dq.popleft()
            for a_, b_ in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
                if 0 <= a_ < nx and 0 <= b_ < ny and free[a_, b_] and not seen[a_, b_]:
                    seen[a_, b_] = True
                    dq.append((a_, b_))
        return seen
    border = ([(i, 0) for i in range(nx)] + [(i, ny - 1) for i in range(nx)] +
              [(0, j) for j in range(ny)] + [(nx - 1, j) for j in range(ny)])
    wall_secs = [section(WV, WT, floor + h) for h in HEIGHTS]
    outside = flood(~dilate(wall_secs[1] | wall_secs[0], CLOSE), border)
    foot = ~outside
    band = foot & dilate(outside, CLOSE + 12 * u) & ~dilate(outside, CLOSE - 12 * u)
    free_w = np.ones((nx, ny), bool)
    for w in wall_secs:
        free_w &= ~dilate(w, CAP)
    cand = band & free_w
    doors, taken = [], np.zeros((nx, ny), bool)
    centre = np.argwhere(foot).mean(axis=0) if foot.any() else np.array([nx / 2, ny / 2])
    for s in zip(*np.nonzero(cand)):
        if taken[s]:
            continue
        comp = flood(cand & ~taken, [s])
        taken |= comp
        idx = np.argwhere(comp)
        if len(idx) * CELL * CELL < (20 * u) ** 2:
            continue
        c = idx.mean(axis=0)
        od = c - centre
        od = od / (np.linalg.norm(od) or 1.0)
        inside_pt = tuple(np.clip((c - od * (60 * u / CELL)).astype(int), 0, [nx - 1, ny - 1]))
        if all(any(flood(~dilate(w, CAP), [inside_pt])[b] for b in border) for w in wall_secs):
            doors.append((c, od))
    if not doors:
        return False, ["No doorway found in the building's own walls (open at knee, waist and head height)."]
    blk = [section(BV, BT, floor + h) for h in HEIGHTS]
    lines, all_ok = [], True
    for n_, (c, od) in enumerate(doors):
        out_pt = tuple(np.clip((c + od * (60 * u / CELL)).astype(int), 0, [nx - 1, ny - 1]))
        res = [any(flood(~dilate(b, CAP), [out_pt])[bb] for bb in border) for b in blk]
        w = lo[:2] + c * CELL
        ok = all(res)
        all_ok &= ok
        unit = "" if u == 1.0 else " m"
        lines.append(f"Doorway {n_ + 1} at ({w[0]:.1f}, {w[1]:.1f}{unit}): way out at knee/waist/head "
                     f"{'/'.join('yes' if r else 'NO' for r in res)} -> {'CLEAR' if ok else 'BLOCKED'}")
    lines.insert(0, f"{len(doors)} doorway(s) in the building's walls; checked against {len(blockers)} collision "
                    f"pieces ({len(blockers) - len(walls)} besides the building's own), NavCut boxes walked through")
    return all_ok, lines


# ----------------------------------------------------------------------------------------------- UI / ops
class FO4SkinBuilderSettings(PropertyGroup):
    original: StringProperty(name="Original model", subtype='FILE_PATH',
                             description="The full AI model you reduced (the .glb/.fbx/.obj Meshy gave you)")
    out_root: StringProperty(name="Output folder", subtype='DIR_PATH', default=OUT_ROOT,
                             description="Data folder the Materials and Textures go into (Downloads by default; "
                                         "copy them into your mod, or point this at the mod)")
    game_folder: StringProperty(name="Game folder", default="Architecture\\Shacks",
                                description="Folder under Materials\\ and Textures\\, e.g. Architecture\\Shacks")
    name: StringProperty(name="Name", default="",
                         description="File name for the textures and material (empty: the skin object's name)")
    size: EnumProperty(name="Size", items=[('4096', "4K", "As the Thornmaw skin"), ('2048', "2K", "Smaller files")],
                       default='4096')
    colour: EnumProperty(name="Colour", items=[
        ('AGX', "As approved (slightly darker)", "The look approved on the Thornmaw shack: Meshy's colours "
                                                 "through Blender's AgX (mid-tones ~5% darker, highlights softer)"),
        ('EXACT', "Exact original colours", "Meshy's colours unchanged")], default='AGX')
    shine: EnumProperty(name="Shine", items=[
        ('WET', "Wet & living", "Levelled like your Materializer maps (mean 0.52)"),
        ('ORIGINAL', "As the original", "The original's own roughness, inverted")], default='WET')
    glow: BoolProperty(name="Glowing eyes", default=True,
                       description="Bright warm eye-sized spots glow (Mirelurk/Deathclaw style glow map)")
    glow_strength: FloatProperty(name="Glow strength", default=0.8, min=0.0, max=10.0,
                                 description="Vanilla glowing creatures use 0.5-0.8")
    two_sided: BoolProperty(name="Two-sided", default=True,
                            description="So the inside of a mouth or opening is not see-through")
    template: EnumProperty(name="Template", items=[
        ('Template/LeafTemplate_Wet.bgsm', "Wet leaf (your skins)", ""),
        ('template/CreatureTemplate_Wet.bgsm', "Wet creature (vanilla glowing creatures)", ""),
        ('', "None", "")], default='Template/LeafTemplate_Wet.bgsm')
    report: StringProperty(default="")


class FO4_OT_SkinBakeOriginal(Operator):
    bl_idname = "fo4.skin_bake_original"
    bl_label = "Bake Original onto My Remesh"
    bl_description = ("Bake the full AI model's colour, bumps, shine and glowing eyes onto your reduced skin "
                      "(every copy lined up with the original automatically), then write the DDS textures and "
                      "the material. Takes a few minutes")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        st = context.scene.fo4_skin_builder
        lo = context.active_object
        name = (st.name or (lo.name if lo else "Skin")).replace(" ", "_")
        out_root = bpy.path.abspath(st.out_root) or OUT_ROOT
        ok, lines, files = bake_original_onto_remesh(
            lo, bpy.path.abspath(st.original), out_root, st.game_folder, name, size=int(st.size),
            colour=st.colour, shine=st.shine, glow=st.glow, glow_strength=st.glow_strength,
            two_sided=st.two_sided, template=st.template)
        st.report = "\n".join(lines)
        self.report({'INFO'} if ok else {'ERROR'}, lines[-1] if lines else "nothing done")
        return {'FINISHED'} if ok else {'CANCELLED'}


class FO4_OT_SkinCheckDoors(Operator):
    bl_idname = "fo4.skin_check_doors"
    bl_label = "Check Doors"
    bl_description = ("Walk out of every doorway of the building in this scene (found from its own walls) "
                      "against all collision, skin hulls included, at knee, waist and head height")

    def execute(self, context):
        ok, lines = check_doors(context)
        context.scene.fo4_skin_builder.report = "\n".join(lines)
        self.report({'INFO'} if ok else {'WARNING'}, "All doorways clear" if ok else lines[-1])
        return {'FINISHED'}


class FO4_PT_SkinBuilderPanel(_FO4SubPanel):
    """AI skin over a building: bake, collision, door check"""
    bl_label = "Skin Builder (AI skin over a building)"
    bl_idname = "FO4_PT_skin_builder_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Fallout 4'
    bl_parent_id = "FO4_PT_mesh_panel"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        st = context.scene.fo4_skin_builder
        lay = self.layout
        box = lay.box()
        box.label(text="1. Textures (select your reduced skin)", icon='TEXTURE')
        box.prop(st, "original")
        box.prop(st, "out_root")
        row = box.row()
        row.prop(st, "game_folder")
        row.prop(st, "name")
        row = box.row()
        row.prop(st, "size")
        row.prop(st, "colour", text="")
        row = box.row()
        row.prop(st, "shine")
        row.prop(st, "template", text="")
        row = box.row()
        row.prop(st, "glow")
        row.prop(st, "glow_strength", text="Strength")
        box.prop(st, "two_sided")
        box.operator("fo4.skin_bake_original", icon='RENDER_STILL')
        box = lay.box()
        box.label(text="2. Collision on the skin (doors kept clear)", icon='MOD_PHYSICS')
        box.operator("fo4.generate_collision_mesh", text="Generate Collision", icon='MESH_DATA')
        box = lay.box()
        box.label(text="3. Check", icon='CHECKMARK')
        box.operator("fo4.skin_check_doors", icon='VIEW_PERSPECTIVE')
        if st.report:
            col = lay.box().column(align=True)
            for ln in st.report.splitlines()[-12:]:
                col.label(text=ln)


classes = (FO4SkinBuilderSettings, FO4_OT_SkinBakeOriginal, FO4_OT_SkinCheckDoors, FO4_PT_SkinBuilderPanel)


def register():
    for cls in classes:
        try:
            bpy.utils.register_class(cls)
        except Exception:
            existing = getattr(bpy.types, cls.__name__, None)
            if existing is not None:
                bpy.utils.unregister_class(existing)
            bpy.utils.register_class(cls)
    bpy.types.Scene.fo4_skin_builder = PointerProperty(type=FO4SkinBuilderSettings)


def unregister():
    try:
        del bpy.types.Scene.fo4_skin_builder
    except Exception:
        pass
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
