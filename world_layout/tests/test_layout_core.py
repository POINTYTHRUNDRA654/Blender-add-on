"""Run:  python -m pytest world_layout/tests   (or python test_layout_core.py)"""
import json
import math
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import layout_core as lc  # noqa: E402

EPS = 1e-6


def close(a, b, eps=1e-4):
    return all(abs(x - y) < eps for x, y in zip(a, b))


def rand_rot(rng):
    """Random proper rotation as (axis_x, axis_y, axis_z) columns."""
    m = lc.mat_mul(lc.rot_z(rng.uniform(-3, 3)),
                   lc.mat_mul(lc.rot_y(rng.uniform(-1.4, 1.4)),
                              lc.rot_x(rng.uniform(-3, 3))))
    return [[m[0][j], m[1][j], m[2][j]] for j in range(3)], m


def test_axes_are_handedness_flips():
    for name, p in lc.PROFILES.items():
        assert abs(lc.det(p["world_axes"]) + 1) < EPS, name
        assert abs(lc.det(lc.mesh_basis(lc.profile_for(name))) + 1) < EPS


def test_fo4_heading_is_clockwise():
    m = lc.fo4_matrix_from_euler_deg(0, 0, 90)
    # facing north (+Y) turned to heading 90 must face east (+X)
    assert close(lc.mat_vec(m, [0, 1, 0]), [1, 0, 0])


def test_fo4_euler_roundtrip():
    rng = random.Random(1)
    for _ in range(500):
        x, y, z = rng.uniform(0, 360), rng.uniform(-85, 85) % 360, rng.uniform(0, 360)
        m = lc.fo4_matrix_from_euler_deg(x, y, z)
        x2, y2, z2 = lc.fo4_euler_deg_from_matrix(m)
        m2 = lc.fo4_matrix_from_euler_deg(x2, y2, z2)
        assert all(close(m[i], m2[i], 1e-9) for i in range(3))
        for a in (x2, y2, z2):
            assert 0 <= a < 360


def test_gimbal_lock_still_reproduces_matrix():
    for x, z in ((30, 40), (0, 0), (170, 300)):
        for y in (90, 270):
            m = lc.fo4_matrix_from_euler_deg(x, y, z)
            m2 = lc.fo4_matrix_from_euler_deg(*lc.fo4_euler_deg_from_matrix(m))
            assert all(close(m[i], m2[i], 1e-6) for i in range(3))


def test_end_to_end_vertex_lands_in_same_place():
    """
    The real contract: a vertex of the mesh, placed by the engine actor,
    must land at the same world point as that vertex of the converted NIF
    placed by the FO4 ref we generate (using FO4's own Euler convention).
    """
    rng = random.Random(7)
    for source in ("unreal", "unity"):
        p = lc.profile_for(source)
        W, B, k = p["world_axes"], lc.mesh_basis(p), p["unit_scale"]
        for _ in range(300):
            axes, r = rand_rot(rng)
            s = rng.uniform(0.2, 3)
            pos = [rng.uniform(-5e4, 5e4) for _ in range(3)]
            off = [rng.uniform(-1e4, 1e4) for _ in range(3)]
            t = lc.convert_transform(p, pos, axes, (s, s, s), off)
            v = [rng.uniform(-100, 100) for _ in range(3)]
            # engine side, then into FO4 world
            ew = lc.mat_vec(r, [c * s for c in v])
            expect = [c * k for c in lc.mat_vec(W, [ew[i] + pos[i] for i in range(3)])]
            expect = [expect[i] + off[i] for i in range(3)]
            # FO4 side: NIF vertex = B v k, placed by ref
            nv = [c * k for c in lc.mat_vec(B, v)]
            m = lc.fo4_matrix_from_euler_deg(*t["rot"])
            got = [c * t["scale"] for c in lc.mat_vec(m, nv)]
            got = [got[i] + t["pos"][i] for i in range(3)]
            assert close(got, expect, 1e-3), (source, got, expect)
            assert t["warnings"] == []


def test_unreal_basics():
    p = lc.profile_for("unreal")
    t = lc.convert_transform(p, [100, 0, 0], ([1, 0, 0], [0, 1, 0], [0, 0, 1]), (1, 1, 1))
    assert close(t["pos"], [0, 100 * 0.01 / lc.FO4_M_PER_UNIT, 0])   # UE +X -> north
    assert close(t["rot"], [0, 0, 180])                                # converter yaw
    t = lc.convert_transform(p, [0, 100, 0], ([1, 0, 0], [0, 1, 0], [0, 0, 1]), (1, 1, 1))
    assert t["pos"][0] > 69 and abs(t["pos"][1]) < EPS                  # UE +Y -> east


