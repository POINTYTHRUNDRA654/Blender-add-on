"""
fo4_batch_tools.py
==================
Batch export, progress reporting, and preset save/load for the Mossy FO4 addon.
"""
import bpy, os, json, time
from pathlib import Path

try:
    from . import export_helpers as _eh
except ImportError:
    _eh = None


# ═══════════════════════════════════════════════════════
# PROGRESS SYSTEM
# ═══════════════════════════════════════════════════════

_progress_state = {
    "active": False, "label": "", "current": 0, "total": 0, "pct": 0.0,
}

def progress_start(label: str, total: int):
    _progress_state.update(active=True, label=label, current=0, total=total, pct=0.0)
    bpy.context.window_manager.progress_begin(0, 100)
    print(f"[Progress] {label} — {total} steps")

def progress_step(step_label: str = "", current: int = None):
    s = _progress_state
    if not s["active"]: return
    if current is not None:
        s["current"] = current
    else:
        s["current"] += 1
    s["pct"] = (s["current"] / max(s["total"], 1)) * 100
    bpy.context.window_manager.progress_update(s["pct"])
    lbl = step_label or s["label"]
    print(f"[Progress] {lbl} {s['current']}/{s['total']} ({s['pct']:.0f}%)")

def progress_end():
    _progress_state["active"] = False
    bpy.context.window_manager.progress_end()
    print("[Progress] Done")


# ═══════════════════════════════════════════════════════
# PRESET SAVE / LOAD
# ═══════════════════════════════════════════════════════

_PRESET_PROPS = [
    "fo4_assets_path", "fo4_export_path", "fo4_plugin_name",
    "fo4_armor_description", "fo4_weapon_description",
    "fo4_npc_description", "fo4_glow_description",
    "fo4_tex_description", "fo4_tex_resolution",
    "fo4_glow_speed", "fo4_glow_strength",
]

def _preset_dir() -> Path:
    p = Path(bpy.utils.user_resource("CONFIG")) / "fo4_presets"
    p.mkdir(parents=True, exist_ok=True)
    return p

def save_preset(name: str, scene) -> tuple:
    data = {}
    for prop in _PRESET_PROPS:
        val = getattr(scene, prop, None)
        if val is not None:
            try: data[prop] = float(val) if isinstance(val, (int,float)) else str(val)
            except Exception: pass
    path = _preset_dir() / (name.replace(" ","_") + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"name": name, "props": data}, f, indent=2)
    return True, f"Preset saved: {path.name}"

def load_preset(name: str, scene) -> tuple:
    path = _preset_dir() / (name.replace(" ","_") + ".json")
    if not path.exists():
        return False, f"Preset not found: {name}"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    for prop, val in data.get("props", {}).items():
        try: setattr(scene, prop, val)
        except Exception: pass
    return True, f"Preset loaded: {name}"

def list_presets() -> list:
    return [p.stem.replace("_"," ") for p in _preset_dir().glob("*.json")]


# ═══════════════════════════════════════════════════════
# BATCH EXPORT
# ═══════════════════════════════════════════════════════

def _is_collision_proxy(obj) -> bool:
    """Return True if *obj* is a collision/physics proxy, not a real
    exportable shape.

    These are never meant to be exported as their own standalone NIF --
    PyNifly's exporter attaches them to their parent real mesh's export
    automatically (see ExportHelpers._find_collision_mesh). Handed to it
    directly instead, the export operator correctly finds nothing to write
    and reports "No objects selected for export", which used to show up as
    a plain batch-export failure with no explanation. Recognizes:
    - The same markers ExportHelpers._is_collision_mesh checks (the
      ``fo4_collision`` custom property, ``UCX_`` prefix, ``_COLLISION``
      suffix)
    - ``pynRigidBody`` -- PyNifly's own import-time marker for a native
      Havok collision shape (e.g. objects named/derived from
      ``bhkPhysicsSystem``)
    """
    if obj.get("fo4_collision") or obj.get("pynRigidBody"):
        return True
    name_upper = obj.name.upper()
    return name_upper.startswith("UCX_") or name_upper.endswith("_COLLISION")


