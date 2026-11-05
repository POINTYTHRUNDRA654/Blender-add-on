"""
fo4_mesh_shaper.py
==================
Hard-surface mesh tools for Fallout 4 modding: reshape bevels and chamfers,
clean up after booleans, control shading with custom normals, keep backup
copies (stashes) of a mesh, and see the vertex count the NIF will really get.

All code here is this add-on's own. The tool set covers the same kind of
work as commercial hard-surface add-ons, written from scratch.

Bevel tools work on a selected strip of faces (a chamfer or a rounded bevel)
in Edit Mode:
  * Unbevel     collapse the strip back to a sharp edge
  * Unfuse      turn a rounded bevel back into a flat chamfer
  * Fuse        turn a flat chamfer into a rounded bevel (any segment count)
  * Change Width  make the bevel wider or narrower, keeping its shape

How the strip is read: the selection's boundary is split at its four corner
vertices (boundary vertices with no edge running into the strip) into two
rails and two caps; a closed ring has two boundary loops and no caps. Each
rail vertex is paired with the matching vertex on the other rail by walking
straight across the strip. The sharp corner for that cross-section is where
the two side faces' planes meet, cut by the plane across the strip.
"""

import math
import time

import bpy
import bmesh
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import Operator, Panel
from mathutils import Matrix, Vector
from mathutils.kdtree import KDTree

try:
    from .ui_panels import _FO4SubPanel
except Exception:  # loaded outside the add-on (tests)
    _FO4SubPanel = Panel


# ─────────────────────────────────────────────────────────────────────────────
# Strip analysis
# ─────────────────────────────────────────────────────────────────────────────

class StripError(Exception):
    """The selection is not a strip of faces the bevel tools can read."""


def _boundary_loops(boundary_edges):
    """Order boundary edges into closed vertex loops."""
    adj = {}
    for e in boundary_edges:
        a, b = e.verts
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    for v, nbrs in adj.items():
        if len(nbrs) != 2:
            raise StripError("The selection's outline crosses itself. Select one strip of "
                             "bevel faces (a chamfer or a rounded bevel).")
    loops, seen = [], set()
    for start in adj:
        if start in seen:
            continue
        loop, prev, cur = [start], None, start
        seen.add(start)
        while True:
            nxt = adj[cur][0] if adj[cur][0] is not prev else adj[cur][1]
            if nxt is start:
                break
            loop.append(nxt)
            seen.add(nxt)
            prev, cur = cur, nxt
        loops.append(loop)
    return loops


def _path_length(path):
    return sum((path[i + 1].co - path[i].co).length for i in range(len(path) - 1))


def _walk_across(start, interior, rail_b):
    """Follow interior edges straight across the strip from *start* until a
    vertex of *rail_b* is reached. Returns the vertex path."""
    edges = [e for e in start.link_edges if e in interior]
    if len(edges) != 1:
        raise StripError("Could not find the edge running across the bevel. Select only "
                         "the bevel faces, without the flat faces beside them.")
    path, e_in, cur = [start], edges[0], start
    for _ in range(10000):
        cur = e_in.other_vert(cur)
        path.append(cur)
        if cur in rail_b:
            return path
        in_faces = set(e_in.link_faces)
        nxt = [e for e in cur.link_edges if e is not e_in and e in interior
               and not (set(e.link_faces) & in_faces)]
        if len(nxt) != 1:
            raise StripError("The bevel's faces are not a clean grid of quads.")
        e_in = nxt[0]
    raise StripError("Could not walk across the bevel.")


def analyse_strip(bm, faces):
    """Read a strip of selected faces.

    Returns dict(rail_a, rail_b, sections, closed) where sections[k] is the
    vertex path from rail_a[k] across the strip to rail_b[k].
    """
    fset = set(faces)
    if not fset:
        raise StripError("Select the faces of a bevel or chamfer first (Edit Mode, face select).")
    tris = sum(1 for f in fset if len(f.verts) == 3)
    if tris * 2 >= len(fset):
        raise StripError("These faces are triangles. Meshes imported from a NIF are always "
                         "triangulated: run Face > Triangles to Quads (Alt+J) first, then select "
                         "the bevel strip again.")
    edges = {e for f in fset for e in f.edges}
    interior = {e for e in edges if len(e.link_faces) == 2 and all(lf in fset for lf in e.link_faces)}
    boundary = edges - interior
    loops = _boundary_loops(boundary)

    def n_int(v):
        return sum(1 for e in v.link_edges if e in interior)

    if len(loops) == 1:
        loop = loops[0]
        corner_idx = [i for i, v in enumerate(loop) if n_int(v) == 0]
        if len(corner_idx) != 4:
            raise StripError(f"Expected a strip with 4 corners, found {len(corner_idx)}. "
                             "Select one bevel strip at a time.")
        paths = []
        for j in range(4):
            i0, i1 = corner_idx[j], corner_idx[(j + 1) % 4]
            seg = loop[i0:i1 + 1] if i1 > i0 else loop[i0:] + loop[:i1 + 1]
            paths.append(seg)
        # Which opposite pair of boundary paths are the bevel's two sides?
        # Try both. The right one has side faces that meet at an angle (a
        # corner exists for its cross-sections); along a straight edge the
        # wrong pair's "sides" are the two end caps, which face opposite ways.
        # Counting vertices alone picked the end caps on short strips such as
        # a multi-segment bevel along one cube edge.
        candidates = []
        for r in (0, 1):
            rail_a, cap_b, rail_b_rev, cap_a = paths[r], paths[r + 1], paths[(r + 2) % 4], paths[(r + 3) % 4]
            rail_b = list(reversed(rail_b_rev))
            if len(rail_a) != len(rail_b):
                continue
            rb_set = set(rail_b)
            try:
                sections = []
                for k, a in enumerate(rail_a):
                    if k == 0:
                        sections.append(list(reversed(cap_a)))   # cap_a runs rail_b[0] -> rail_a[0]
                    elif k == len(rail_a) - 1:
                        sections.append(cap_b)                    # cap_b runs rail_a[-1] -> rail_b[-1]
                    else:
                        sections.append(_walk_across(a, interior, rb_set))
            except StripError:
                continue
            if any(s[0] is not rail_a[k] or s[-1] is not rail_b[k] for k, s in enumerate(sections)):
                continue
            strip = dict(rail_a=rail_a, rail_b=rail_b, sections=sections, closed=False)
            corners = section_corners(strip, fset)
            score = (sum(1 for c in corners if c is not None),
                     len(rail_a), _path_length(rail_a) + _path_length(rail_b))
            candidates.append((score, strip))
        if not candidates:
            raise StripError("Could not pair the two sides of the bevel.")
        return max(candidates, key=lambda c: c[0])[1]

    if len(loops) == 2:
        rail_a, loop_b = loops
        if len(rail_a) != len(loop_b):
            raise StripError("The two edges of the bevel ring have different vertex counts.")
        rb_set = set(loop_b)
        s0 = _walk_across(rail_a[0], interior, rb_set)
        s1 = _walk_across(rail_a[1], interior, rb_set)
        i0, i1 = loop_b.index(s0[-1]), loop_b.index(s1[-1])
        step = 1 if (i1 - i0) % len(loop_b) == 1 else -1
        rail_b = [loop_b[(i0 + step * k) % len(loop_b)] for k in range(len(loop_b))]
        sections = [s0, s1] + [_walk_across(a, interior, rb_set) for a in rail_a[2:]]
        for k, s in enumerate(sections):
            if s[-1] is not rail_b[k]:
                raise StripError("Could not pair the two edges of the bevel ring.")
        return dict(rail_a=rail_a, rail_b=rail_b, sections=sections, closed=True)

    raise StripError(f"The selection has {len(loops)} separate outlines. Select one bevel strip at a time.")


