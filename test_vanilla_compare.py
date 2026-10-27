"""Standalone test (no Blender): the checks run against simple stand-ins for Blender's mesh objects."""
import os, sys, types
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import vanilla_compare as vc

class G:  # vertex group membership
    def __init__(s, group, weight): s.group, s.weight = group, weight
class V:
    def __init__(s, groups=()): s.groups = list(groups)
class P:
    def __init__(s, verts): s.vertices = verts
class Mesh:
    def __init__(s, verts, polys, uv=1): s.vertices, s.polygons, s.uv_layers = verts, polys, [0] * uv
class VG:
    def __init__(s, name): s.name = name
class Obj:
    def __init__(s, name="Obj", scale=(1, 1, 1), groups=("Bone1", "Bone2", "Bone3", "Bone4", "Bone5")):
        s.name, s.scale, s.vertex_groups = name, scale, [VG(g) for g in groups]
class Arm:
    def __init__(s, names): s.data = types.SimpleNamespace(bones=[types.SimpleNamespace(name=n) for n in names])

def codes(checks): return {c["code"] for c in checks}

def test_clean_mesh_has_no_findings():
    m = Mesh([V(), V(), V(), V()], [P([0, 1, 2]), P([0, 2, 3])])
    assert vc.rule_checks(Obj(), m, None) == []

def test_ngon_scale_uv_loose():
    m = Mesh([V() for _ in range(7)], [P([0, 1, 2, 3, 4])], uv=0)
    c = codes(vc.rule_checks(Obj(scale=(2, 1, 1)), m, None))
    assert c == {"ngons", "unapplied_scale", "no_uv", "loose_vertices"}

def test_negative_scale_flagged():
    m = Mesh([V()] * 3, [P([0, 1, 2])])
    assert "negative_scale" in codes(vc.rule_checks(Obj(scale=(-1, 1, 1)), m, None))

def test_influences_over_four_and_unweighted():
    arm = Arm(["Bone1", "Bone2", "Bone3", "Bone4", "Bone5"])
    five = V([G(i, 0.2) for i in range(5)])
    none = V([])
    ok = V([G(0, 1.0)])
    m = Mesh([five, none, ok], [P([0, 1, 2])])
    checks = {c["code"]: c["count"] for c in vc.rule_checks(Obj(), m, arm)}
    assert checks == {"too_many_influences": 1, "unweighted_vertices": 1}

def test_zero_weight_and_non_bone_groups_do_not_count():
    arm = Arm(["Bone1"])
    v = V([G(0, 1.0), G(1, 0.5), G(2, 0.0)])     # only Bone1 is a real bone; group 2 has weight 0
    m = Mesh([v], [P([0, 0, 0])])
    assert vc.rule_checks(Obj(), m, arm) == []
