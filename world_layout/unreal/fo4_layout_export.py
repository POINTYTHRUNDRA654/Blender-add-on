"""
fo4_layout_export.py -- Unreal Editor (UE 5.x) -> Fallout 4 layout exporter.

Run it from the Unreal Editor's Output Log (Cmd dropdown set to "Python"):

    py "D:/Projects/blender-addon-repo/world_layout/unreal/fo4_layout_export.py"
    py "D:/.../fo4_layout_export.py" --all          # whole level, not selection
    py "D:/.../fo4_layout_export.py" --calibrate    # spawn the calibration scene

Requires the "Python Editor Script Plugin" (Edit > Plugins), on by default
in UE5.

First run writes  <YourProject>/FO4Layout/fo4_layout_config.json  with
defaults -- edit it (worldspace, mod folder, NIF sub-path) and run again.
Output: <YourProject>/FO4Layout/layout.json + layout.tsv.  Feed the .tsv to
"FO4 - Import Layout.pas" in xEdit.

Per-actor overrides (Actor > Details > Actor > Tags, or component tags):
    fo4:skip                         -- leave this actor out
    fo4:nif=ORS\\Props\\Crate01.nif   -- NIF to use (relative to Meshes\\)
    fo4:base=Fallout4.esm|0001A2B3   -- place an existing base object
    fo4:base=EditorID                -- same, looked up by EditorID
"""

import json
import os
import sys

import unreal

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))           # world_layout/
import layout_core as lc                              # noqa: E402

DEFAULT_CONFIG = {
    "worldspace": "OperationResoluteSentinel",
    "plugin": "ORS_Layout.esp",
    "prefix": "ORSL",
    # where the Blender UE converter put the NIFs ("Mesh Sub-path" there)
    "nif_root": "ORS\\Unreal",
    # MO2 mod folder holding Meshes\ -- used to warn about missing NIFs
    "mod_folder": "",
    # FO4 units added to every position (move the whole layout in the world)
    "world_offset": [0.0, 0.0, 0.0],
    # null = profile default; set after the calibration check if needed
    "mesh_yaw_offset_deg": None,
    # explicit asset -> NIF mapping, e.g. {"/Game/Props/SM_Crate": "ORS\\Props\\Crate.nif"}
    "nif_map": {},
    # explicit asset -> existing base object, e.g. {"/Game/Proxy/Barrel": "Fallout4.esm|0001A2B3"}
    "base_map": {},
}


def _log(msg):
    unreal.log(f"[FO4 Layout] {msg}")


def _warn(msg):
    unreal.log_warning(f"[FO4 Layout] {msg}")


def _layout_dir():
    return os.path.join(os.path.abspath(unreal.Paths.project_dir()), "FO4Layout")


