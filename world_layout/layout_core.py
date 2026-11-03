"""
layout_core.py -- engine-neutral math + file writers for the
Unreal/Unity -> Fallout 4 world-layout pipeline.

Pure Python, no bpy / unreal / numpy imports, so it runs inside the Unreal
Editor's Python, plain CPython (tests), or Blender alike.  The Unity side
(C#) mirrors this file in FO4LayoutMath.cs; both are checked against the
same vectors in tests/vectors.json.

See SPEC.md for the file format and the reasoning behind every constant.

Conventions used throughout
---------------------------
* Matrices are 3x3 row-major lists, applied to COLUMN vectors (v' = M v).
* A rotation matrix's columns are the object's local X, Y, Z axes expressed
  in world space -- that is exactly what Unreal's forward/right/up vectors
  and Unity's transform.right/up/forward give us, so we never have to
  trust either engine's Euler-angle order.
* FO4 world: X = east, Y = north, Z = up, right-handed, 1 unit = 0.0142875 m.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re

FORMAT_VERSION = 1

FO4_M_PER_UNIT = 0.0142875      # Havok world scale, matches fo4_ck_cell.py
FO4_CELL_SIZE = 4096.0          # exterior cell edge, game units
FO4_REF_SCALE_MIN = 0.01        # CK refuses ref scales outside this range
FO4_REF_SCALE_MAX = 10.0

# ---------------------------------------------------------------------------
# Source-engine profiles
# ---------------------------------------------------------------------------
# world_axes  W : fo4_vec = W @ src_vec   (det = -1: both engines are
#                 left-handed, FO4 is right-handed)
# mesh_yaw_offset_deg : the Blender converters bake an extra yaw into every
#                 NIF relative to W (derived from the FBX axis settings they
#                 import with -- see SPEC.md "Mesh basis").  NIF-local
#                 coords = Rz(offset) @ W @ src-local coords.
#                 UNVERIFIED until the calibration scene has been checked
#                 in the CK; override per export if it turns out wrong.
PROFILES = {
    "unreal": {
        "metres_per_unit": 0.01,                    # centimetres
        # Unreal top view: +X up the screen (north), +Y right (east)
        "world_axes": [[0, 1, 0],
                       [1, 0, 0],
                       [0, 0, 1]],
        "mesh_yaw_offset_deg": 180.0,
    },
    "unity": {
        "metres_per_unit": 1.0,                     # metres
        # Unity: +X right (east), +Z forward (north), +Y up
        "world_axes": [[1, 0, 0],
                       [0, 0, 1],
                       [0, 1, 0]],
        "mesh_yaw_offset_deg": 180.0,
    },
}


# ---------------------------------------------------------------------------
# Small matrix helpers
# ---------------------------------------------------------------------------

def mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
            for i in range(3)]


def mat_vec(m, v):
    return [sum(m[i][k] * v[k] for k in range(3)) for i in range(3)]


def transpose(m):
    return [[m[j][i] for j in range(3)] for i in range(3)]


def det(m):
    return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))


def rot_x(a):
    c, s = math.cos(a), math.sin(a)
    return [[1, 0, 0], [0, c, -s], [0, s, c]]


def rot_y(a):
    c, s = math.cos(a), math.sin(a)
    return [[c, 0, s], [0, 1, 0], [-s, 0, c]]


def rot_z(a):
    c, s = math.cos(a), math.sin(a)
    return [[c, -s, 0], [s, c, 0], [0, 0, 1]]


def from_axes(ax, ay, az):
    """Rotation matrix whose columns are the given world-space axes."""
    return [[ax[0], ay[0], az[0]],
            [ax[1], ay[1], az[1]],
            [ax[2], ay[2], az[2]]]


def orthonormalize(m):
    """Gram-Schmidt on the columns -- engines hand back float32 axes."""
    cols = [[m[0][j], m[1][j], m[2][j]] for j in range(3)]

    def norm(v):
        l = math.sqrt(sum(c * c for c in v)) or 1.0
        return [c / l for c in v]

    def sub(a, b, k):
        return [a[i] - b[i] * k for i in range(3)]

    def dot(a, b):
        return sum(a[i] * b[i] for i in range(3))

    x = norm(cols[0])
    y = norm(sub(cols[1], x, dot(cols[1], x)))
    z = [x[1] * y[2] - x[2] * y[1],
         x[2] * y[0] - x[0] * y[2],
         x[0] * y[1] - x[1] * y[0]]
    if dot(z, cols[2]) < 0:      # keep the caller's handedness
        z = [-c for c in z]
    return from_axes(x, y, z)


# ---------------------------------------------------------------------------
# FO4 reference rotation convention
# ---------------------------------------------------------------------------
# REFR DATA stores three angles (radians on disk, degrees in xEdit/CK).
# Positive Z is a compass heading: 0 = facing north (+Y), 90 = east (+X),
# i.e. CLOCKWISE seen from above.  The world matrix is
#       M = Rz(-z) @ Ry(-y) @ Rx(-x)
# (Gamebryo's XYZ Euler build, transposed).  The Z part is certain from the
# heading behaviour; the X/Y order is the part the calibration scene checks.

def fo4_matrix_from_euler_deg(x, y, z):
    return mat_mul(rot_z(-math.radians(z)),
                   mat_mul(rot_y(-math.radians(y)), rot_x(-math.radians(x))))


def fo4_euler_deg_from_matrix(m):
    """Inverse of fo4_matrix_from_euler_deg.  Returns (x, y, z) in [0, 360)."""
    # m = Rz(c) Ry(b) Rx(a) with a=-x, b=-y, c=-z  (standard ZYX)
    sb = max(-1.0, min(1.0, -m[2][0]))
    b = math.asin(sb)
    if abs(sb) < 0.9999995:
        a = math.atan2(m[2][1], m[2][2])
        c = math.atan2(m[1][0], m[0][0])
    else:                         # gimbal lock: fold everything into c
        a = 0.0
        c = math.atan2(-m[0][1], m[1][1])

    def wrap(deg):
        deg = math.fmod(deg, 360.0)
        if deg < 0:
            deg += 360.0
        if deg >= 359.99995:      # avoid writing 360.0000
            deg = 0.0
        return deg

    return (wrap(-math.degrees(a)), wrap(-math.degrees(b)), wrap(-math.degrees(c)))


# ---------------------------------------------------------------------------
# Transform conversion
# ---------------------------------------------------------------------------

def profile_for(source, mesh_yaw_offset_deg=None):
    if source not in PROFILES:
        raise ValueError(f"unknown source engine {source!r}")
    p = dict(PROFILES[source])
    if mesh_yaw_offset_deg is not None:
        p["mesh_yaw_offset_deg"] = float(mesh_yaw_offset_deg)
    p["unit_scale"] = p["metres_per_unit"] / FO4_M_PER_UNIT
    return p


def mesh_basis(profile):
    """NIF-local = B @ source-local (before unit scale)."""
    return mat_mul(rot_z(math.radians(profile["mesh_yaw_offset_deg"])),
                   profile["world_axes"])


def convert_transform(profile, position, axes, scale3d,
                      world_offset=(0.0, 0.0, 0.0)):
    """
    position : source-world position, source units
    axes     : (local_x, local_y, local_z) world-space unit axes in the source
               engine (Unreal: forward, right, up / Unity: right, up, forward)
    scale3d  : source per-axis scale
    world_offset : FO4 units added after conversion

    Returns dict(pos=[x,y,z] FO4 units, rot=[x,y,z] FO4 degrees,
                 scale=float, warnings=[...]).
    """
    warnings = []
    W = profile["world_axes"]
    B = mesh_basis(profile)
    k = profile["unit_scale"]

    r_src = orthonormalize(from_axes(*axes))
    r_fo4 = mat_mul(mat_mul(W, r_src), transpose(B))   # W R B^-1

    if scale3d[0] * scale3d[1] * scale3d[2] < 0:
        warnings.append("mirrored (negative scale) -- FO4 refs cannot be "
                        "mirrored; bake the mirror into the mesh instead")
    sx, sy, sz = (abs(s) for s in scale3d)
    s = (sx + sy + sz) / 3.0
    if max(sx, sy, sz) - min(sx, sy, sz) > 1e-3 * max(s, 1e-6):
        warnings.append(f"non-uniform scale {tuple(round(v, 4) for v in scale3d)}"
                        f" -- FO4 refs are uniform only, using average {s:.4f}")
    if not (FO4_REF_SCALE_MIN <= s <= FO4_REF_SCALE_MAX):
        warnings.append(f"scale {s:.4f} outside FO4 range "
                        f"{FO4_REF_SCALE_MIN}-{FO4_REF_SCALE_MAX}")

    p = mat_vec(W, position)
    pos = [p[i] * k + world_offset[i] for i in range(3)]
    rot = list(fo4_euler_deg_from_matrix(r_fo4))
    return {"pos": pos, "rot": rot, "scale": s, "warnings": warnings}


def convert_local_bounds(profile, bmin, bmax):
    """Source-local AABB -> NIF-space AABB (FO4 units), for the STAT's OBND."""
    B = mesh_basis(profile)
    k = profile["unit_scale"]
    xs, ys, zs = [], [], []
    for cx in (bmin[0], bmax[0]):
        for cy in (bmin[1], bmax[1]):
            for cz in (bmin[2], bmax[2]):
                v = mat_vec(B, [cx, cy, cz])
                xs.append(v[0] * k)
                ys.append(v[1] * k)
                zs.append(v[2] * k)
    return [min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)]