def _side_normal(v, rail, fset):
    """Average normal of the unselected faces along *rail* at vertex *v*."""
    idx = rail.index(v)
    rail_edges = []
    for j in (idx - 1, idx + 1):
        if 0 <= j < len(rail):
            e = v.link_edges and next((e for e in v.link_edges if e.other_vert(v) is rail[j]), None)
            if e:
                rail_edges.append(e)
    n = Vector()
    for e in rail_edges:
        for f in e.link_faces:
            if f not in fset:
                n += f.normal
    if n.length < 1e-9:  # fall back to any unselected face at v
        for f in v.link_faces:
            if f not in fset:
                n += f.normal
    return n.normalized() if n.length > 1e-9 else None


def plane_corner(pa, na, pb, nb):
    """Where plane (pa, na) meets plane (pb, nb), on the plane across the strip
    through the midpoint. None when the planes are (nearly) parallel."""
    if na is None or nb is None:
        return None
    t = na.cross(nb)
    if t.length < 1e-4:  # less than ~0.006 degrees apart: flat
        return None
    t.normalize()
    mid = (pa + pb) * 0.5
    m = Matrix((na, nb, t))
    if abs(m.determinant()) < 1e-9:
        return None
    d = Vector((na.dot(pa), nb.dot(pb), t.dot(mid)))
    return m.inverted() @ d


def _side_edge_dir(v, strip_verts, rail, fset):
    """Direction of the mesh edge that runs from rail vertex *v* out across its
    side face (the edge the bevel slid *v* along), or None if there isn't
    exactly one."""
    rail_nbrs = set()
    i = rail.index(v)
    for j in (i - 1, i + 1):
        if 0 <= j < len(rail):
            rail_nbrs.add(rail[j])
    cands = [e for e in v.link_edges
             if e.other_vert(v) not in strip_verts and e.other_vert(v) not in rail_nbrs
             and not all(f in fset for f in e.link_faces)]
    if len(cands) != 1:
        return None
    d = v.co - cands[0].other_vert(v).co
    return d.normalized() if d.length > 1e-9 else None


def _line_plane(p, d, q, n):
    den = d.dot(n)
    if abs(den) < 1e-6:
        return None
    return p + d * ((q - p).dot(n) / den)


def section_corners(strip, fset):
    """Sharp-corner point for every cross-section (None where flat).

    Where a rail vertex has a mesh edge running out across its side face, the
    bevel slid it along that edge, so the corner is where that edge's line
    meets the other side's plane: exact even on curved edges (a cylinder rim).
    Otherwise the corner is where both side planes meet, level with the
    cross-section's midpoint."""
    ra, rb = strip["rail_a"], strip["rail_b"]
    strip_verts = {v for s in strip["sections"] for v in s}
    out = []
    for k in range(len(ra)):
        a, b = ra[k], rb[k]
        na = _side_normal(a, ra, fset)
        nb = _side_normal(b, rb, fset)
        base = plane_corner(a.co.copy(), na, b.co.copy(), nb)
        if base is None:
            out.append(None)
            continue
        hits = []
        da = _side_edge_dir(a, strip_verts, ra, fset)
        if da is not None:
            h = _line_plane(a.co, da, b.co, nb)
            if h is not None:
                hits.append(h)
        db = _side_edge_dir(b, strip_verts, rb, fset)
        if db is not None:
            h = _line_plane(b.co, db, a.co, na)
            if h is not None:
                hits.append(h)
        width = (a.co - b.co).length or 1.0
        hits = [h for h in hits if (h - base).length < 4 * width]   # ignore runaway lines
        out.append(sum(hits, Vector()) / len(hits) if hits else base)
    return out


def bezier_point(a, c, b, t, tension):
    """Cubic Bezier from a to b whose handles point at corner c."""
    p1 = a + (c - a) * tension
    p2 = b + (c - b) * tension
    u = 1.0 - t
    return a * (u ** 3) + p1 * (3 * u * u * t) + p2 * (3 * u * t * t) + b * (t ** 3)


# ─────────────────────────────────────────────────────────────────────────────
# Stashes (backup copies of a mesh)
# ─────────────────────────────────────────────────────────────────────────────

_STASH_KEY = "fo4_stashes"


def _stash_names(obj):
    return [n for n in list(obj.get(_STASH_KEY, [])) if n in bpy.data.meshes]


