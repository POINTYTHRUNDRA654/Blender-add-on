"""Tree LODs built like Bethesda's: clean tapered limbs + branch-picture cards.

Added 2026-11-18 for Billy's GSMuckPool_L01 fungal trees (no leaves: trunk, wood branches,
roots) standing on their ground. Decimating thin branchy wood shredded it into shards; vanilla
tree LODs (checked on the unpacked game) are built differently:

* TreeElmFreePW01Gr_LOD_0: trunk + limb stubs 152 tris (clean, tapered), 75 alpha cards
  (150 tris) filling the crown, each a picture of real branches; _LOD_1 53 + 80 tris;
  _LOD_2 one crossed billboard. Full tree 1,401 tris.
* TreeHero01_LOD_0 (a 3D tree without leaf cards, 6,900 tris full): 1,066 tris of clean
  branching tubes + twig cards; _LOD_1 480; one baked 128x128 texture.

This module traces the tree's skeleton (geodesic bands from the base, one node per
connected slice), turns the thick part into tapered tubes and leaves the thin branches to
Tree mode's existing card renderer (tree_lod). Pure numpy + heapq so it runs headless.
"""
import heapq
import math

import numpy as np


def _weld(V, faces, tol):
    """Merge vertices closer than *tol* (UV seams split a mesh into islands that are
    really one surface). Returns (welded positions, old->new index map)."""
    key = np.round(V / tol).astype(np.int64)
    _u, inv = np.unique(key, axis=0, return_inverse=True)
    inv = inv.ravel()
    n = inv.max() + 1
    P = np.zeros((n, 3))
    cnt = np.zeros(n)
    np.add.at(P, inv, V)
    np.add.at(cnt, inv, 1)
    return P / cnt[:, None], inv


def _edges(faces, remap):
    E = set()
    for f in faces:
        g = [int(remap[i]) for i in f]
        for a, b in zip(g, g[1:] + g[:1]):
            if a != b:
                E.add((a, b) if a < b else (b, a))
    return E


def _components(n, E):
    par = list(range(n))

    def find(x):
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x
    for a, b in E:
        ra, rb = find(a), find(b)
        if ra != rb:
            par[ra] = rb
    return np.array([find(i) for i in range(n)])