def cell_of(x, y):
    return [int(math.floor(x / FO4_CELL_SIZE)), int(math.floor(y / FO4_CELL_SIZE))]


# ---------------------------------------------------------------------------
# Names and paths
# ---------------------------------------------------------------------------

_EDID_BAD = re.compile(r"[^A-Za-z0-9_]")


def sanitize_edid(text, max_len=80):
    s = _EDID_BAD.sub("_", text).strip("_")
    s = re.sub(r"_+", "_", s)
    return s[:max_len] or "Unnamed"


def ref_edid_for(prefix, label, actor_id):
    """
    Stable, unique REFR EditorID: prefix + readable label + 8-hex MD5 of the
    engine's full actor id.  The importer finds refs from earlier runs by
    this EDID, so it must not change between exports of the same actor.
    (Unity mirrors this in FO4LayoutMath.RefEdid -- keep them identical.)
    """
    h = hashlib.md5(actor_id.encode("utf-8")).hexdigest()[:8].upper()
    return f"{sanitize_edid(prefix, 16)}_{sanitize_edid(label, 40)}_{h}"


def normalize_nif_path(path):
    """Return a path relative to Meshes\\ with backslashes, or '' if empty."""
    if not path:
        return ""
    p = path.replace("/", "\\").strip().lstrip("\\")
    low = p.lower()
    for prefix in ("data\\meshes\\", "meshes\\"):
        if low.startswith(prefix):
            p = p[len(prefix):]
            break
    if not p.lower().endswith(".nif"):
        p += ".nif"
    return p