def batch_export_objects(objects: list, output_dir: str,
                          apply_fo4_prep: bool = True) -> dict:
    """Export objects as NIF file(s), recombining multi-shape sources.

    A single source NIF can decompose into several Blender objects on
    import (multiple BSTriShapes in one file -- e.g. a 3-piece dead shrub).
    Objects imported via Batch Import NIFs carry a ``fo4_source_nif`` tag
    recording which file they came from; objects sharing that tag are
    grouped and written back out as ONE combined NIF (via
    ExportHelpers.export_scene_as_single_nif), matching how they arrived.
    An object without the tag (e.g. imported some other way) gets its own
    singleton group, keyed by its own name -- unchanged from the old
    one-file-per-object behaviour.

    Returns {success_count, fail_count, exported: [paths], skipped: [names]}
    """
    if not output_dir:
        return {"success": 0, "fail": len(objects), "exported": [], "skipped": []}
    os.makedirs(output_dir, exist_ok=True)
    results = {"success": 0, "fail": 0, "exported": [], "skipped": []}

    real_objects = []
    for obj in objects:
        if _is_collision_proxy(obj):
            results["skipped"].append(obj.name)
            print(f"[Batch] Skipped {obj.name}: collision/physics proxy, "
                  f"exported automatically with its parent mesh")
        else:
            real_objects.append(obj)

    # Group by source file. Objects with no fo4_source_nif tag each get
    # their own singleton group.
    groups = {}
    group_order = []
    for obj in real_objects:
        key = obj.get("fo4_source_nif") or f"__singleton_{id(obj)}"
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(obj)

    progress_start(f"Batch export {len(groups)} file(s) from {len(real_objects)} object(s)", len(group_order))

    for key in group_order:
        group_objs = groups[key]
        label = key if not key.startswith("__singleton_") else group_objs[0].name
        progress_step(f"Exporting {label} ({len(group_objs)} shape(s))")
        safe = label.replace(" ", "_").replace(".", "_")
        nif_path = os.path.join(output_dir, safe + ".nif")

        # Isolate this group BEFORE any edit-mode prep step -- fo4_post_process
        # enters Edit Mode and runs mesh.remove_doubles()/normals_make_consistent(),
        # which under Blender's multi-object editing apply to EVERY currently
        # selected mesh, not just the ones in this group. Selecting only this
        # group first prevents it from also mutating whatever the user had
        # selected before Batch Export was invoked, or the previous group.
        bpy.ops.object.select_all(action="DESELECT")
        for obj in group_objs:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = group_objs[0]

        # FO4 prep, per shape in the group
        if apply_fo4_prep:
            try:
                from . import imageto3d_helpers as _ith
                if hasattr(_ith, "fo4_post_process"):
                    for obj in group_objs:
                        _ith.fo4_post_process(obj, name=safe)
            except Exception:
                pass

        exported = False
        if _eh:
            ok, msg = _eh.ExportHelpers.export_scene_as_single_nif(
                bpy.context.scene, nif_path, objects=group_objs)
            if ok:
                exported = True
                print(f"[Batch] ✓ {os.path.basename(nif_path)}: {msg}")
            else:
                print(f"[Batch] FAILED {label}: {msg}")
        else:
            print(f"[Batch] FAILED {label}: export_helpers unavailable — install PyNifly")

        # ── Auto-export BGSM + textures alongside the combined NIF ─────────────
        # export_mesh_to_nif() (the single-object export path) ships the
        # material's .bgsm and referenced texture files next to the NIF it
        # writes; export_scene_as_single_nif() -- used above so a
        # multi-shape source recombines into one file -- does NOT do this
        # on its own. Without it, a recombined export (e.g. every
        # multi-shape source in a batch) silently drops the BGSM/textures
        # that used to ship automatically with the old one-file-per-shape
        # export, which is how a batch export folder can end up with NIFs
        # that reference textures nobody actually copied there.
        if exported:
            try:
                from . import bgsm_helpers as _bh_batch
                _mat_dir = os.path.join(output_dir, "Materials")
                _tex_dir = os.path.join(output_dir, "Textures")
                for _o in group_objs:
                    if not _o.data.materials:
                        continue
                    for _ok, _m in _bh_batch.export_bgsm_for_object(
                            _o, _mat_dir, all_slots=False,
                            is_lod=bool(_o.get("fo4_lod_level"))):
                        if not _ok:
                            print(f"[Batch] BGSM warning ({_o.name}): {_m}")
                    for _ok, _m in _bh_batch.export_textures_for_object(
                            _o, _tex_dir, all_slots=False):
                        if not _ok:
                            print(f"[Batch] Texture warning ({_o.name}): {_m}")
            except Exception as _bexc:
                print(f"[Batch] BGSM/texture auto-export skipped for {label}: {_bexc}")

        if exported:
            results["success"] += 1
            results["exported"].append(nif_path)
        else:
            results["fail"] += 1

    progress_end()
    return results


# ═══════════════════════════════════════════════════════
# OPERATORS
# ═══════════════════════════════════════════════════════