def create_stash(obj, label="manual"):
    """Store a copy of *obj*'s mesh (as it is now) and return its name.
    The copy is a mesh datablock with a fake user: saved in the .blend, never
    in the scene, so it can never be exported."""
    if obj.mode == 'EDIT':
        obj.update_from_editmode()
    copy = obj.data.copy()
    stamp = time.strftime("%H:%M:%S")
    copy.name = f"STASH {obj.name} {stamp} {label}"
    copy.use_fake_user = True
    copy["fo4_stash_of"] = obj.name
    copy["fo4_stash_label"] = label
    copy["fo4_stash_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    names = _stash_names(obj) + [copy.name]
    obj[_STASH_KEY] = names
    return copy.name


def _auto_stash(context, obj, label):
    if getattr(context.scene, "fo4_shaper_autostash", True):
        create_stash(obj, label)


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _edit_bmesh(context):
    obj = context.edit_object
    if obj is None or obj.type != 'MESH':
        raise StripError("Go into Edit Mode on a mesh first.")
    return obj, bmesh.from_edit_mesh(obj.data)


def _selected_strip(bm):
    faces = [f for f in bm.faces if f.select]
    return faces, analyse_strip(bm, faces)


def _set_selection(bm, faces):
    for f in bm.faces:
        f.select = False
    for e in bm.edges:
        e.select = False
    for v in bm.verts:
        v.select = False
    for f in faces:
        if f.is_valid:
            f.select = True
    bm.select_flush_mode()


def _strip_faces_between(sec_a, sec_b):
    """Faces spanning two neighbouring cross-sections."""
    common = set(sec_a[0].link_faces) & set(sec_b[0].link_faces) & set(sec_a[-1].link_faces)
    return list(common)


def unfuse_strip(bm, strip):
    """Dissolve the in-between vertices of every cross-section so each section
    is a single edge (rounded bevel -> flat chamfer). Returns count removed."""
    secs = strip["sections"]
    inner = list({v for s in secs for v in s[1:-1]})
    if not inner:
        return 0
    # 1. dissolve the lengthwise edges between neighbouring cross-sections, so
    #    each span becomes one face; 2. then the in-between vertices only join
    #    two edges and can go without merging the end caps into the strip
    #    (dissolving them straight away merged every face around them).
    pairs = list(zip(secs, secs[1:])) + ([(secs[-1], secs[0])] if strip["closed"] else [])
    lengthwise = set()
    for sa, sb in pairs:
        for va, vb in zip(sa[1:-1], sb[1:-1]):
            e = bm.edges.get((va, vb))
            if e is not None:
                lengthwise.add(e)
    if lengthwise:
        bmesh.ops.dissolve_edges(bm, edges=list(lengthwise), use_verts=False)
    bmesh.ops.dissolve_verts(bm, verts=[v for v in inner if v.is_valid])
    return len(inner)


class _MeshShaperOp(Operator):
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.edit_object is not None and context.edit_object.type == 'MESH'


# ─────────────────────────────────────────────────────────────────────────────
# Bevel operators
# ─────────────────────────────────────────────────────────────────────────────

class FO4_OT_ShaperUnbevel(_MeshShaperOp):
    """Collapse the selected chamfer or rounded bevel back into a sharp edge.
    The corner is rebuilt where the two neighbouring faces' planes meet"""
    bl_idname = "fo4.shaper_unbevel"
    bl_label = "Unbevel"

    mark_sharp: BoolProperty(name="Mark Sharp", default=True,
                             description="Mark the rebuilt edge sharp so it shades as a hard corner")

    def execute(self, context):
        try:
            obj, bm = _edit_bmesh(context)
            faces, strip = _selected_strip(bm)
            fset = set(faces)
            corners = section_corners(strip, fset)
            _auto_stash(context, obj, "before Unbevel")
            pairs = [(s[0], s[-1], c) for s, c in zip(strip["sections"], corners)]
            unfuse_strip(bm, strip)
            merged, flat = [], 0
            for a, b, c in pairs:
                if c is None:
                    flat += 1
                    c = (a.co + b.co) * 0.5
                bmesh.ops.pointmerge(bm, verts=[a, b], merge_co=c)
                merged.append(a if a.is_valid else b)
            bmesh.ops.dissolve_degenerate(bm, dist=1e-6, edges=bm.edges[:])
            if self.mark_sharp:
                ms = [v for v in merged if v.is_valid]
                vs = set(ms)
                for v in ms:
                    for e in v.link_edges:
                        if e.other_vert(v) in vs:
                            e.smooth = False
            bmesh.update_edit_mesh(obj.data)
        except StripError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        msg = f"Unbevel: {len(pairs)} cross-sections collapsed to a sharp edge"
        if flat:
            msg += f" ({flat} were flat, merged at their midpoint)"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class FO4_OT_ShaperUnfuse(_MeshShaperOp):
    """Turn the selected rounded bevel back into a flat chamfer (one face across)"""
    bl_idname = "fo4.shaper_unfuse"
    bl_label = "Unfuse"

    def execute(self, context):
        try:
            obj, bm = _edit_bmesh(context)
            faces, strip = _selected_strip(bm)
            ends = [(s[0], s[-1]) for s in strip["sections"]]
            _auto_stash(context, obj, "before Unfuse")
            n = unfuse_strip(bm, strip)
            new_faces = set()
            for (a0, b0), (a1, b1) in zip(ends, ends[1:] + (ends[:1] if strip["closed"] else [])):
                new_faces.update(set(a0.link_faces) & set(a1.link_faces) & set(b0.link_faces))
            _set_selection(bm, new_faces)
            bmesh.update_edit_mesh(obj.data)
        except StripError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'} if n else {'WARNING'},
                    f"Unfuse: removed {n} in-between vertices" if n else "Already a flat chamfer")
        return {'FINISHED'}


