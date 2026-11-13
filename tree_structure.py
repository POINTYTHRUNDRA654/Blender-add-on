"""Find the solid woody core of a tree mesh (trunk, thick limbs, roots) and use it
for collision and wind when the leaves are not separate from the trunk.

AI-generated and baked trees are often ONE mesh with ONE material: trunk and
leaves fused (Billy's 2026-11-12 tree: 9,317 of 11,000 faces in one connected
piece). Splitting by loose parts or by material then sees one piece, so the
collision became one hull around the whole canopy and the wind ramp moved the
trunk as much as the leaves (thick trunk faces 0.28 at the base -> 0.91 at the top).

Woody core = faces whose surface encloses a real volume: a ray shot inward from
the face travels at least CORE_THICKNESS of the tree's height before it leaves
through the back of the same solid. Leaves are thin shells or flat cards (on that
tree: median leaf thickness 0.45% of the height, only 2% of leaf faces reach 3%;
the trunk reaches 12%).

What vanilla does with the core (measured 2026-11-12 on the unpacked game):
* Collision: BlastedForestFungalTree Large/Medium/Small use about 9 separate
  convex pieces along the trunk, root flare and crown masses, smaller than the
  visible tree (Large: 794 x 747 vs 1118 x 1238); BlastedForestBurntTreeUpright01
  uses one tight trunk piece (154 x 195 vs 288 x 366). No hull over the leaves.
* Wind, one-piece trees (TreeElmForest01, TreeElmFree01,
  TreeMapleInstitute01Green): the bottom 20% is 0, and above that the wind
  grows with distance from the trunk, not with height: 0.03-0.06 within 5% of
  the height from the trunk axis, 0.14-0.37 at 5-15%, 0.22-0.90 at 15-30%, up
  to 0.86-1.0 at the outer canopy. Trees with separate trunk and leaf shapes
  (TreeMapleForest1) keep the trunk material without wind at all.

Pure functions (mathutils + numpy only) so they can be tested headless.
"""
import math

import numpy as np

CORE_THICKNESS = 0.03       # of the tree height (see module docstring)
MIN_CORE_PART = 0.01        # a core part smaller than this share of the core is noise

# Wind on a one-piece tree, fitted 2026-11-12 to Bethesda's four one-piece wind
# trees (TreeElmForest01/02, TreeElmFree01, TreeMapleInstitute01Green): weight =
# smoothstep(distance from the TRUNK surface at that height) x smoothstep(height),
# then evened out where it breaks along an edge (smooth_along_edges).
# Re-painting those four trees and comparing vertex by vertex: mean difference
# from Bethesda's own values 38.9/255 (32.9 before the tearing guard), against
# 43.5/255 for the per-piece ramp used before; better on 3 of 4 trees, and the
# trunk stays almost still like vanilla's (0.00-0.06) instead of swaying.
# Measuring from the trunk's centre line scored 29.1 on vanilla's thin trunks
# but let a swollen trunk sway (Billy's tree: upper trunk 0.39-0.43), so the
# trunk radius per height is subtracted. Vanilla's low-poly trees have many
# edges jumping > 0.40 (406 of 2,562 on TreeElmForest01) because their edges
# are long; on a dense mesh the same jump over a short edge stretches, so the
# guard limits the change per length and per edge.
WIND_DIST = (0.04, 0.15)    # distance from the trunk (of height): no wind -> full wind
WIND_HEIGHT = (0.05, 0.55)  # height (of height): anchored base -> free crown
TRUNK_BINS = 24