class FO4_OT_BatchExport(bpy.types.Operator):
    """Export all selected meshes as individual NIFs with FO4 prep applied."""
    bl_idname  = "fo4.batch_export"
    bl_label   = "Batch Export Selected Objects"
    bl_options = {"REGISTER"}

    output_dir: bpy.props.StringProperty(
        name="Output Folder", subtype="DIR_PATH", default="",
    )
    apply_fo4_prep: bpy.props.BoolProperty(
        name="Apply FO4 Prep", default=True,
        description="Auto-triangulate, UV-unwrap, and apply scale before export",
    )

    def execute(self, context):
        objects = [o for o in context.selected_objects if o.type == "MESH"]
        if not objects:
            self.report({"ERROR"}, "No mesh objects selected")
            return {"CANCELLED"}
        out = bpy.path.abspath(self.output_dir) if self.output_dir else bpy.path.abspath("//exports/")
        res = batch_export_objects(objects, out, self.apply_fo4_prep)
        summary = f"Batch export: {res['success']} OK, {res['fail']} failed"
        if res["skipped"]:
            summary += f", {len(res['skipped'])} skipped (collision/physics proxies)"
        summary += f" → {out}"
        self.report({"INFO"} if not res["fail"] else {"WARNING"}, summary)
        return {"FINISHED"}


class FO4_OT_SaveWorkflowSettings(bpy.types.Operator):
    """Save the current panel settings (paths, descriptions, resolution,
    glow settings -- see _PRESET_PROPS) as a named workflow preset.

    Distinct from fo4.save_preset, which snapshots selected objects'
    transforms/materials instead of any panel field -- the "Save Current
    Settings" button previously called that operator by mistake, so
    saving/loading a workflow preset never touched panel settings at all.
    """
    bl_idname  = "fo4.save_workflow_settings"
    bl_label   = "Save Workflow Settings"
    bl_options = {"REGISTER"}

    preset_name: bpy.props.StringProperty(name="Preset Name", default="My Workflow")

    def execute(self, context):
        name = self.preset_name.strip()
        if not name:
            self.report({"ERROR"}, "Enter a preset name first")
            return {"CANCELLED"}
        ok, msg = save_preset(name, context.scene)
        self.report({"INFO"} if ok else {"ERROR"}, msg)
        return {"FINISHED"}


def _workflow_preset_items(self, context):
    names = list_presets()
    return [(n, n, "") for n in names] if names else [("", "(no saved presets)", "")]


class FO4_OT_LoadWorkflowSettings(bpy.types.Operator):
    """Load a previously saved workflow settings preset onto the scene."""
    bl_idname  = "fo4.load_workflow_settings"
    bl_label   = "Load Workflow Settings"
    bl_options = {"REGISTER"}

    preset_name: bpy.props.EnumProperty(name="Preset", items=_workflow_preset_items)

    def execute(self, context):
        if not self.preset_name:
            self.report({"ERROR"}, "No saved workflow presets found")
            return {"CANCELLED"}
        ok, msg = load_preset(self.preset_name, context.scene)
        self.report({"INFO"} if ok else {"ERROR"}, msg)
        return {"FINISHED"}


class FO4_OT_GenerateTextureFromDesc(bpy.types.Operator):
    """Shortcut: generate texture and apply to active object material."""
    bl_idname  = "fo4.generate_and_apply_texture"
    bl_label   = "Generate + Apply Texture"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            bpy.ops.fo4.generate_texture()
        except AttributeError:
            self.report({'ERROR'}, "fo4.generate_texture operator not available")
            return {'CANCELLED'}
        return {"FINISHED"}


_CLASSES = [
    FO4_OT_BatchExport,
    FO4_OT_SaveWorkflowSettings,
    FO4_OT_LoadWorkflowSettings,
    FO4_OT_GenerateTextureFromDesc,
]

_SCENE_PROPS = [
    ("fo4_batch_output", bpy.props.StringProperty(
        name="Batch Output Folder", subtype="DIR_PATH", default="",
    )),
    ("fo4_batch_fo4_prep", bpy.props.BoolProperty(
        name="Apply FO4 Prep on Batch Export", default=True,
    )),
    ("fo4_workflow_preset_name", bpy.props.StringProperty(
        name="Workflow Preset Name", default="My Workflow",
    )),
]


def register():
    for cls in _CLASSES:
        try: bpy.utils.register_class(cls)
        except Exception: pass
    for name, prop in _SCENE_PROPS:
        try: setattr(bpy.types.Scene, name, prop)
        except Exception: pass


def unregister():
    for name, _ in reversed(_SCENE_PROPS):
        try: delattr(bpy.types.Scene, name)
        except Exception: pass
    for cls in reversed(_CLASSES):
        try: bpy.utils.unregister_class(cls)
        except Exception: pass