class FO4_OT_ShaperFuse(_MeshShaperOp):
    """Turn the selected chamfer (or rounded bevel) into a smooth rounded bevel
    with the chosen number of segments"""
    bl_idname = "fo4.shaper_fuse"
    bl_label = "Fuse"

    segments: IntProperty(name="Segments", default=6, min=2, max=64,
                          description="Faces across the rounded bevel")
    tension: FloatProperty(name="Tension", default=0.55, min=0.05, max=1.0,
                           description="Roundness: 0.55 is close to a circular arc, "
                                       "higher is boxier, lower is flatter")
    smooth: BoolProperty(name="Smooth Shade", default=True,
                         description="Smooth-shade the new rounded faces")

    def execute(self, context):
        try:
            obj, bm = _edit_bmesh(context)
            faces, strip = _selected_strip(bm)
            fset = set(faces)
            corners = section_corners(strip, fset)
            ends = [(s[0], s[-1]) for s in strip["sections"]]
            _auto_stash(context, obj, "before Fuse")
            unfuse_strip(bm, strip)
            n = self.segments
            chains = []
            for (a, b), c in zip(ends, corners):
                e = bm.edges.get((a, b))
                if e is None:
                    raise StripError("Lost the edge across the bevel while rebuilding it.")
                chain, cur = [a], e
                for i in range(n - 1):
                    ne, nv = bmesh.utils.edge_split(cur, chain[-1], 1.0 / (n - i))
                    chain.append(nv)
                    cur = ne if b in ne.verts else cur
                chain.append(b)
                for i, v in enumerate(chain[1:-1], start=1):
                    t = i / n
                    v.co = bezier_point(a.co, c, b.co, t, self.tension) if c is not None \
                        else a.co.lerp(b.co, t)
                chains.append(chain)
            pairs = list(zip(chains, chains[1:]))
            if strip["closed"]:
                pairs.append((chains[-1], chains[0]))
            for ca, cb in pairs:
                face = next(iter(set(ca[0].link_faces) & set(cb[0].link_faces)
                                 & set(ca[-1].link_faces) & set(cb[-1].link_faces)), None)
                if face is None:
                    raise StripError("Could not find a bevel face to split.")
                for i in range(1, n):
                    f_new, _ = bmesh.utils.face_split(face, ca[i], cb[i])
                    # keep splitting the part that still holds the far side
                    if ca[-1] in f_new.verts:
                        face = f_new
            result = set()
            for ca, cb in pairs:
                for i in range(n):
                    result.update(set(ca[i].link_faces) & set(cb[i].link_faces)
                                  & set(ca[i + 1].link_faces) & set(cb[i + 1].link_faces))
            for f in result:
                f.smooth = self.smooth
            _set_selection(bm, result)
            bmesh.update_edit_mesh(obj.data)
        except StripError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        flat = sum(1 for c in corners if c is None)
        msg = f"Fuse: {len(chains)} cross-sections rounded into {self.segments} segments"
        if flat:
            msg += f" ({flat} were flat and stay straight)"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class FO4_OT_ShaperChangeWidth(_MeshShaperOp):
    """Make the selected chamfer or rounded bevel wider or narrower, keeping
    its profile. Each side slides along its own neighbouring face"""
    bl_idname = "fo4.shaper_change_width"
    bl_label = "Change Width"

    factor: FloatProperty(name="Width", default=1.5, min=0.01, max=20.0,
                          description="New width as a multiple of the current one")

    def execute(self, context):
        try:
            obj, bm = _edit_bmesh(context)
            faces, strip = _selected_strip(bm)
            corners = section_corners(strip, set(faces))
            _auto_stash(context, obj, "before Change Width")
            moved = 0
            for s, c in zip(strip["sections"], corners):
                if c is None:
                    continue
                for v in s:
                    v.co = c + (v.co - c) * self.factor
                moved += 1
            bmesh.update_edit_mesh(obj.data)
        except StripError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Change Width: {moved} cross-sections scaled by {self.factor:g}")
        return {'FINISHED'}


class FO4_OT_ShaperSelectStrip(_MeshShaperOp):
    """Select the whole bevel strip from one selected face of it"""
    bl_idname = "fo4.shaper_select_strip"
    bl_label = "Select Bevel Strip"

    max_angle: FloatProperty(name="Max Bend", default=30.0, min=0.0, max=89.0, subtype='NONE',
                             description="Stop where the strip bends more than this (degrees)")

    def execute(self, context):
        obj = context.edit_object
        bm = bmesh.from_edit_mesh(obj.data)
        seeds = [f for f in bm.faces if f.select]
        if not seeds:
            self.report({'ERROR'}, "Select one face of the bevel first")
            return {'CANCELLED'}
        limit = math.radians(self.max_angle)
        picked = set(seeds)

        def opposite(face, e):
            return next((oe for oe in face.edges if not (set(oe.verts) & set(e.verts))), None)

        def bend(f, e):
            other = next((lf for lf in e.link_faces if lf is not f), None)
            return (f.normal.angle(other.normal, 0.0), other) if other else (math.pi, None)

        for seed in seeds:
            if len(seed.edges) != 4:
                continue
            e0, e1 = seed.edges[0], seed.edges[1]
            pairs = [(e0, opposite(seed, e0)), (e1, opposite(seed, e1))]
            # the pair across which the strip continues bends least
            pair = min(pairs, key=lambda p: sum(bend(seed, e)[0] for e in p))
            for start_edge in pair:
                f, e = seed, start_edge
                for _ in range(100000):
                    ang, nxt = bend(f, e)
                    if nxt is None or nxt in picked or len(nxt.edges) != 4 or ang > limit:
                        break
                    picked.add(nxt)
                    e = opposite(nxt, e)
                    f = nxt
                    if e is None:
                        break
        _set_selection(bm, picked)
        bmesh.update_edit_mesh(obj.data)
        self.report({'INFO'}, f"Selected {len(picked)} bevel faces")
        return {'FINISHED'}


# ─────────────────────────────────────────────────────────────────────────────
# Boolean tools
# ─────────────────────────────────────────────────────────────────────────────