def load_config():
    path = os.path.join(_layout_dir(), "fo4_layout_config.json")
    if not os.path.isfile(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        _warn(f"Created default config at {path} -- check it, then run again.")
    with open(path, encoding="utf-8") as f:
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(json.load(f))
    return cfg, path


def _tags(obj):
    try:
        return [str(t) for t in (obj.get_editor_property("tags") or [])]
    except Exception:
        return []


def _tag_value(tags, key):
    for t in tags:
        if t.lower().startswith(key + "="):
            return t.split("=", 1)[1].strip()
    return None


def _axes(transform):
    rot = transform.rotation.rotator()
    f = unreal.MathLibrary.get_forward_vector(rot)   # local +X
    r = unreal.MathLibrary.get_right_vector(rot)     # local +Y
    u = unreal.MathLibrary.get_up_vector(rot)        # local +Z
    return ([f.x, f.y, f.z], [r.x, r.y, r.z], [u.x, u.y, u.z])


def _instance_transform(comp, i):
    res = comp.get_instance_transform(i, True)
    if isinstance(res, unreal.Transform):
        return res
    if isinstance(res, tuple):                       # (ok, transform) on some builds
        for v in res:
            if isinstance(v, unreal.Transform):
                return v
    return None


def _asset_key(mesh):
    # "/Game/Props/SM_Crate.SM_Crate" -> "/Game/Props/SM_Crate"
    return mesh.get_path_name().split(".")[0]


def collect(cfg, profile, actors):
    entries, skipped = [], 0
    offset = cfg.get("world_offset") or [0, 0, 0]
    for actor in actors:
        a_tags = _tags(actor)
        if "fo4:skip" in (t.lower() for t in a_tags):
            skipped += 1
            continue
        comps = actor.get_components_by_class(unreal.StaticMeshComponent) or []
        for comp in comps:
            mesh = comp.get_editor_property("static_mesh")
            if mesh is None:
                continue
            c_tags = _tags(comp) + a_tags
            if "fo4:skip" in (t.lower() for t in c_tags):
                skipped += 1
                continue
            key = _asset_key(mesh)
            nif = _tag_value(c_tags, "fo4:nif") or cfg["nif_map"].get(key)
            base = _tag_value(c_tags, "fo4:base") or cfg["base_map"].get(key)
            bb = mesh.get_bounding_box()
            bounds = ([bb.min.x, bb.min.y, bb.min.z], [bb.max.x, bb.max.y, bb.max.z])
            label = actor.get_actor_label()
            base_id = actor.get_path_name()
            if len(comps) > 1:
                base_id += "." + comp.get_name()

            if isinstance(comp, unreal.InstancedStaticMeshComponent):
                transforms = [(f"{base_id}#{i}", f"{label}_{i}", _instance_transform(comp, i))
                              for i in range(comp.get_instance_count())]
            else:
                transforms = [(base_id, label, comp.get_world_transform())]

            for actor_id, lab, t in transforms:
                if t is None:
                    _warn(f"{lab}: could not read instance transform, skipped")
                    continue
                tr = t.translation
                sc = t.scale3d
                e = lc.make_entry(
                    profile, actor_id=actor_id, actor_label=lab,
                    asset_name=mesh.get_name(), asset_path=key,
                    position=[tr.x, tr.y, tr.z], axes=_axes(t),
                    scale3d=(sc.x, sc.y, sc.z), local_bounds=bounds,
                    nif=nif, base=base, nif_root=cfg["nif_root"],
                    prefix=cfg["prefix"], world_offset=offset)
                entries.append(e)
    return entries, skipped


def _level_name():
    try:
        world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        return world.get_path_name() if world else ""
    except Exception:
        return ""


def export(all_actors=False):
    cfg, cfg_path = load_config()
    profile = lc.profile_for("unreal", cfg.get("mesh_yaw_offset_deg"))
    subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actors = subsys.get_all_level_actors() if all_actors else subsys.get_selected_level_actors()
    if not actors:
        _warn("Nothing selected. Select actors, or run with --all.")
        return None

    entries, skipped = collect(cfg, profile, actors)
    if not entries:
        _warn("No static meshes found in the chosen actors.")
        return None

    missing = lc.check_nifs(entries, cfg.get("mod_folder"))
    header = {
        "source": "unreal",
        "source_project": unreal.Paths.get_project_file_path(),
        "level": _level_name(),
        "worldspace": cfg["worldspace"],
        "plugin": cfg["plugin"],
        "prefix": cfg["prefix"],
        "mesh_yaw_offset_deg": profile["mesh_yaw_offset_deg"],
        "world_offset": cfg.get("world_offset"),
    }
    jpath, tpath = lc.write_layout(_layout_dir(), header, entries)

    cells = sorted({tuple(e["cell"]) for e in entries})
    n_warn = sum(1 for e in entries if e["warnings"])
    for e in entries:
        for w in e["warnings"]:
            _warn(f"{e['label']}: {w}")
    _log(f"Exported {len(entries)} placement(s) across {len(cells)} cell(s) "
         f"({skipped} skipped by tag) -> {tpath}")
    if missing:
        _warn(f"{missing} placement(s) point at NIFs that are not in the mod folder yet "
              "-- convert those assets with the Blender add-on first.")
    if n_warn:
        _warn(f"{n_warn} placement(s) have warnings (see above).")
    return tpath


# ---------------------------------------------------------------------------
# Calibration scene: a few copies of one asymmetric mesh at known rotations.
# Export it, import into a test plugin, and compare in the CK (see SPEC.md).
# ---------------------------------------------------------------------------

CALIBRATION = [
    # label,           location (cm),       rotator (pitch, yaw, roll)
    ("Cal_Identity",   (0, 0, 0),           (0, 0, 0)),
    ("Cal_Yaw90",      (400, 0, 0),         (0, 90, 0)),
    ("Cal_Pitch30",    (800, 0, 0),         (30, 0, 0)),
    ("Cal_Roll45",     (1200, 0, 0),        (0, 0, 45)),
    ("Cal_Mixed",      (1600, 0, 0),        (20, 135, -35)),
    ("Cal_North400",   (0, 400, 0),         (0, 0, 0)),
]


def spawn_calibration(mesh_path="/Engine/BasicShapes/Cone.Cone"):
    """
    Spawns the calibration actors.  Use one of YOUR converted assets that
    is clearly asymmetric (a chair, a sign) -- pass its path, e.g.
    spawn_calibration("/Game/Props/SM_Chair.SM_Chair").
    """
    mesh = unreal.EditorAssetLibrary.load_asset(mesh_path)
    if mesh is None:
        _warn(f"Could not load {mesh_path}")
        return
    subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    spawned = []
    for label, loc, (p, y, r) in CALIBRATION:
        a = subsys.spawn_actor_from_class(unreal.StaticMeshActor,
                                          unreal.Vector(*loc),
                                          unreal.Rotator(roll=r, pitch=p, yaw=y))
        a.set_actor_label(label)
        a.static_mesh_component.set_editor_property("static_mesh", mesh)
        spawned.append(a)
    subsys.set_selected_level_actors(spawned)
    _log(f"Spawned {len(spawned)} calibration actors (selected). "
         "Run the exporter now to write them out.")


if __name__ == "__main__":
    args = [a.lower() for a in sys.argv[1:]]
    if "--calibrate" in args:
        rest = [a for a in sys.argv[1:] if not a.startswith("--")]
        spawn_calibration(*rest[:1])
    else:
        export(all_actors="--all" in args)