def _bridge(P, E, gap):
    """AI trees are many overlapping pieces, not one surface (GSMuckPool_L01 trees: tracing
    along the surface from the base reached only the pieces touching it). Join pieces that
    touch or sit within *gap*, then any still-loose piece to its nearest neighbour, so the
    geodesic walk reaches the whole tree."""
    # The bridges are only for measuring distance from the base; slices are cut along the real
    # surface (see trace_skeleton), so two branches that merely touch stay separate limbs.
    from mathutils.kdtree import KDTree
    comp = _components(len(P), E)
    kd = KDTree(len(P))
    for i, p in enumerate(P):
        kd.insert(p, i)
    kd.balance()
    E = set(E)
    for i, p in enumerate(P):
        for _co, j, _d in kd.find_range(p, gap):
            if comp[j] != comp[i]:
                E.add((i, j) if i < j else (j, i))
    comp = _components(len(P), E)
    labels, counts = np.unique(comp, return_counts=True)
    if len(labels) > 1:
        main = labels[np.argmax(counts)]
        main_idx = np.nonzero(comp == main)[0]
        kd2 = KDTree(len(main_idx))
        for k, i in enumerate(main_idx):
            kd2.insert(P[i], k)
        kd2.balance()
        for lab in labels:
            if lab == main:
                continue
            idx = np.nonzero(comp == lab)[0]
            best = None
            for i in idx[:: max(1, len(idx) // 64)]:
                _co, k, d = kd2.find(P[i])
                if best is None or d < best[0]:
                    best = (d, i, int(main_idx[k]))
            if best is not None:
                E.add((best[1], best[2]) if best[1] < best[2] else (best[2], best[1]))
    return E


def _geodesic(P, E, sources):
    adj = [[] for _ in range(len(P))]
    for a, b in E:
        w = float(np.linalg.norm(P[a] - P[b]))
        adj[a].append((b, w))
        adj[b].append((a, w))
    d = np.full(len(P), np.inf)
    h = []
    for s in sources:
        d[s] = 0.0
        h.append((0.0, int(s)))
    heapq.heapify(h)
    while h:
        dist, u = heapq.heappop(h)
        if dist > d[u]:
            continue
        for v, w in adj[u]:
            nd = dist + w
            if nd < d[v]:
                d[v] = nd
                heapq.heappush(h, (nd, v))
    return d, adj


def trace_skeleton(V, faces, bands=48):
    """Skeleton of a tree mesh (world positions *V*, polygon index lists *faces*).

    Returns a list of nodes {"c": centre, "r": radius, "parent": index or -1, "d": geodesic
    distance from the base}. Nodes are connected slices of the surface between two geodesic
    distances from the lowest points (the base), so a fork gives two nodes in one band."""
    H = float(np.ptp(V[:, 2])) or 1.0
    P, remap = _weld(V, faces, H * 2e-4)
    E_surface = _edges(faces, remap)
    E = _bridge(P, E_surface, 0.012 * H)
    surf_adj = [[] for _ in range(len(P))]
    for a, b in E_surface:
        surf_adj[a].append(b)
        surf_adj[b].append(a)
    z = P[:, 2]
    # start at the trunk's base, not a root tip: the lowest points within reach of the
    # trunk axis (centre of the wood between 15% and 40% of the height)
    z0 = z.min()
    mid = (z > z0 + 0.15 * H) & (z < z0 + 0.40 * H)
    axis = P[mid, :2].mean(axis=0) if mid.any() else P[:, :2].mean(axis=0)
    rxy = np.linalg.norm(P[:, :2] - axis, axis=1)
    near = rxy < 0.08 * H
    if not near.any():
        near = rxy <= np.quantile(rxy, 0.05)
    zn = z[near].min()
    base = np.nonzero(near & (z <= zn + 0.02 * H))[0]
    d, adj = _geodesic(P, E, base)
    ok = np.isfinite(d)
    if not ok.any():
        return []
    dmax = float(d[ok].max())
    step = dmax / bands
    band = np.full(len(P), -1)
    band[ok] = np.minimum((d[ok] / step).astype(int), bands - 1)
    # connected pieces inside each band
    comp = np.full(len(P), -1)
    nodes = []
    for b in range(bands):
        vs = np.nonzero(band == b)[0]
        for s in vs:
            if comp[s] >= 0:
                continue
            stack, members = [s], []
            comp[s] = len(nodes)
            while stack:
                u = stack.pop()
                members.append(u)
                # across the bridges too: on these many-piece trees a slice cut along the real
                # surface only fell apart into 1-3 point fragments and the limbs broke up
                for v, _w in adj[u]:
                    if comp[v] < 0 and band[v] == b:
                        comp[v] = len(nodes)
                        stack.append(v)
            M = P[members]
            c = M.mean(axis=0)
            r = float(np.median(np.linalg.norm(M - c, axis=1)))
            nodes.append({"c": c, "r": r, "band": b, "n": len(members), "parent": -1,
                          "d": float(np.mean(d[members]))})
    # parent: the node in the band below that this piece touches most
    # (a bridge between two pieces can jump several bands, so the parent is the touching
    # node in the CLOSEST lower band, not only the band right below)
    votes = {}
    for a, b2 in E:
        ca, cb = comp[a], comp[b2]
        if ca < 0 or cb < 0 or ca == cb:
            continue
        ba, bb = nodes[ca]["band"], nodes[cb]["band"]
        if bb < ba:
            votes.setdefault(ca, {}).setdefault(cb, 0)
            votes[ca][cb] += 1
        elif ba < bb:
            votes.setdefault(cb, {}).setdefault(ca, 0)
            votes[cb][ca] += 1
    for i, vv in votes.items():
        top = max(nodes[k]["band"] for k in vv)
        cand = {k: n for k, n in vv.items() if nodes[k]["band"] == top}
        nodes[i]["parent"] = max(cand, key=cand.get)
    # Near the base one slice holds the trunk AND the starts of the roots, so its spread is
    # far wider than the trunk (a flat disc on the fungal trees). Nothing on a tree is thicker
    # than its trunk plus a root flare: cap every node at 1.6x the trunk radius measured
    # between 15% and 40% of the height near the trunk axis.
    trunk_r = [n["r"] for n in nodes
               if z0 + 0.15 * H < n["c"][2] < z0 + 0.40 * H and np.linalg.norm(n["c"][:2] - axis) < 0.08 * H
               and n["n"] >= 20]                 # real trunk slices, not 1-3 point slivers
    if trunk_r:
        cap = 1.6 * float(np.median(trunk_r))
        for n in nodes:
            n["r"] = min(n["r"], cap)
    return nodes


def chains(nodes, min_len_frac=0.04, H=None):
    """Split the node tree into limbs: chains from a fork (or the base) to the next fork or a
    tip. Short twigs (shorter than min_len_frac of the height) are dropped -- the cards carry
    them. Returns lists of node indices, base to tip, each starting at its parent's fork node."""
    kids = {}
    for i, n in enumerate(nodes):
        kids.setdefault(n["parent"], []).append(i)
    if H is None:
        Z = np.array([n["c"][2] for n in nodes])
        H = float(np.ptp(Z)) or 1.0

    # length of the longest path below each node (to judge twigs)
    reach = {}
    for i in sorted(range(len(nodes)), key=lambda k: -nodes[k]["band"]):
        best = 0.0
        for k in kids.get(i, []):
            best = max(best, reach.get(k, 0.0) + float(np.linalg.norm(nodes[k]["c"] - nodes[i]["c"])))
        reach[i] = best
    out = []
    # the base nodes: a real slice (not a 1-point sliver) with no parent
    roots = [r for r in kids.get(-1, []) if nodes[r]["n"] >= 4] or kids.get(-1, [])
    stack = [(r, -1) for r in roots]

    def total(k, cur):
        return reach.get(k, 0.0) + float(np.linalg.norm(nodes[k]["c"] - nodes[cur]["c"]))
    while stack:
        start, fork = stack.pop()
        chain = ([fork] if fork >= 0 else []) + [start]
        cur = start
        while True:
            # slivers (1-3 points) are surface bumps, not limbs
            # ...unless a long limb carries on beyond them: low-detail roots slice into 1-3 points
            # (GSFungusTree03: 50 of 83 slices below 12% of the height; its roots reach 0.42 H
            # out but the traced roots stopped after two slices)
            ks = [k for k in kids.get(cur, [])
                  if (nodes[k]["n"] >= 4 or total(k, cur) >= 0.08 * H) and total(k, cur) >= min_len_frac * H]
            if not ks:
                break
            # carry on through the child with the most tree above it (keeps the trunk one long
            # limb into the leader); every other real child starts its own limb at this fork
            main = max(ks, key=lambda k: total(k, cur))
            for k in ks:
                if k != main:
                    stack.append((k, cur))
            cur = main
            chain.append(cur)
        if len(chain) >= 2:
            out.append(chain)
    return out


# per add-on LOD key (LOD1 -> _LOD_0, LOD2 -> _LOD_1): tube sides, most limbs kept, card count,
# baked wood texture size. Vanilla TreeElmFreePW01Gr: _LOD_0 152 wood tris + 75 cards,
# _LOD_1 53 + 40; TreeHero01 (3D, no leaves) 1,066 / 480 tris.
SHAPE_LEVELS = {
    "LOD1": {"sides": 6, "limbs": 40, "cards": 60, "tex": 512},
    "LOD2": {"sides": 4, "limbs": 16, "cards": 30, "tex": 256},
}


def build_tree_shape_lods(src_obj, tree_slots, levels, safe, rel_dir, textures_root, mod_folder):
    """Clean-limb + branch-card LOD parts for every tree part of *src_obj*.

    *tree_slots*: {slot index: material} of the tree materials (fo4_lod_generator.
    _tree_material_indices). *levels*: add-on LOD keys to build ("LOD1", "LOD2").
    Returns ({level key: [objects]}, notes). Objects carry their own materials, textures
    (written under Textures\\LOD\\<rel_dir>) and .bgsm files (when *mod_folder*)."""
    import os
    import re
    import tempfile
    import bpy
    from . import tree_lod as TL
    from . import fo4_lod_generator as LG

    out = {k: [] for k in levels}
    notes = []
    tex_dir = os.path.join(textures_root, "LOD", *[p for p in rel_dir.split("\\") if p])
    mat_dir_rel = "materials\\LOD\\" + (rel_dir + "\\" if rel_dir else "")
    tmp = tempfile.mkdtemp(prefix="fo4treeshape_")
    me = src_obj.data
    for mi, mat in tree_slots.items():
        ids = [p.index for p in me.polygons if p.material_index == mi]
        if not ids:
            continue
        tag = re.sub(r"[^A-Za-z0-9_]+", "_", os.path.splitext(os.path.basename(
            str(mat.get("BSLSP_Shader_Name") or mat.name)))[0])
        tree = TL.part_object(src_obj, ids, f"{safe}_{tag}_shape_src")
        tree.hide_render = True
        try:
            V, F = TL._world_mesh(tree)
            V = np.asarray(V, float)
            H = float(np.ptp(V[:, 2])) or 1.0
            nodes = trace_skeleton(V, F, bands=48)
            limbs = chains(nodes, 0.04, H)
            labels, info = TL.split_wood_foliage(tree)
            leaf_ids = np.nonzero(labels == "leaf")[0]
            # no cards from thin bits at the very base (they made stray cards on the ground beside
            # the trees); the limbs carry the root zone
            if len(leaf_ids):
                _z0 = float(V[:, 2].min())
                _cz = np.array([V[list(F[i]), 2].mean() for i in leaf_ids])
                leaf_ids = leaf_ids[_cz >= _z0 + 0.12 * H]
            wood_ids = np.nonzero(labels == "wood")[0]
            axis_xy = (V[np.unique([v for i in wood_ids for v in F[i]])][:, :2].mean(axis=0)
                       if len(wood_ids) else V[:, :2].mean(axis=0))
            # cards: every level's tiles in one atlas per tree
            tile_sets, frames_by = [], {}
            if len(leaf_ids):
                centres = np.array([V[list(F[i])].mean(axis=0) for i in leaf_ids])
                weights = np.array([TL._face_area(V, F[i]) for i in leaf_ids]) + 1e-9
                for key in levels:
                    lab = TL.cluster_faces(centres, weights, SHAPE_LEVELS[key]["cards"])
                    frames = TL.card_frames(V, F, leaf_ids, lab, axis_xy, H)
                    by_cluster = {j: leaf_ids[lab == j] for j in set(lab.tolist())}
                    tiles = TL.render_views(tree, by_cluster, frames, TL.CARD_TILE, f"{safe}_{tag}_{key}")
                    tile_sets.append((key, tiles))
                    frames_by[key] = frames
            cmat = None
            if tile_sets:
                atlas, where, asize = TL.build_atlas(tile_sets, TL.CARD_TILE)
                cbase = f"{safe}_{tag}BranchesLOD"
                png = os.path.join(tmp, f"{cbase}_d.png")
                TL._save_png(atlas, png)
                dds = os.path.join(tex_dir, f"{cbase}_d.dds")
                ok, msg = TL._to_dds(png, dds, compression_format='bc3')
                cpaths = {"Diffuse": dds if ok else png}
                if not ok:
                    notes.append(f"⚠ {cbase}_d: DDS conversion failed ({msg}); FO4 cannot load the PNG")
                flat, fn = TL.write_flat_maps(tex_dir, cbase, tmp)
                cpaths.update(flat)
                notes.extend(fn)
                cbgsm = mat_dir_rel + cbase + ".bgsm"
                cmat = TL.alpha_material(cbase, cbgsm, cpaths, TL.CANOPY_ALPHA_REF)
                if mod_folder:
                    TL.write_bgsm(cmat, mod_folder, cbgsm, alpha_ref=TL.CANOPY_ALPHA_REF, two_sided=True)
            for key in levels:
                cfg = SHAPE_LEVELS[key]
                made = []
                # clean limbs
                vv, ff, _own = tube_mesh(nodes, limbs, sides=cfg["sides"], max_limbs=cfg["limbs"])
                if ff:
                    tme = bpy.data.meshes.new(f"{safe}_{key}_{tag}_Limbs")
                    tme.from_pydata(vv, [], ff)
                    tme.update()
                    tob = bpy.data.objects.new(tme.name, tme)
                    bpy.context.scene.collection.objects.link(tob)
                    tme.uv_layers.new(name="UVMap")
                    LG._reuv_for_bake(tob)
                    tbase = f"{safe}_{tag}LimbsLOD{int(key[-1]) - 1}"
                    tex_paths, bn = LG.bake_lod_textures(tree, tob, cfg["tex"], textures_root, rel_dir, tbase)
                    notes.extend(bn)
                    tmat = LG.make_lod_material(mat, tob, tex_paths, mat_dir_rel + tbase + ".bgsm", opaque=True)
                    tmat.use_backface_culling = True
                    if mod_folder:
                        TL.write_bgsm(tmat, mod_folder, mat_dir_rel + tbase + ".bgsm")
                    made.append(tob)
                    notes.append(f"{tag} {key}: {len(tme.polygons)} limb faces "
                                 f"({sum(len(p.vertices) - 2 for p in tme.polygons)} triangles), "
                                 f"{cfg['sides']}-sided, wood baked {cfg['tex']}px")
                # branch cards
                if cmat is not None and key in frames_by:
                    cards = TL.build_card_mesh(f"{safe}_{key}_{tag}_Branches", frames_by[key], key, where,
                                               TL.CARD_TILE, asize)
                    cards.data.materials.append(cmat)
                    made.append(cards)
                    notes.append(f"{tag} {key}: {len(cards.data.polygons)} branch cards")
                for ob in made:
                    ob["fo4_lod_level"] = key
                    ob["fo4_lod_source"] = src_obj.name
                    ob["fo4_no_wind"] = True
                    ob["PYN_GAME"] = "FO4"
                    for k in ("fo4_unit_scale", "fo4_scale_applied"):
                        if k in src_obj:
                            ob[k] = src_obj[k]
                out[key].extend(made)
        finally:
            m_ = tree.data
            bpy.data.objects.remove(tree, do_unlink=True)
            if m_.users == 0:
                bpy.data.meshes.remove(m_)
    return out, notes


def _simplify(points, tol):
    """Douglas-Peucker on a polyline; keeps the ends."""
    if len(points) <= 2:
        return list(range(len(points)))
    a, b = points[0], points[-1]
    ab = b - a
    L = float(np.linalg.norm(ab)) or 1e-9
    dist = [float(np.linalg.norm(np.cross(p - a, ab))) / L for p in points[1:-1]]
    k = int(np.argmax(dist)) + 1 if dist else 0
    if dist and dist[k - 1] > tol:
        left = _simplify(points[:k + 1], tol)
        right = _simplify(points[k:], tol)
        return left[:-1] + [i + k for i in right]
    return [0, len(points) - 1]


def tube_mesh(nodes, limb_chains, sides=6, min_radius=0.0, tol_frac=0.5, max_limbs=None):
    """Tapered tubes along the limb chains. Returns (verts, faces, per-face limb index).
    Thick limbs first; *max_limbs* keeps the most important ones (radius x length)."""
    def weight(ch):
        r = np.mean([nodes[i]["r"] for i in ch])
        L = sum(float(np.linalg.norm(nodes[ch[k + 1]]["c"] - nodes[ch[k]]["c"])) for k in range(len(ch) - 1))
        return r * L
    order = sorted(limb_chains, key=weight, reverse=True)
    order = [ch for ch in order if np.mean([nodes[i]["r"] for i in ch]) >= min_radius]
    if max_limbs:
        order = order[:max_limbs]
    verts, faces, owner = [], [], []
    for li, ch in enumerate(order):
        pts = np.array([nodes[i]["c"] for i in ch])
        rad = np.array([nodes[i]["r"] for i in ch])
        # even out slice-to-slice wobble of the centre line (ends stay put: they meet the
        # parent limb and the tip)
        for _it in range(3):
            if len(pts) > 2:
                pts[1:-1] = 0.5 * pts[1:-1] + 0.25 * (pts[:-2] + pts[2:])
        # smooth radius along the limb, never growing toward the tip; a 1-3 point slice measures
        # almost no radius, so it tapers from the slice before instead of pinching to nothing
        for k in range(1, len(rad)):
            if rad[k] < 0.35 * rad[k - 1]:
                rad[k] = rad[k - 1] * 0.85
            rad[k] = min(rad[k], rad[k - 1] * 1.05)
        keep = _simplify(pts, tol_frac * float(np.median(rad)))
        pts, rad = pts[keep], rad[keep]
        if len(pts) < 2:
            continue
        # parallel-transport frame
        t0 = pts[1] - pts[0]
        t0 /= (np.linalg.norm(t0) or 1)
        ref = np.array([0, 0, 1.0]) if abs(t0[2]) < 0.9 else np.array([1.0, 0, 0])
        nrm = np.cross(t0, ref)
        nrm /= (np.linalg.norm(nrm) or 1)
        rings = []
        for k in range(len(pts)):
            if k == 0:
                t = pts[1] - pts[0]
            elif k == len(pts) - 1:
                t = pts[-1] - pts[-2]
            else:
                t = pts[k + 1] - pts[k - 1]
            t /= (np.linalg.norm(t) or 1)
            nrm = nrm - t * float(np.dot(nrm, t))
            nrm /= (np.linalg.norm(nrm) or 1)
            bi = np.cross(t, nrm)
            is_tip = (k == len(pts) - 1)
            if is_tip:
                rings.append([len(verts)])
                verts.append(tuple(pts[k]))
                continue
            ring = []
            for s in range(sides):
                ang = 2 * math.pi * s / sides
                p = pts[k] + rad[k] * (math.cos(ang) * nrm + math.sin(ang) * bi)
                ring.append(len(verts))
                verts.append(tuple(p))
            rings.append(ring)
        for k in range(len(rings) - 1):
            A, B = rings[k], rings[k + 1]
            for s in range(sides):
                a0, a1 = A[s], A[(s + 1) % sides]
                if len(B) == 1:
                    faces.append((a0, a1, B[0]))
                else:
                    b0, b1 = B[s], B[(s + 1) % sides]
                    faces.append((a0, a1, b1, b0))
                owner.append(li)
    return verts, faces, owner