def cleanup_mesh(bm, merge_dist=0.0001, straight_angle=0.5, coplanar=False, coplanar_angle=0.5):
    """Tidy the topology booleans leave behind. Returns a dict of counts."""
    report = {}
    nv = len(bm.verts)
    bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=merge_dist)
    report["merged vertices"] = nv - len(bm.verts)
    nf = len(bm.faces)
    bmesh.ops.dissolve_degenerate(bm, dist=merge_dist, edges=bm.edges[:])
    report["degenerate faces removed"] = max(0, nf - len(bm.faces))
    loose_e = [e for e in bm.edges if not e.link_faces]
    loose_v = [v for v in bm.verts if not v.link_edges]
    if loose_e:
        bmesh.ops.delete(bm, geom=loose_e, context='EDGES')
    loose_v = [v for v in bm.verts if v.is_valid and not v.link_edges]
    if loose_v:
        bmesh.ops.delete(bm, geom=loose_v, context='VERTS')
    report["loose edges/vertices removed"] = len(loose_e) + len(loose_v)
    # vertices sitting in the middle of a straight edge (2 edges, ~180 degrees)
    lim = math.radians(straight_angle)
    straight = []
    for v in bm.verts:
        if len(v.link_edges) == 2:
            a, b = (e.other_vert(v).co - v.co for e in v.link_edges)
            if a.length > 1e-9 and b.length > 1e-9 and a.angle(-b) < lim:
                straight.append(v)
    if straight:
        bmesh.ops.dissolve_verts(bm, verts=straight)
    report["straight-edge vertices dissolved"] = len(straight)
    if coplanar:
        nf = len(bm.faces)
        bmesh.ops.dissolve_limit(bm, angle_limit=math.radians(coplanar_angle),
                                 verts=bm.verts[:], edges=bm.edges[:], delimit={'NORMAL', 'MATERIAL', 'SEAM', 'UV'})
        report["coplanar faces merged"] = max(0, nf - len(bm.faces))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    return report


class FO4_OT_ShaperBooleanCleanup(Operator):
    """Clean up the mess a boolean leaves: merge doubled vertices, remove
    zero-area faces, loose edges and vertices stranded on straight edges"""
    bl_idname = "fo4.shaper_boolean_cleanup"
    bl_label = "Boolean Cleanup"
    bl_options = {'REGISTER', 'UNDO'}

    merge_distance: FloatProperty(name="Merge Distance", default=0.0001, min=0.0, max=0.1, precision=5)
    straight_angle: FloatProperty(name="Straight Edge Tolerance", default=0.5, min=0.0, max=10.0,
                                  description="Dissolve vertices on edges straighter than this (degrees)")
    coplanar: BoolProperty(name="Merge Coplanar Faces", default=False,
                           description="Also merge faces lying flat against each other (makes n-gons; "
                                       "keeps seams, UV islands and material borders)")

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        _auto_stash(context, obj, "before Boolean Cleanup")
        if obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(obj.data)
            rep = cleanup_mesh(bm, self.merge_distance, self.straight_angle, self.coplanar)
            bmesh.update_edit_mesh(obj.data)
        else:
            bm = bmesh.new()
            bm.from_mesh(obj.data)
            rep = cleanup_mesh(bm, self.merge_distance, self.straight_angle, self.coplanar)
            bm.to_mesh(obj.data)
            bm.free()
            obj.data.update()
        parts = [f"{v} {k}" for k, v in rep.items() if v]
        self.report({'INFO'}, "Boolean Cleanup: " + (", ".join(parts) if parts else "nothing to clean"))
        return {'FINISHED'}


def _boolean_solvers():
    try:
        return [i.identifier for i in bpy.types.BooleanModifier.bl_rna.properties["solver"].enum_items]
    except Exception:
        return ['EXACT']


class FO4_OT_ShaperBooleanCut(Operator):
    """Cut, join or intersect the active mesh with the other selected meshes,
    then clean up the result. The active mesh is stashed first"""
    bl_idname = "fo4.shaper_boolean_cut"
    bl_label = "Boolean"
    bl_options = {'REGISTER', 'UNDO'}

    operation: EnumProperty(name="Operation", default='DIFFERENCE', items=[
        ('DIFFERENCE', "Cut", "Remove the other meshes' volume from the active mesh"),
        ('UNION', "Join", "Merge the other meshes into the active mesh"),
        ('INTERSECT', "Intersect", "Keep only the overlap"),
    ])
    cutters: EnumProperty(name="Cutters Afterwards", default='HIDE', items=[
        ('HIDE', "Hide", "Hide the cutter objects (kept in the scene)"),
        ('DELETE', "Delete", "Delete the cutter objects"),
        ('KEEP', "Keep", "Leave them as they are"),
    ])
    cleanup: BoolProperty(name="Clean Up Result", default=True)

    @classmethod
    def poll(cls, context):
        a = context.active_object
        return (a is not None and a.type == 'MESH' and context.mode == 'OBJECT'
                and any(o.type == 'MESH' and o is not a for o in context.selected_objects))

    def execute(self, context):
        target = context.active_object
        cutters = [o for o in context.selected_objects if o.type == 'MESH' and o is not target]
        _auto_stash(context, target, f"before Boolean {self.operation.title()}")
        solvers = _boolean_solvers()
        solver = 'EXACT' if 'EXACT' in solvers else solvers[0]
        for c in cutters:
            mod = target.modifiers.new(name="FO4_Boolean", type='BOOLEAN')
            mod.operation = self.operation
            mod.solver = solver
            mod.object = c
            with context.temp_override(object=target, active_object=target):
                bpy.ops.object.modifier_apply(modifier=mod.name)
        rep = {}
        if self.cleanup:
            bm = bmesh.new()
            bm.from_mesh(target.data)
            rep = cleanup_mesh(bm)
            bm.to_mesh(target.data)
            bm.free()
            target.data.update()
        for c in cutters:
            if self.cutters == 'DELETE':
                bpy.data.objects.remove(c, do_unlink=True)
            elif self.cutters == 'HIDE':
                c.hide_set(True)
                c.hide_render = True
        cleaned = sum(rep.values()) if rep else 0
        self.report({'INFO'}, f"Boolean {self.operation.title()} with {len(cutters)} mesh(es) "
                              f"({solver} solver); cleanup fixed {cleaned} item(s)")
        return {'FINISHED'}


# ─────────────────────────────────────────────────────────────────────────────
# Normal tools
# ─────────────────────────────────────────────────────────────────────────────