def default_nif_for_asset(asset_name, nif_root):
    """Mirror the Blender converters' naming: <sub-path>\\<mesh name>.nif."""
    name = asset_name.replace(" ", "_")
    root = (nif_root or "").replace("/", "\\").strip("\\")
    return normalize_nif_path(f"{root}\\{name}.nif" if root else f"{name}.nif")


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

TSV_COLUMNS = ["id", "ref_edid", "base", "nif", "stat_edid",
               "px", "py", "pz", "rx", "ry", "rz", "scale",
               "cell_x", "cell_y",
               "bminx", "bminy", "bminz", "bmaxx", "bmaxy", "bmaxz"]


def _f(v):
    return f"{v:.4f}"


def write_layout(out_dir, header, entries, basename="layout"):
    """
    Write <basename>.json (canonical, for tools/Mossy) and <basename>.tsv
    (what the xEdit script reads).  Returns (json_path, tsv_path).
    """
    os.makedirs(out_dir, exist_ok=True)
    doc = dict(header)
    doc["format"] = "fo4-layout"
    doc["format_version"] = FORMAT_VERSION
    doc["entries"] = entries
    jpath = os.path.join(out_dir, basename + ".json")
    tpath = os.path.join(out_dir, basename + ".tsv")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    with open(tpath, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"#format\tfo4-layout\t{FORMAT_VERSION}\n")
        for key in ("source", "worldspace", "prefix", "plugin"):
            f.write(f"#{key}\t{header.get(key, '')}\n")
        f.write("\t".join(TSV_COLUMNS) + "\n")
        for e in entries:
            b = e.get("bounds") or [0, 0, 0, 0, 0, 0]
            row = [e["id"], e["ref_edid"], e.get("base", ""), e.get("nif", ""),
                   e.get("stat_edid", ""),
                   *(_f(v) for v in e["pos"]), *(_f(v) for v in e["rot"]),
                   _f(e["scale"]), str(e["cell"][0]), str(e["cell"][1]),
                   *(str(int(round(v))) for v in b)]
            f.write("\t".join(str(c).replace("\t", " ") for c in row) + "\n")
    return jpath, tpath


def make_entry(profile, *, actor_id, actor_label, asset_name, asset_path,
               position, axes, scale3d, local_bounds=None, nif=None,
               base=None, nif_root="", prefix="ORSL", world_offset=(0, 0, 0)):
    """Build one layout entry from engine data.  See SPEC.md for fields."""
    t = convert_transform(profile, position, axes, scale3d, world_offset)
    nif_rel = normalize_nif_path(nif) if nif else (
        "" if base else default_nif_for_asset(asset_name, nif_root))
    entry = {
        "id": actor_id,
        "label": actor_label,
        "asset": asset_path,
        "ref_edid": ref_edid_for(prefix, actor_label, actor_id),
        "base": base or "",
        "nif": nif_rel,
        # whole relative path (not just the file name) so two meshes with the
        # same name in different folders never share a STAT
        "stat_edid": "" if base else sanitize_edid(
            f"{prefix}_{nif_rel[:-4] if nif_rel.lower().endswith('.nif') else nif_rel}"),
        "pos": t["pos"],
        "rot": t["rot"],
        "scale": t["scale"],
        "cell": cell_of(t["pos"][0], t["pos"][1]),
        "bounds": (convert_local_bounds(profile, *local_bounds)
                   if local_bounds else None),
        "warnings": t["warnings"],
    }
    return entry


def check_nifs(entries, mod_folder):
    """Flag entries whose NIF is not in <mod_folder>/Meshes.  Returns count."""
    missing = 0
    if not mod_folder:
        return 0
    for e in entries:
        if not e.get("nif"):
            continue
        p = os.path.join(mod_folder, "Meshes", *e["nif"].split("\\"))
        if not os.path.isfile(p):
            e["warnings"].append(f"NIF not found in mod folder: Meshes\\{e['nif']}")
            missing += 1
    return missing
