"""
Smoke test for unreal/fo4_layout_export.py against a minimal fake `unreal`
module -- catches typos and wiring mistakes outside the editor.  It cannot
prove the real UE API behaves the same; the calibration scene does that.
"""
import json
import math
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


class Vector:
    def __init__(self, x=0, y=0, z=0):
        self.x, self.y, self.z = x, y, z


class Rotator:
    def __init__(self, roll=0, pitch=0, yaw=0):
        self.roll, self.pitch, self.yaw = roll, pitch, yaw


class Quat:
    def __init__(self, rot):
        self._r = rot

    def rotator(self):
        return self._r


class Transform:
    def __init__(self, t, r, s):
        self.translation, self.rotation, self.scale3d = t, Quat(r), s


def _fwd(r):   # UE: yaw about Z, positive turns X toward Y
    y = math.radians(r.yaw)
    return Vector(math.cos(y), math.sin(y), 0)


def _right(r):
    y = math.radians(r.yaw)
    return Vector(-math.sin(y), math.cos(y), 0)


def _up(r):
    return Vector(0, 0, 1)


class Box:
    def __init__(self):
        self.min, self.max = Vector(-50, -50, 0), Vector(50, 50, 100)


class Mesh:
    def __init__(self, name):
        self.n = name

    def get_path_name(self):
        return f"/Game/Props/{self.n}.{self.n}"

    def get_name(self):
        return self.n

    def get_bounding_box(self):
        return Box()


class StaticMeshComponent:
    def __init__(self, mesh, t, tags=()):
        self.mesh, self.t, self.tags = mesh, t, list(tags)

    def get_editor_property(self, k):
        return {"static_mesh": self.mesh, "tags": self.tags}[k]

    def get_world_transform(self):
        return self.t

    def get_name(self):
        return "StaticMeshComponent0"


class InstancedStaticMeshComponent(StaticMeshComponent):
    def __init__(self, mesh, ts):
        super().__init__(mesh, None)
        self.ts = ts

    def get_instance_count(self):
        return len(self.ts)

    def get_instance_transform(self, i, world):
        return (True, self.ts[i])


class Actor:
    def __init__(self, label, comps, tags=()):
        self.label, self.comps, self.tags = label, comps, list(tags)

    def get_editor_property(self, k):
        return self.tags

    def get_components_by_class(self, cls):
        return [c for c in self.comps if isinstance(c, cls)]

    def get_actor_label(self):
        return self.label

    def get_path_name(self):
        return f"/Game/Maps/Town.Town:PersistentLevel.{self.label}"


def make_fake(project_dir, actors):
    u = types.ModuleType("unreal")
    u.Vector, u.Rotator, u.Transform = Vector, Rotator, Transform
    u.StaticMeshComponent = StaticMeshComponent
    u.InstancedStaticMeshComponent = InstancedStaticMeshComponent
    u.log = u.log_warning = lambda m: print(m)
    u.MathLibrary = types.SimpleNamespace(get_forward_vector=_fwd,
                                          get_right_vector=_right, get_up_vector=_up)
    u.Paths = types.SimpleNamespace(project_dir=lambda: project_dir,
                                    get_project_file_path=lambda: project_dir + "/Town.uproject")

    class Subsys:
        def get_selected_level_actors(self):
            return actors

        def get_all_level_actors(self):
            return actors

    class World:
        def get_path_name(self):
            return "/Game/Maps/Town"

    class EdSub:
        def get_editor_world(self):
            return World()

    u.EditorActorSubsystem, u.UnrealEditorSubsystem = Subsys, EdSub
    u.get_editor_subsystem = lambda cls: cls()
    return u


def test_export_smoke():
    proj = tempfile.mkdtemp()
    crate = Mesh("SM_Crate")
    T = lambda x, y, yaw, s=1: Transform(Vector(x, y, 0), Rotator(yaw=yaw), Vector(s, s, s))
    actors = [
        Actor("Crate1", [StaticMeshComponent(crate, T(0, 0, 0))]),
        Actor("Crate2", [StaticMeshComponent(crate, T(10000, 0, 90))]),
        Actor("Hidden", [StaticMeshComponent(crate, T(0, 0, 0))], tags=["fo4:skip"]),
        Actor("Vanilla", [StaticMeshComponent(crate, T(0, 0, 0), tags=["fo4:base=Fallout4.esm|0001A2B3"])]),
        Actor("Fence", [InstancedStaticMeshComponent(Mesh("SM_Fence"), [T(0, i * 200, 0) for i in range(3)])]),
    ]
    sys.modules["unreal"] = make_fake(proj, actors)
    sys.path.insert(0, os.path.join(ROOT, "unreal"))
    import fo4_layout_export as ex
    ex.load_config()                      # first run writes the default config
    tsv = ex.export()
    doc = json.load(open(os.path.join(proj, "FO4Layout", "layout.json")))
    es = doc["entries"]
    assert len(es) == 6, len(es)          # 2 crates + vanilla + 3 fence instances
    by = {e["label"]: e for e in es}
    assert by["Vanilla"]["base"] == "Fallout4.esm|0001A2B3" and by["Vanilla"]["nif"] == ""
    assert by["Crate1"]["nif"] == "ORS\\Unreal\\SM_Crate.nif"
    assert by["Crate2"]["cell"] == [0, 1]                # 10000 cm north = 7000 units
    assert len({e["ref_edid"] for e in es}) == 6         # all unique
    assert os.path.isfile(tsv)
    print("unreal exporter smoke test ok")


if __name__ == "__main__":
    test_export_smoke()