def _corner_normals(me):
    """Current per-corner (loop) normals of mesh *me* (Blender 4.1+ API)."""
    if hasattr(me, "corner_normals"):
        return [cn.vector.copy() for cn in me.corner_normals]
    me.calc_normals_split()  # older Blender
    return [l.normal.copy() for l in me.loops]


def _set_corner_normals(me, normals):
    for p in me.polygons:
        p.use_smooth = True
    me.normals_split_custom_set([tuple(n) for n in normals])
    me.update()


class _ObjectModeNormalsOp(Operator):
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def _enter(self, context):
        obj = context.active_object
        self._was_edit = obj.mode == 'EDIT'
        if self._was_edit:
            bpy.ops.object.mode_set(mode='OBJECT')
        return obj

    def _leave(self):
        if self._was_edit:
            bpy.ops.object.mode_set(mode='EDIT')


class FO4_OT_ShaperFlattenNormals(_ObjectModeNormalsOp):
    """Make the selected faces shade perfectly flat while the rest of the mesh
    stays smooth (the classic fix for shading smears on flat hard-surface panels)"""
    bl_idname = "fo4.shaper_flatten_normals"
    bl_label = "Flatten Normals"

    def execute(self, context):
        obj = self._enter(context)
        me = obj.data
        sel = [p for p in me.polygons if p.select]
        if not sel:
            self._leave()
            self.report({'ERROR'}, "Select the faces to flatten (Edit Mode, face select)")
            return {'CANCELLED'}
        normals = _corner_normals(me)
        for p in sel:
            for li in p.loop_indices:
                normals[li] = p.normal.copy()
        _set_corner_normals(me, normals)
        self._leave()
        self.report({'INFO'}, f"Flattened normals on {len(sel)} faces")
        return {'FINISHED'}


class FO4_OT_ShaperWeightedNormals(_ObjectModeNormalsOp):
    """Weight every vertex normal by the size of the faces around it, so big
    flat faces shade flat and bevels take the shading change (the standard
    game-art setup for hard-surface meshes)"""
    bl_idname = "fo4.shaper_weighted_normals"
    bl_label = "Weighted Normals"

    keep_sharp: BoolProperty(name="Keep Sharp Edges", default=True)
    weight: IntProperty(name="Weight", default=50, min=1, max=100)

    def execute(self, context):
        obj = self._enter(context)
        for p in obj.data.polygons:
            p.use_smooth = True
        mod = obj.modifiers.new(name="FO4_WeightedNormal", type='WEIGHTED_NORMAL')
        mod.mode = 'FACE_AREA'
        mod.weight = self.weight
        mod.keep_sharp = self.keep_sharp
        with context.temp_override(object=obj, active_object=obj):
            bpy.ops.object.modifier_apply(modifier=mod.name)
        self._leave()
        self.report({'INFO'}, "Weighted normals applied (face area)")
        return {'FINISHED'}


class FO4_OT_ShaperTransferNormals(_ObjectModeNormalsOp):
    """Copy shading (normals) onto the active mesh from another mesh or from
    one of its stashes, e.g. to keep a smooth surface's shading after cutting
    details into it. In Edit Mode only the selected faces change"""
    bl_idname = "fo4.shaper_transfer_normals"
    bl_label = "Transfer Normals"

    source: StringProperty(name="Stash", default="",
                           description="Stash to copy from; empty uses the other selected mesh")

    def execute(self, context):
        obj = context.active_object
        src_obj, temp = None, None
        if self.source and self.source in bpy.data.meshes:
            temp = bpy.data.objects.new("FO4_NormalSource", bpy.data.meshes[self.source])
            temp.matrix_world = obj.matrix_world
            context.scene.collection.objects.link(temp)
            src_obj = temp
        else:
            src_obj = next((o for o in context.selected_objects if o.type == 'MESH' and o is not obj), None)
        if src_obj is None:
            self.report({'ERROR'}, "Pick a stash, or select the source mesh and then the target (active)")
            return {'CANCELLED'}
        obj = self._enter(context)
        me = obj.data
        group = None
        sel_verts = [v.index for v in me.vertices if v.select]
        if self._was_edit and sel_verts:
            group = obj.vertex_groups.new(name="FO4_NormalTransfer")
            group.add(sel_verts, 1.0, 'REPLACE')
        for p in me.polygons:
            p.use_smooth = True
        mod = obj.modifiers.new(name="FO4_NormalTransfer", type='DATA_TRANSFER')
        mod.object = src_obj
        mod.use_loop_data = True
        mod.data_types_loops = {'CUSTOM_NORMAL'}
        mod.loop_mapping = 'POLYINTERP_NEAREST'
        if group:
            mod.vertex_group = group.name
        with context.temp_override(object=obj, active_object=obj):
            bpy.ops.object.modifier_apply(modifier=mod.name)
        if group:
            obj.vertex_groups.remove(group)
        if temp:
            bpy.data.objects.remove(temp, do_unlink=True)
        self._leave()
        where = f"{len(sel_verts)} selected vertices" if group else "the whole mesh"
        self.report({'INFO'}, f"Normals transferred from {self.source or src_obj.name} to {where}")
        return {'FINISHED'}


