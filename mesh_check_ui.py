"""
Mossy mesh check: a button and results panel for the rule checks in vanilla_compare.py.

Plain, verifiable checks only (n-gons, scale, UV, loose vertices, bone influences).
Nothing here talks to the network or to Mossy; the "most similar vanilla mesh"
comparison happens on Mossy's side over the bridge. Two helpers are offered:
select the problem geometry so you can see it, and apply scale. Nothing else is
changed automatically.
"""
from __future__ import annotations

import json

import bpy
from bpy.props import StringProperty
from bpy.types import Operator, Panel

from . import vanilla_compare

_SEVERITY_ICON = {"error": "ERROR", "warning": "QUESTION", "info": "INFO"}


def _run_checks(context):
    meshes = vanilla_compare.selected_meshes(bpy)
    results = []
    for obj in meshes:
        armature = vanilla_compare._armature_of(obj)
        results.extend(vanilla_compare.rule_checks(obj, obj.data, armature))
    return meshes, results


class FO4_OT_mossy_mesh_check(Operator):
    bl_idname = "fo4.mossy_mesh_check"
    bl_label = "Check Mesh for Fallout 4"
    bl_description = "Run Mossy's plain checks (n-gons, scale, UVs, loose vertices, bone influences) on the selected mesh(es)"
    bl_options = {"REGISTER"}

    def execute(self, context):
        meshes, results = _run_checks(context)
        if not meshes:
            self.report({"ERROR"}, "Select a mesh object first.")
            return {"CANCELLED"}
        context.window_manager.mossy_mesh_check_json = json.dumps(
            {"objects": [o.name for o in meshes], "results": results}
        )
        errors = sum(1 for r in results if r["severity"] == "error")
        warns = len(results) - errors
        if not results:
            self.report({"INFO"}, f"{len(meshes)} mesh(es) checked: no problems found.")
        else:
            self.report({"WARNING"}, f"{errors} error(s), {warns} warning(s) found. See the Mesh Check panel.")
        return {"FINISHED"}


class FO4_OT_mossy_mesh_fix(Operator):
    bl_idname = "fo4.mossy_mesh_fix"
    bl_label = "Mesh Check Helper"
    bl_description = "Select the problem geometry, or apply scale"
    bl_options = {"REGISTER", "UNDO"}

    action: StringProperty(default="")

    def execute(self, context):
        meshes = vanilla_compare.selected_meshes(bpy)
        if not meshes:
            self.report({"ERROR"}, "Select a mesh object first.")
            return {"CANCELLED"}
        if self.action == "apply_scale":
            try:
                bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
            except RuntimeError as exc:
                self.report({"ERROR"}, f"Could not apply scale: {exc}")
                return {"CANCELLED"}
            self.report({"INFO"}, "Scale applied.")
            return {"FINISHED"}
        if self.action in ("select_ngons", "select_loose"):
            import bmesh

            obj = meshes[0]
            context.view_layer.objects.active = obj
            if context.mode != "EDIT_MESH":
                bpy.ops.object.mode_set(mode="EDIT")
            bm = bmesh.from_edit_mesh(obj.data)
            for f in bm.faces:
                f.select_set(False)
            for e in bm.edges:
                e.select_set(False)
            for v in bm.verts:
                v.select_set(False)
            count = 0
            if self.action == "select_ngons":
                bpy.ops.mesh.select_mode(type="FACE")
                for f in bm.faces:
                    if len(f.verts) > 4:
                        f.select_set(True)
                        count += 1
            else:
                bpy.ops.mesh.select_mode(type="VERT")
                for v in bm.verts:
                    if not v.link_faces:
                        v.select_set(True)
                        count += 1
            bm.select_flush_mode()
            bmesh.update_edit_mesh(obj.data)
            self.report({"INFO"}, f"Selected {count} item(s) on '{obj.name}'.")
            return {"FINISHED"}
        return {"CANCELLED"}


class FO4_PT_mossy_mesh_check(Panel):
    bl_label = "Mesh Check (Mossy)"
    bl_idname = "FO4_PT_mossy_mesh_check"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Fallout 4"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        layout.operator("fo4.mossy_mesh_check", icon="CHECKMARK")
        raw = getattr(context.window_manager, "mossy_mesh_check_json", "")
        if not raw:
            layout.label(text="Select a mesh, then press the button.")
            return
        try:
            data = json.loads(raw)
        except ValueError:
            return
        results = data.get("results", [])
        layout.label(text="Checked: " + ", ".join(data.get("objects", []))[:60])
        if not results:
            layout.label(text="No problems found.", icon="CHECKMARK")
            return
        for r in results:
            box = layout.box()
            col = box.column(align=True)
            col.label(text=f"{r['code'].replace('_', ' ').title()} ({r['count']})", icon=_SEVERITY_ICON.get(r["severity"], "INFO"))
            # Wrap the message into short lines so it fits the sidebar.
            words, line = r["message"].split(), ""
            for w in words:
                if len(line) + len(w) + 1 > 42:
                    col.label(text=line)
                    line = w
                else:
                    line = (line + " " + w).strip()
            if line:
                col.label(text=line)
            row = box.row(align=True)
            if r["code"] == "ngons":
                op = row.operator("fo4.mossy_mesh_fix", text="Select n-gons")
                op.action = "select_ngons"
            elif r["code"] == "loose_vertices":
                op = row.operator("fo4.mossy_mesh_fix", text="Select loose vertices")
                op.action = "select_loose"
            elif r["code"] in ("unapplied_scale", "negative_scale"):
                op = row.operator("fo4.mossy_mesh_fix", text="Apply scale")
                op.action = "apply_scale"


_classes = (FO4_OT_mossy_mesh_check, FO4_OT_mossy_mesh_fix, FO4_PT_mossy_mesh_check)


def register():
    bpy.types.WindowManager.mossy_mesh_check_json = StringProperty(default="")
    for cls in _classes:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            pass  # already registered (add-on reload)


def unregister():
    for cls in reversed(_classes):
        try:
            bpy.utils.unregister_class(cls)
        except (RuntimeError, ValueError):
            pass
    try:
        del bpy.types.WindowManager.mossy_mesh_check_json
    except AttributeError:
        pass