def test_yaw_override():
    p = lc.profile_for("unreal", mesh_yaw_offset_deg=0)
    t = lc.convert_transform(p, [0, 0, 0], ([1, 0, 0], [0, 1, 0], [0, 0, 1]), (1, 1, 1))
    assert close(t["rot"], [0, 0, 0])


def test_unity_scale_and_axes():
    p = lc.profile_for("unity")
    t = lc.convert_transform(p, [1, 2, 3], ([1, 0, 0], [0, 1, 0], [0, 0, 1]), (1, 1, 1))
    k = 1 / lc.FO4_M_PER_UNIT
    assert close(t["pos"], [1 * k, 3 * k, 2 * k])


def test_warnings():
    p = lc.profile_for("unreal")
    ident = ([1, 0, 0], [0, 1, 0], [0, 0, 1])
    assert any("non-uniform" in w for w in lc.convert_transform(p, [0] * 3, ident, (1, 2, 1))["warnings"])
    assert any("mirrored" in w for w in lc.convert_transform(p, [0] * 3, ident, (-1, 1, 1))["warnings"])
    assert any("outside" in w for w in lc.convert_transform(p, [0] * 3, ident, (20, 20, 20))["warnings"])


def test_cells():
    assert lc.cell_of(0, 0) == [0, 0]
    assert lc.cell_of(4095.9, -0.1) == [0, -1]
    assert lc.cell_of(-4096, 8192) == [-1, 2]


def test_paths_and_names():
    assert lc.normalize_nif_path("Data/Meshes/ORS/Props/Crate.nif") == "ORS\\Props\\Crate.nif"
    assert lc.normalize_nif_path("meshes\\a\\b") == "a\\b.nif"
    assert lc.default_nif_for_asset("SM Crate 01", "ORS/Unreal") == "ORS\\Unreal\\SM_Crate_01.nif"
    assert lc.sanitize_edid("ORSL_/Game/Maps/Town:PersistentLevel.SM_Crate_3") == \
        "ORSL_Game_Maps_Town_PersistentLevel_SM_Crate_3"


def test_bounds_follow_mesh_basis():
    p = lc.profile_for("unreal")
    # UE mesh 200cm long on X, 100 wide on Y, 50 tall
    b = lc.convert_local_bounds(p, [0, -50, 0], [200, 50, 50])
    k = p["unit_scale"]
    # UE local X -> NIF -Y (W swaps X/Y, then 180 yaw)
    assert close(b, [-50 * k, -200 * k, 0, 50 * k, 0, 50 * k])


def test_write_layout_roundtrip():
    p = lc.profile_for("unreal")
    e = lc.make_entry(p, actor_id="Town.SM_Crate_3", actor_label="Crate", asset_name="SM_Crate",
                      asset_path="/Game/Props/SM_Crate", position=[10, 20, 30],
                      axes=([1, 0, 0], [0, 1, 0], [0, 0, 1]), scale3d=(1, 1, 1),
                      local_bounds=([-10, -10, 0], [10, 10, 20]), nif_root="ORS\\Unreal")
    base = lc.make_entry(p, actor_id="Town.Barrel", actor_label="Barrel", asset_name="x",
                         asset_path="/Game/x", position=[0, 0, 0],
                         axes=([1, 0, 0], [0, 1, 0], [0, 0, 1]), scale3d=(1, 1, 1),
                         base="Fallout4.esm|0001A2B3")
    assert base["nif"] == "" and base["stat_edid"] == ""
    d = tempfile.mkdtemp()
    j, t = lc.write_layout(d, {"source": "unreal", "worldspace": "ORSWorld",
                               "prefix": "ORSL", "plugin": "ORS_Layout.esp"}, [e, base])
    doc = json.load(open(j))
    assert doc["format_version"] == lc.FORMAT_VERSION and len(doc["entries"]) == 2
    lines = open(t).read().splitlines()
    assert lines[0].startswith("#format\tfo4-layout")
    hdr = [l for l in lines if not l.startswith("#")][0].split("\t")
    assert hdr == lc.TSV_COLUMNS
    row = dict(zip(hdr, [l for l in lines if not l.startswith("#")][1].split("\t")))
    assert row["nif"] == "ORS\\Unreal\\SM_Crate.nif"
    assert row["stat_edid"] == "ORSL_ORS_Unreal_SM_Crate"
    assert len(row) == len(lc.TSV_COLUMNS)


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except AssertionError as ex:
                fails += 1
                print("FAIL", name, ex)
    sys.exit(1 if fails else 0)