class FO4_OT_ShaperSymmetrize(_ObjectModeNormalsOp):
    """Mirror one half of the mesh onto the other, keeping custom normals
    (Blender's own Symmetrize drops them)"""
    bl_idname = "fo4.shaper_symmetrize"
    bl_label = "Symmetrize (keep normals)"

    direction: EnumProperty(name="Direction", default='NEGATIVE_X', items=[
        ('NEGATIVE_X', "-X to +X", ""), ('POSITIVE_X', "+X to -X", ""),
        ('NEGATIVE_Y', "-Y to +Y", ""), ('POSITIVE_Y', "+Y to -Y", ""),
        ('NEGATIVE_Z', "-Z to +Z", ""), ('POSITIVE_Z', "+Z to -Z", ""),
    ])

    def execute(self, context):
        obj = self._enter(context)
        me = obj.data
        axis = "XYZ".index(self.direction[-1])
        keep_sign = -1.0 if self.direction.startswith("NEGATIVE") else 1.0
        old_normals = _corner_normals(me)
        size = max(obj.dimensions) or 1.0
        eps = size * 1e-3
        kd = KDTree(len(me.loops))
        recorded = []
        for p in me.polygons:
            for li in p.loop_indices:
                v = me.vertices[me.loops[li].vertex_index].co
                kd.insert(v + p.normal * eps, len(recorded))
                recorded.append(old_normals[li])
        kd.balance()
        _auto_stash(context, obj, "before Symmetrize")
        bm = bmesh.new()
        bm.from_mesh(me)
        bmesh.ops.symmetrize(bm, input=bm.verts[:] + bm.edges[:] + bm.faces[:],
                             direction=self.direction.replace("NEGATIVE_", "-").replace("POSITIVE_", ""),
                             dist=size * 1e-5)
        bm.to_mesh(me)
        bm.free()
        me.update()

        def mirror(vec):
            m = vec.copy()
            m[axis] = -m[axis]
            return m

        new = []
        for p in me.polygons:
            on_kept = p.center[axis] * keep_sign >= 0
            for li in p.loop_indices:
                v = me.vertices[me.loops[li].vertex_index].co
                q = v + p.normal * eps
                if on_kept:
                    _, idx, _ = kd.find(q)
                    new.append(recorded[idx])
                else:
                    _, idx, _ = kd.find(mirror(q))
                    new.append(mirror(recorded[idx]))
        _set_corner_normals(me, new)
        self._leave()
        self.report({'INFO'}, f"Symmetrized ({self.direction.replace('_', ' ').title()}) with normals kept")
        return {'FINISHED'}


class FO4_OT_ShaperClearNormals(_ObjectModeNormalsOp):
    """Remove custom normals so Blender calculates them again"""
    bl_idname = "fo4.shaper_clear_normals"
    bl_label = "Clear Custom Normals"

    def execute(self, context):
        obj = self._enter(context)
        with context.temp_override(object=obj, active_object=obj):
            bpy.ops.mesh.customdata_custom_splitnormals_clear()
        self._leave()
        self.report({'INFO'}, "Custom normals cleared")
        return {'FINISHED'}


# ─────────────────────────────────────────────────────────────────────────────
# Stash operators
# ─────────────────────────────────────────────────────────────────────────────

class FO4_OT_ShaperStash(Operator):
    """Save a backup copy of this mesh as it is now. Stashes stay in the .blend
    file but never in the scene, so they are never exported"""
    bl_idname = "fo4.shaper_stash"
    bl_label = "Stash Mesh"
    bl_options = {'REGISTER', 'UNDO'}

    label: StringProperty(name="Label", default="manual")

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        name = create_stash(context.active_object, self.label)
        self.report({'INFO'}, f"Stashed: {name}")
        return {'FINISHED'}


class FO4_OT_ShaperStashRestore(Operator):
    """Put a stashed copy back as the mesh (the current mesh is stashed first)"""
    bl_idname = "fo4.shaper_stash_restore"
    bl_label = "Restore Stash"
    bl_options = {'REGISTER', 'UNDO'}

    stash: StringProperty()

    def execute(self, context):
        obj = context.active_object
        if obj is None or self.stash not in bpy.data.meshes:
            self.report({'ERROR'}, "That stash no longer exists")
            return {'CANCELLED'}
        was_edit = obj.mode == 'EDIT'
        if was_edit:
            bpy.ops.object.mode_set(mode='OBJECT')
        create_stash(obj, "before Restore")
        old = obj.data
        new = bpy.data.meshes[self.stash].copy()
        new.use_fake_user = False
        for k in ("fo4_stash_of", "fo4_stash_label", "fo4_stash_time"):
            if k in new:
                del new[k]
        new.name = old.name
        obj.data = new
        if old.users == 0:
            bpy.data.meshes.remove(old)
        if was_edit:
            bpy.ops.object.mode_set(mode='EDIT')
        self.report({'INFO'}, f"Restored {self.stash}")
        return {'FINISHED'}


class FO4_OT_ShaperStashDelete(Operator):
    """Delete a stash"""
    bl_idname = "fo4.shaper_stash_delete"
    bl_label = "Delete Stash"
    bl_options = {'REGISTER', 'UNDO'}

    stash: StringProperty()

    def execute(self, context):
        obj = context.active_object
        me = bpy.data.meshes.get(self.stash)
        if me is not None and me.get("fo4_stash_of"):
            me.use_fake_user = False
            if me.users == 0:
                bpy.data.meshes.remove(me)
        if obj is not None:
            obj[_STASH_KEY] = [n for n in _stash_names(obj) if n != self.stash]
        self.report({'INFO'}, f"Deleted {self.stash}")
        return {'FINISHED'}


# ─────────────────────────────────────────────────────────────────────────────
# Export vertex count
# ─────────────────────────────────────────────────────────────────────────────

FO4_MAX_SHAPE_VERTICES = 65535   # BSTriShape stores its vertex count as a 16-bit number


def nif_vertex_count(me):
    """Vertices the NIF will hold: a vertex is written once per distinct
    (position, UV, normal) it carries, so UV seams and hard edges split it."""
    normals = _corner_normals(me)
    uv = me.uv_layers.active.data if me.uv_layers.active else None
    # per (vertex, UV): the distinct normals, counting normals within 1 degree
    # as one (custom normals are stored compressed, so corners that share a
    # normal can differ in the last digits; the exporter keeps them together)
    groups = {}
    cos_tol = math.cos(math.radians(1.0))
    for li, loop in enumerate(me.loops):
        key = (loop.vertex_index,
               (round(uv[li].uv[0], 5), round(uv[li].uv[1], 5)) if uv else None)
        n = normals[li]
        reps = groups.setdefault(key, [])
        if not any(n.dot(r) >= cos_tol for r in reps):
            reps.append(n)
    tris = sum(len(p.vertices) - 2 for p in me.polygons)
    return sum(len(r) for r in groups.values()), tris