def _smoothstep(x, a, b):
    t = np.clip((x - a) / max(b - a, 1e-9), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def face_thickness(verts, faces, height):
    """Per face: how far a ray shot inward from the face centre travels before it
    leaves the same closed surface (0 when it does not hit a back face: a thin
    sheet or flat card). *verts* are (x, y, z) tuples, *faces* index lists."""
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree
    bvh = BVHTree.FromPolygons([Vector(v) for v in verts], [list(f) for f in faces])
    V = np.asarray(verts, dtype=float)
    out = np.zeros(len(faces))
    eps = 1e-4 * max(height, 1e-6)
    for i, f in enumerate(faces):
        pts = V[list(f)]
        # Newell normal: robust for quads and n-gons
        n = np.zeros(3)
        for k in range(len(pts)):
            a, b = pts[k], pts[(k + 1) % len(pts)]
            n += ((a[1] - b[1]) * (a[2] + b[2]), (a[2] - b[2]) * (a[0] + b[0]), (a[0] - b[0]) * (a[1] + b[1]))
        ln = np.linalg.norm(n)
        if ln < 1e-12:
            continue
        d = Vector(tuple(-n / ln))
        c = Vector(tuple(pts.mean(axis=0)))
        hit, hn, _hi, dist = bvh.ray_cast(c + d * eps, d, height)
        if hit is not None and hn.dot(d) > 0:
            out[i] = dist
    return out


def core_faces(verts, faces, height=None, thickness=CORE_THICKNESS):
    """Boolean mask of woody-core faces, small stray parts removed."""
    V = np.asarray(verts, dtype=float)
    if height is None:
        height = float(V[:, 2].max() - V[:, 2].min()) or 1.0
    th = face_thickness(verts, faces, height)
    core = th >= thickness * height
    if not core.any():
        return core
    # connected parts of core faces (shared vertices), drop tiny ones
    idx = np.nonzero(core)[0]
    par = {int(i): int(i) for i in idx}

    def find(a):
        while par[a] != a:
            par[a] = par[par[a]]
            a = par[a]
        return a
    owner = {}
    for i in idx:
        for v in faces[i]:
            j = owner.setdefault(v, int(i))
            if j != i:
                ra, rb = find(int(i)), find(j)
                if ra != rb:
                    par[ra] = rb
    groups = {}
    for i in idx:
        groups.setdefault(find(int(i)), []).append(int(i))
    keep = np.zeros(len(faces), bool)
    for g in groups.values():
        if len(g) >= MIN_CORE_PART * len(idx):
            keep[g] = True
    return keep


def core_vertices(verts, faces, face_mask):
    m = np.zeros(len(verts), bool)
    for i in np.nonzero(face_mask)[0]:
        m[list(faces[i])] = True
    return m


def trunk_centres(V, core_mask, H, z0, bins=TRUNK_BINS):
    """Trunk centre (x, y) and radius per height slice: follow the woody core up
    from the base, staying with the part nearest the slice below (a leaning or
    curved trunk is followed; a side limb is not). The radius is the median
    distance of that slice's trunk surface from the centre, so a swollen trunk
    (a bulb or burl) counts as trunk, not as canopy."""
    C = V[core_mask]
    zc = (C[:, 2] - z0) / H
    centres, radii, prev, prev_r = [], [], None, 0.0
    for b in range(bins):
        m = (zc >= b / bins) & (zc < (b + 1) / bins)
        if m.sum() >= 3:
            pts = C[m][:, :2]
            if prev is None:
                c = pts.mean(0)
            else:
                d = np.linalg.norm(pts - prev, axis=1)
                near = pts[d <= max(np.percentile(d, 30), 0.03 * H)]
                c = near.mean(0) if len(near) else prev
                step = np.linalg.norm(c - prev)
                if step > 0.08 * H:
                    c = prev + (c - prev) / step * 0.08 * H
            dc = np.linalg.norm(pts - c, axis=1)
            ring = dc[dc <= 0.15 * H]
            prev_r = float(np.median(ring)) if len(ring) else prev_r
            prev = c
        centres.append(prev)
        radii.append(prev_r)
    first = next((c for c in centres if c is not None), V[:, :2].mean(0))
    return (np.array([c if c is not None else first for c in centres]),
            np.array(radii))


def is_fused_tree(verts, edges, core_mask):
    """True when trunk and leaves are one connected piece: the biggest piece holds
    most of the mesh and contains both woody core and thin parts. A tree built
    from separate trunk and leaf pieces keeps the per-piece painting."""
    n = len(verts)
    par = list(range(n))

    def find(a):
        while par[a] != a:
            par[a] = par[par[a]]
            a = par[a]
        return a
    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            par[ra] = rb
    roots = np.array([find(i) for i in range(n)])
    vals, counts = np.unique(roots, return_counts=True)
    big = vals[np.argmax(counts)]
    inbig = roots == big
    if inbig.mean() < 0.5:
        return False
    core_in = core_mask[inbig].mean()
    return 0.02 <= core_in <= 0.85


def tree_wind_weights(verts, faces, max_weight=1.0, core_mask=None,
                      dist=WIND_DIST, height=WIND_HEIGHT, edges=None):
    """Vanilla-style wind for a tree whose leaves are part of the trunk mesh:
    weight = (distance from the trunk at that height) x (height above the base).
    The trunk stays still, the outer canopy moves most, the base is anchored.
    Returns a list of weights, or None when the mesh has no usable woody core."""
    V = np.asarray(verts, dtype=float)
    if len(V) < 8:
        return None
    z0, z1 = V[:, 2].min(), V[:, 2].max()
    H = float(z1 - z0) or 1.0
    if core_mask is None:
        core_mask = core_vertices(verts, faces, core_faces(verts, faces, H))
    share = float(core_mask.mean())
    if share < 0.02 or share > 0.9:
        return None
    cen, rad = trunk_centres(V, core_mask, H, z0)
    zf = (V[:, 2] - z0) / H
    # trunk centre and radius blended between slices (no steps at slice edges)
    mids = (np.arange(TRUNK_BINS) + 0.5) / TRUNK_BINS
    cx = np.interp(zf, mids, cen[:, 0])
    cy = np.interp(zf, mids, cen[:, 1])
    cr = np.interp(zf, mids, rad)
    # distance from the trunk SURFACE at that height
    r = np.maximum(np.hypot(V[:, 0] - cx, V[:, 1] - cy) - cr, 0.0) / H
    w = _smoothstep(r, *dist) * _smoothstep(zf, *height)
    w = smooth_along_edges(w, V, faces, H, edges=edges)
    return (np.clip(w, 0.0, 1.0) * max_weight).tolist()


# Steepest change allowed along an edge, per tree height: the painting itself
# rises at most ~1.5 / (0.11 H) = 13.6 / H, so only real breaks (a leaf joined
# straight onto the trunk) are evened out. MAX_EDGE_JUMP keeps every edge under
# the export check's 0.40 tearing warning.
MAX_SLOPE = 1.0 / 0.06
MAX_EDGE_JUMP = 0.35


def smooth_along_edges(w, V, faces, H, max_slope=MAX_SLOPE, max_jump=MAX_EDGE_JUMP, max_iter=150,
                       edges=None):
    """Even out the weights along the mesh's own edges where they change faster
    than the painting itself can (a break, not a gradient), so nothing stretches
    in the wind. Flat areas and gentle gradients keep their values. Pass the
    mesh's *edges* to include loose edges that belong to no face."""
    a, b = [], []
    if edges is not None:
        for e in edges:
            a.append(e[0]); b.append(e[1])
    else:
        for f in faces:
            f = list(f)
            for k in range(len(f)):
                a.append(f[k]); b.append(f[(k + 1) % len(f)])
    V = np.asarray(V, dtype=float)
    if not a:
        return np.asarray(w, dtype=float)
    a = np.asarray(a); b = np.asarray(b)
    n = len(w)
    length = np.linalg.norm(V[a] - V[b], axis=1) / max(H, 1e-9)
    allowed = np.minimum(length * max_slope, max_jump)
    deg = np.maximum(np.bincount(a, minlength=n) + np.bincount(b, minlength=n), 1)
    # vertices at the same spot (UV seams, split normals) are welded on export:
    # keep them on one value so the copies cannot drift apart
    key = np.round(np.asarray(V, dtype=float) / max(1e-5 * H, 1e-9)).astype(np.int64)
    _, grp = np.unique(key, axis=0, return_inverse=True)
    grp = grp.ravel()
    gcount = np.bincount(grp)

    def weld(x):
        return (np.bincount(grp, weights=x) / gcount)[grp]
    w = weld(np.asarray(w, dtype=float))
    for _ in range(max_iter):
        bad = np.abs(w[a] - w[b]) > allowed + 1e-6
        if not bad.any():
            break
        s = np.bincount(a, weights=w[b], minlength=n) + np.bincount(b, weights=w[a], minlength=n)
        avg = s / deg
        steep = np.zeros(n, bool)
        steep[a[bad]] = True
        steep[b[bad]] = True
        w[steep] = 0.5 * w[steep] + 0.5 * avg[steep]
        # a long edge whose two sides each agree with themselves does not move by
        # averaging: pull its two ends toward each other directly
        for k in np.nonzero(np.abs(w[a] - w[b]) > allowed + 1e-6)[0]:
            i, j = a[k], b[k]
            mid = 0.5 * (w[i] + w[j])
            half = 0.5 * allowed[k] * 0.98
            if w[i] > w[j]:
                w[i], w[j] = mid + half, mid - half
            else:
                w[i], w[j] = mid - half, mid + half
        w = weld(w)
    return w


def core_collision_pieces(verts, faces, max_pieces=12, piece_size=0.2, core_mask_faces=None):
    """Split the woody core into compact parts for one convex hull each.
    Returns a list of point lists, or None without a usable core."""
    V = np.asarray(verts, dtype=float)
    if len(V) < 8:
        return None
    H = float(V[:, 2].max() - V[:, 2].min()) or 1.0
    cf = core_faces(verts, faces, H) if core_mask_faces is None else core_mask_faces
    fidx = np.nonzero(cf)[0]
    if len(fidx) < 4:
        return None
    cent = np.array([V[list(faces[i])].mean(axis=0) for i in fidx])
    ext = cent.max(axis=0) - cent.min(axis=0)
    k = int(min(max_pieces, max(1, math.ceil(float(np.linalg.norm(ext)) / (piece_size * H)))))
    # k-means (farthest-point start, deterministic)
    cs = [cent[np.argmin(cent[:, 2])]]
    for _ in range(1, k):
        dd = np.min(np.linalg.norm(cent[:, None, :] - np.array(cs)[None, :, :], axis=2), axis=1)
        cs.append(cent[int(np.argmax(dd))])
    C = np.array(cs)
    for _ in range(30):
        lab = np.argmin(np.linalg.norm(cent[:, None, :] - C[None, :, :], axis=2), axis=1)
        newC = np.array([cent[lab == j].mean(axis=0) if np.any(lab == j) else C[j] for j in range(k)])
        if np.allclose(newC, C):
            break
        C = newC
    pieces = []
    for j in range(k):
        fs = fidx[lab == j]
        if len(fs) == 0:
            continue
        vi = sorted({v for i in fs for v in faces[i]})
        if len(vi) >= 4:
            pieces.append(V[vi].tolist())
    pieces.sort(key=lambda p: -len(p))
    return pieces or None