class FO4_OT_ShaperExportCount(Operator):
    """Count the vertices and triangles this mesh will have in the NIF. UV seams
    and hard edges split vertices, so this is usually more than Blender shows"""
    bl_idname = "fo4.shaper_export_count"
    bl_label = "NIF Vertex Count"

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        if obj.mode == 'EDIT':
            obj.update_from_editmode()
        dg = context.evaluated_depsgraph_get()
        ev = obj.evaluated_get(dg)
        me = ev.to_mesh()
        try:
            verts, tris = nif_vertex_count(me)
        finally:
            ev.to_mesh_clear()
        level = {'WARNING'} if verts > FO4_MAX_SHAPE_VERTICES else {'INFO'}
        msg = (f"{obj.name}: about {verts:,} NIF vertices ({len(obj.data.vertices):,} in Blender), "
               f"{tris:,} triangles")
        if verts > FO4_MAX_SHAPE_VERTICES:
            msg += f". Over the {FO4_MAX_SHAPE_VERTICES:,} a single FO4 shape can hold: split the mesh"
        self.report(level, msg)
        return {'FINISHED'}


# ─────────────────────────────────────────────────────────────────────────────
# Panel
# ─────────────────────────────────────────────────────────────────────────────

def _retopoflow_installed():
    try:
        bpy.ops.cgcookie.retopoflow.get_rna_type()
        return True
    except Exception:
        return False


class FO4_PT_MeshShaperPanel(_FO4SubPanel):
    """Hard-surface tools: bevels, booleans, normals, stashes"""
    bl_label = "Mesh Shaper (Hard Surface)"
    bl_idname = "FO4_PT_mesh_shaper_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Fallout 4'
    bl_parent_id = "FO4_PT_mesh_panel"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        editing = context.mode == 'EDIT_MESH'

        box = layout.box()
        box.label(text="Bevels (Edit Mode: select the bevel faces)", icon='MOD_BEVEL')
        col = box.column(align=True)
        col.enabled = editing
        col.operator("fo4.shaper_select_strip", icon='RESTRICT_SELECT_OFF')
        row = col.row(align=True)
        row.operator("fo4.shaper_unbevel", icon='SNAP_EDGE')
        row.operator("fo4.shaper_unfuse", icon='MOD_SIMPLIFY')
        row = col.row(align=True)
        row.operator("fo4.shaper_fuse", icon='SPHERECURVE')
        row.operator("fo4.shaper_change_width", icon='ARROW_LEFTRIGHT')

        box = layout.box()
        box.label(text="Booleans", icon='MOD_BOOLEAN')
        col = box.column(align=True)
        row = col.row(align=True)
        for op, text in (('DIFFERENCE', "Cut"), ('UNION', "Join"), ('INTERSECT', "Intersect")):
            row.operator("fo4.shaper_boolean_cut", text=text).operation = op
        col.operator("fo4.shaper_boolean_cleanup", icon='BRUSH_DATA')

        box = layout.box()
        box.label(text="Normals (shading)", icon='NORMALS_FACE')
        col = box.column(align=True)
        col.operator("fo4.shaper_flatten_normals", icon='FACESEL')
        col.operator("fo4.shaper_weighted_normals", icon='MOD_NORMALEDIT')
        col.operator("fo4.shaper_transfer_normals", icon='MOD_DATA_TRANSFER').source = ""
        col.operator("fo4.shaper_symmetrize", icon='MOD_MIRROR')
        col.operator("fo4.shaper_clear_normals", icon='X')

        box = layout.box()
        box.label(text="Stashes (backup copies)", icon='FILE_BACKUP')
        box.prop(context.scene, "fo4_shaper_autostash")
        box.operator("fo4.shaper_stash", icon='ADD')
        if obj is not None and obj.type == 'MESH':
            for name in reversed(_stash_names(obj)[-8:]):
                me = bpy.data.meshes.get(name)
                row = box.row(align=True)
                row.label(text=f"{me.get('fo4_stash_time', '')[11:]}  {me.get('fo4_stash_label', '')}", icon='MESH_DATA')
                row.operator("fo4.shaper_stash_restore", text="", icon='LOOP_BACK').stash = name
                row.operator("fo4.shaper_transfer_normals", text="", icon='MOD_DATA_TRANSFER').source = name
                row.operator("fo4.shaper_stash_delete", text="", icon='TRASH').stash = name

        box = layout.box()
        box.label(text="Retopology", icon='MOD_REMESH')
        box.operator("fo4.retopo_and_bake", text="Retopologize & Bake (automatic)", icon='AUTO')
        if _retopoflow_installed():
            # RetopoFlow (Orange Turbine / CG Cookie, GPL-3.0) is a separate
            # add-on: draw a clean low-poly mesh over the selected high-poly
            box.operator("cgcookie.retopoflow", text="Retopologize by Hand (RetopoFlow)",
                         icon='GREASEPENCIL')
        else:
            box.label(text="For hand retopology, install RetopoFlow (free, GPL)", icon='INFO')
            box.operator("wm.url_open", text="RetopoFlow releases",
                         icon='URL').url = "https://github.com/CGCookie/retopoflow/releases"

        box = layout.box()
        box.label(text="Fallout 4 check", icon='CHECKMARK')
        box.operator("fo4.shaper_export_count", icon='INFO')


classes = (
    FO4_OT_ShaperUnbevel,
    FO4_OT_ShaperUnfuse,
    FO4_OT_ShaperFuse,
    FO4_OT_ShaperChangeWidth,
    FO4_OT_ShaperSelectStrip,
    FO4_OT_ShaperBooleanCleanup,
    FO4_OT_ShaperBooleanCut,
    FO4_OT_ShaperFlattenNormals,
    FO4_OT_ShaperWeightedNormals,
    FO4_OT_ShaperTransferNormals,
    FO4_OT_ShaperSymmetrize,
    FO4_OT_ShaperClearNormals,
    FO4_OT_ShaperStash,
    FO4_OT_ShaperStashRestore,
    FO4_OT_ShaperStashDelete,
    FO4_OT_ShaperExportCount,
    FO4_PT_MeshShaperPanel,
)


def register():
    bpy.types.Scene.fo4_shaper_autostash = BoolProperty(
        name="Stash Before Each Edit", default=True,
        description="Keep a backup copy of the mesh before every Mesh Shaper edit")
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
    try:
        del bpy.types.Scene.fo4_shaper_autostash
    except Exception:
        pass
