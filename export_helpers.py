"""
Export helper functions for Fallout 4 mod creation
"""

import bpy
import os
import json
import sys
import traceback

# FO4 game units per metre (Havok scale).  Must match operators._FO4_UNIT_SCALE_INV.
_FO4_UNIT_SCALE_INV = 69.99125


class ExportHelpers:
    """Helper functions for exporting to Fallout 4"""

    # Vertex group names used exclusively for FO4 procedural wind on vegetation.
    # Meshes whose groups are all in this set don't need an armature to export.
    _WIND_GROUP_NAMES = frozenset({
        'Wind', 'wind', 'WIND',
        'WindWeight', 'windweight', 'WINDWEIGHT',
        'WindStiff', 'windstiff',
    })

    # FO4 LOD vertex groups — used by the renderer to select faces per LOD
    # distance level.  No armature is needed; blocking export on these is wrong.
    _LOD_GROUP_NAMES = frozenset({
        'LOD0', 'LOD1', 'LOD2', 'LOD3', 'LOD4',
        'lod0', 'lod1', 'lod2', 'lod3', 'lod4',
    })

    # All vertex group names that are valid without an armature
    _ARMATURE_FREE_GROUP_NAMES = _WIND_GROUP_NAMES | _LOD_GROUP_NAMES

    @staticmethod
    def _is_partition_group(name):
        """True for FO4 dismemberment/segment partition groups (e.g.
        'FO4 Seg 000', or the legacy 'SBP_32' naming from older scenes).

        These mark every vertex of a shape/body-part with weight 1.0 for pyNIF
        to convert into a BSSubIndexTriShape/FO4Segment partition — they are
        not a skinning weight and must not count toward the 4-bone-influence
        limit.
        """
        upper = name.upper()
        return upper.startswith("FO4 SEG") or upper.startswith("SBP_")

    @staticmethod
    def _is_vegetation_object(obj):
        """Return True if *obj* is a vegetation/plant asset.

        Vegetation in FO4 uses procedural in-engine wind driven by the Wind
        vertex group — no armature skeleton is needed or wanted.  The check
        looks for four signals in priority order:
        1. ``fo4_mesh_type == 'VEGETATION'`` — the real, user-facing "Mesh
           Type" dropdown (a proper RNA EnumProperty; read via getattr, not
           .get(), since it isn't a custom ID property).
        2. ``fo4_object_type == 'VEGETATION'`` custom property on the object.
        3. ``fo4_shader_type == 'vegetation'`` or ``fo4_core_profile == 'foliage'``
           on any of the object's materials.
        4. All vertex groups are recognised wind-weight group names.

        NOTE: a ``VERTEX_ALPHA`` colour attribute's mere *presence* was tried
        as a 5th signal (the theory being that FO4's BSLeafAnimNode reads
        this channel as per-vertex wind-sway weight, so an already-wind-
        enabled NIF import would carry it). Checked directly against a real
        mod's assets and reverted: a static Red Rocket building shape ALSO
        had a VERTEX_ALPHA attribute, uniformly 1.0 with zero correlation to
        vertex height — indistinguishable from the vegetation samples, which
        were *also* uniformly 1.0 (one didn't even have the attribute at
        all). This channel's presence (even a non-uniform one) is not a
        reliable vegetation signal in real-world NIFs; do not reintroduce it
        without a much stronger, verified-on-real-assets distinguishing test.
        """
        if getattr(obj, "fo4_mesh_type", "") == "VEGETATION":
            return True
        if obj.get("fo4_object_type", "").upper() == "VEGETATION":
            return True
        for slot in getattr(obj, 'material_slots', []):
            mat = slot.material
            if not mat:
                continue
            if (mat.get("fo4_shader_type") == "vegetation"
                    or mat.get("fo4_core_profile") == "foliage"):
                return True
        if obj.vertex_groups:
            if all(vg.name in ExportHelpers._WIND_GROUP_NAMES
                   for vg in obj.vertex_groups):
                return True
        return False

    @staticmethod
    def detect_fo4_object_class(obj):
        """Detect the FO4 object class for *obj*.

        Returns one of:
        - ``'VEGETATION'``  – trees, plants, shrubs (vertex-group wind, no armature)
        - ``'CHARACTER'``   – player / NPC (armature with "NPC " bone names)
        - ``'CREATURE'``    – creature / animal (armature, non-NPC bone names)
        - ``'STATIC'``      – static prop / architecture (no armature)
        - ``'WEAPON'``      – weapon (explicit override or naming convention)

        An explicit ``fo4_object_type`` custom property on the object always
        wins over auto-detection.
        """
        forced = obj.get("fo4_object_type", "")
        if forced:
            return forced.upper()

        # fo4_weapon_animation.build_weapon_rig() parents a weapon mesh to a
        # Blender-only animation-authoring armature (for previewing
        # fire/reload poses before FBX->HKX export) and tags both the mesh
        # and the armature with fo4_weapon_type. Without this check, the
        # generic "parented to an armature with non-NPC bone names" rule
        # below would misclassify that weapon as CREATURE.
        if obj.get("fo4_weapon_type") or getattr(obj, "fo4_mesh_type", "") == "WEAPON":
            return 'WEAPON'

        # Power Armor and other rigid, unskinned armor pieces (tagged by
        # FO4_OT_CreatePowerArmorPiece) attach to their own frame bone rather
        # than deforming with the human skeleton -- classify like a static
        # prop, not skinned clothing.
        if obj.get("fo4_rigid_armor"):
            return 'STATIC'

        if ExportHelpers._is_vegetation_object(obj):
            return 'VEGETATION'

        # Locate armature (parent or modifier)
        arm = None
        if obj.parent and obj.parent.type == 'ARMATURE':
            arm = obj.parent
        else:
            for mod in getattr(obj, 'modifiers', []):
                if mod.type == 'ARMATURE' and getattr(mod, 'object', None):
                    arm = mod.object
                    break

        if arm:
            bone_names = [b.name for b in arm.data.bones]
            if any(b.startswith("NPC ") for b in bone_names):
                return 'CHARACTER'
            return 'CREATURE'

        return 'STATIC'

    @staticmethod
    def _is_collision_mesh(obj):
        """Return True if *obj* is a collision or occlusion mesh.

        Collision/occlusion meshes are invisible in-game and do not need
        textures or a closed (manifold) surface.  They are identified by:
        - The ``fo4_collision`` custom property set by :func:`add_collision_mesh`
        - The ``UCX_`` prefix (Fallout 4 / FBX naming convention)
        - The ``_COLLISION`` suffix (legacy add-on naming convention)
        """
        if obj.get("fo4_collision"):
            return True
        name_upper = obj.name.upper()
        return name_upper.startswith("UCX_") or name_upper.endswith("_COLLISION")

    @staticmethod
    def validate_before_export(obj):
        """Validate object before export.

        Returns (success, blocking_issues).  Texture warnings are printed to
        the console but never block the export — a mesh can be exported without
        textures and textured in the CK or Outfit Studio afterwards.
        """
        from . import mesh_helpers, texture_helpers, notification_system

        blocking = []   # hard errors — stop the export
        warnings = []   # soft issues — logged but export continues

        if obj.type == 'MESH':
            is_collision = ExportHelpers._is_collision_mesh(obj)

            # Geometry validation — non-manifold edges, missing UVs, and
            # poly-count violations are hard blockers for all visible meshes.
            success, mesh_issues = mesh_helpers.MeshHelpers.validate_mesh(
                obj, is_collision=is_collision
            )
            if not success:
                blocking.extend(mesh_issues)

            # Texture validation is advisory only — missing or unloaded textures
            # warn the user but must not prevent geometry from being exported.
            # Collision and occlusion meshes are intentionally untextured.
            if not is_collision and any(s.material for s in obj.material_slots):
                tex_ok, texture_issues = texture_helpers.TextureHelpers.validate_textures(obj)
                if not tex_ok:
                    warnings.extend(texture_issues)

            # Wind data staleness — advisory only, matching the texture-warning
            # treatment above. Vegetation whose "Wind" group/vertex-colors no
            # longer match the current geometry (e.g. new geometry joined on
            # after the original wind setup) won't fail export, but the pieces
            # will visibly separate from each other under in-game wind sway —
            # see animation_helpers.scan_wind_readiness's coverage/staleness
            # checks for the root cause this catches.
            if obj.vertex_groups.get("Wind"):
                from . import animation_helpers as _anim_helpers
                _wind_ready, _wind_issues, _ = _anim_helpers.AnimationHelpers.scan_wind_readiness(obj)
                if not _wind_ready:
                    warnings.extend(_wind_issues)

            # Bone weight limit (FO4 supports max 4 influences per vertex) — hard block.
            # Dismemberment partition groups (SBP_*) tag every vertex with weight
            # 1.0 for pyNIF's BSDismemberSkinInstance conversion — they are not a
            # skinning weight and must be excluded from this count.
            if obj.vertex_groups and obj.parent and obj.parent.type == 'ARMATURE':
                _bone_group_idx = {
                    vg.index for vg in obj.vertex_groups
                    if not ExportHelpers._is_partition_group(vg.name)
                }
                for v in obj.data.vertices:
                    influences = [
                        g for g in v.groups
                        if g.weight > 0.001 and g.group in _bone_group_idx
                    ]
                    if len(influences) > 4:
                        blocking.append(
                            f"Vertex {v.index} has {len(influences)} bone influences "
                            "(max 4 for FO4). Run 'Enforce Bone Limit' before export."
                        )
                        break

        elif obj.type == 'ARMATURE':
            from . import animation_helpers
            success, anim_issues = animation_helpers.AnimationHelpers.validate_animation(obj)
            if not success:
                blocking.extend(anim_issues)

        if warnings:
            print(f"[FO4 Export] Warnings (export will proceed): {'; '.join(warnings)}")

        return len(blocking) == 0, blocking

    @staticmethod
    def nif_exporter_available():
        """Check if the Niftools v0.1.1 exporter operator is registered."""
        blender_version = bpy.app.version
        version_str = f"{blender_version[0]}.{blender_version[1]}"
        export_scene = getattr(bpy.ops, "export_scene", None)
        if not export_scene:
            return False, f"bpy.ops.export_scene missing (Blender {version_str})"

        if not hasattr(export_scene, "nif"):
            if blender_version >= (4, 0, 0):
                return False, "Niftools exporter not registered; official v0.1.1 targets Blender ≤3.6. Install a 4.x-compatible fork or use 3.6 for export."
            return False, "Niftools exporter not registered"

        if blender_version >= (4, 0, 0):
            return True, "Niftools exporter detected on Blender 4.x (ensure compatibility; experimental)"

        return True, "Niftools exporter available"

    @staticmethod
    def pynifly_exporter_available():
        """Check if the PyNifly exporter operator (BadDogSkyrim/PyNifly) is registered.

        PyNifly is the recommended NIF exporter for Blender 4.x and 5.x.  It
        registers ``bpy.ops.export_scene.pynifly`` when installed.

        Returns
        -------
        tuple[bool, str]
            ``(True, message)`` when PyNifly is ready, ``(False, reason)`` otherwise.
        """
        export_scene = getattr(bpy.ops, "export_scene", None)
        if not export_scene:
            return False, "bpy.ops.export_scene not available"
        if hasattr(export_scene, "pynifly"):
            return True, "PyNifly exporter available (BadDog / BadDogSkyrim)"
        return False, (
            "PyNifly not installed - click 'Auto-Install PyNifly (Latest)' in the Setup & Status panel "
            "to auto-download and install it"
        )

    @staticmethod
    def get_nif_exporter_info():
        """Return which NIF exporter is active and whether it is available.

        Priority order (highest to lowest):
          1. **PyNifly** (BadDogSkyrim) - preferred for Blender 4.x / 5.x
          2. **Niftools v0.1.1**        - legacy, Blender 3.6 LTS only

        Returns
        -------
        tuple[str, bool, str]
            ``(exporter_name, available, message)`` where *exporter_name* is
            one of ``"pynifly"``, ``"niftools"``, or ``"none"``.
        """
        pynifly_ok, pynifly_msg = ExportHelpers.pynifly_exporter_available()
        if pynifly_ok:
            return "pynifly", True, pynifly_msg

        nif_ok, nif_msg = ExportHelpers.nif_exporter_available()
        if nif_ok:
            return "niftools", True, nif_msg

        return "none", False, (
            f"No NIF exporter installed.  "
            f"PyNifly: {pynifly_msg}.  "
            f"Niftools v0.1.1: {nif_msg}."
        )

    @staticmethod
    def _safe_enum(props, key, preferred, fallbacks=None):
        """Return *preferred* if it is a valid choice for the enum property *key*
        in *props*, otherwise try each item in *fallbacks* in order.

        Falls back to *preferred* unchanged when the property does not expose
        enum_items (so callers never pass an empty string to the operator).
        Returns ``None`` only when fallbacks are exhausted and enum_items are
        verifiably available.
        """
        try:
            enum_items = props[key].enum_items
            valid = {item.identifier for item in enum_items}
            if preferred in valid:
                return preferred
            for val in (fallbacks or []):
                if val in valid:
                    return val
            # preferred is not in this build's enum; skip the kwarg
            return None
        except Exception:
            # Property doesn't expose enum_items – assume preferred is valid
            return preferred

    @staticmethod
    def _build_nif_export_kwargs(filepath):
        """Assemble kwargs for the NIF exporter (Niftools v0.1.1) for Fallout 4.

        Fallout 4 NIF format requirements enforced by these settings:
          - NIF version 20.2.0.7 / user version 12 / user_version_2 130
            (automatically selected by game="FALLOUT_4")
          - BSTriShape geometry nodes – selected by the FALLOUT_4 game profile;
            do NOT use NiTriShape for FO4 or meshes will be invisible in-game.
          - BSLightingShaderProperty for materials (game profile handles this).
          - Tangent-space normal maps: use_tangent_space must be True so the
            exporter emits the tangent/bitangent vectors FO4 shaders expect.
          - Scale: 1 Blender unit = 1 NIF unit (scale_correction=1.0).
          - Geometry must be triangulated; apply_modifiers=True applies the
            temporary Triangulate modifier added by _prepare_mesh_for_nif.
        """
        kwargs = {
            "filepath": filepath,
            "use_selection": True,
        }

        try:
            props = bpy.ops.export_scene.nif.get_rna_type().properties
            prop_keys = props.keys()

            # ----------------------------------------------------------------
            # Game profile – FALLOUT_4 sets NIF 20.2.0.7 with user version 12
            # and user_version_2 130, and forces BSTriShape geometry nodes
            # which are required by Fallout 4's renderer.
            # Niftools v0.1.1 valid identifier: 'FALLOUT_4'.
            # ----------------------------------------------------------------
            if "game" in prop_keys:
                game_val = ExportHelpers._safe_enum(props, "game", "FALLOUT_4")
                if game_val:
                    kwargs["game"] = game_val

            # Export as a NIF geometry file, not a KF animation file.
            if "export_type" in prop_keys:
                et_val = ExportHelpers._safe_enum(
                    props, "export_type", "nif", fallbacks=["NIF", "nif_and_kf"]
                )
                if et_val:
                    kwargs["export_type"] = et_val

            # ----------------------------------------------------------------
            # Tangent space – FO4 BSLightingShaderProperty normal maps require
            # tangent vectors in the NIF.  Without this the mesh appears
            # flat-lit in-game regardless of the normal map texture.
            # Niftools v0.1.1 may expose this as 'use_tangent_space'.
            # ----------------------------------------------------------------
            for tkey in ("use_tangent_space", "tangent_space"):
                if tkey in prop_keys:
                    kwargs[tkey] = True
                    break

            # Keep smoothing consistent; only set when the property exists and
            # the value is a recognised enum item.
            if "smoothing" in prop_keys:
                smooth_val = ExportHelpers._safe_enum(props, "smoothing", "SMOOTH")
                if smooth_val:
                    kwargs["smoothing"] = smooth_val

            # 1 Blender unit = 1 NIF unit for FO4 (do not rescale geometry).
            if "scale_correction" in prop_keys:
                kwargs["scale_correction"] = 1.0

            # Apply modifiers so the temporary Triangulate modifier added by
            # _prepare_mesh_for_nif is baked into the exported geometry.
            if "apply_modifiers" in prop_keys:
                kwargs["apply_modifiers"] = True

            # Static (non-skinned) meshes do not carry skin data; flatten_skin
            # would corrupt weights on rigged actors so leave it False.
            if "flatten_skin" in prop_keys:
                kwargs["flatten_skin"] = False

        except Exception:
            # If anything goes wrong introspecting the operator, fall back to
            # the minimal set of kwargs so the export can still be attempted.
            pass

        return kwargs

    @staticmethod
    def _build_pynifly_export_kwargs(filepath):
        """Assemble kwargs for PyNifly (by BadDog) for Fallout 4 NIF export.

        Bundled version: PyNifly V27.0.0 (io_scene_nifly_V27.0.0.zip).
        Installed automatically from bundled/ on first addon load (BadDog's permission).

        V27.0.0 ExportSettings defaults and our overrides:
          - ``target_game = "FO4"``       → Fallout 4 BSTriShape/BSSubIndexTriShape format
                                            (V27 operator property; ExportSettings uses "game")
          - ``export_modifiers = True``   → apply modifiers before export
                                            (V27 defaults False — critical override)
          - ``export_colors = True``      → write vertex colours (LOD & alpha blending in CK)
                                            (V27 defaults True — explicit for safety)
          - ``blender_xf = False``        → NIF-native coordinates, no 90° rotation
                                            (V27 defaults False — explicit for safety)
          - ``rename_bones = True``       → convert Blender .L/.R names back to NIF names
                                            (V27 defaults True — explicit for safety)
          - ``export_animations = False`` → skip animation data for static/LOD props
                                            (V27 defaults True — critical override)
          - ``write_bodytri = False``     → no BODYTRI extra data for non-character meshes
                                            (V27 defaults True — critical override)
          - ``export_pose = False``       → export at rest/bind pose, not current pose
                                            (V27 default False — explicit to block intuit_defaults)

        V27 properties left at their defaults (correct for FO4):
          - ``rotate_bones_pretty = False``    → don't reorient bones cosmetically
          - ``rename_bones_niftools = False``  → use BadDog naming, not NifTools
          - ``preserve_hierarchy = False``     → flat BSSubIndexTriShape style

        NOTE: ``export_collision`` does not exist in PyNifly V27.  Collision is
        exported automatically when UCX_-named objects with a PASSIVE rigid body
        are present in the selection.  The caller adds UCX_ children before invoking.

        Runtime RNA introspection (get_rna_type().properties) keeps this code
        resilient to property additions/renames in future PyNifly releases.

        Credit: PyNifly by BadDog (BadDogSkyrim) - https://github.com/BadDogSkyrim/PyNifly
        """
        kwargs: dict = {
            "filepath": filepath,
        }
        try:
            props = bpy.ops.export_scene.pynifly.get_rna_type().properties
            prop_keys = props.keys()

            # ── Selection-only export ─────────────────────────────────────────
            # use_selection restricts export to the currently-selected objects.
            # The caller deselects everything then selects only the target mesh
            # (+ its UCX_ collision child) before invoking, so this is critical.
            if "use_selection" in prop_keys:
                kwargs["use_selection"] = True

            # ── Target game ──────────────────────────────────────────────────
            # V27 operator uses "target_game"; ExportSettings class uses "game".
            # "FO4" selects Fallout 4 BSTriShape / BSSubIndexTriShape format.
            # Always pass a game value — if _safe_enum returns None the property
            # exists but FO4 wasn't found in its enum; fall through and try raw
            # string assignment so PyNifly still gets "FO4" rather than
            # defaulting silently to Skyrim/SSE.
            for game_key in ("target_game", "game"):
                if game_key in prop_keys:
                    game_val = ExportHelpers._safe_enum(
                        props, game_key, "FO4",
                        fallbacks=["FALLOUT4", "Fallout4", "FALLOUT_4"],
                    )
                    kwargs[game_key] = game_val if game_val else "FO4"
                    break

            # ── Disable PyNifly's own game auto-detection ────────────────────
            # ExportNIF.intuit_defaults defaults to True and is only forced
            # False inside the operator's invoke() (UI path). We call execute()
            # directly via bpy.ops (no invoke()), so without this override
            # ExportNIF.execute() silently re-derives target_game from the
            # selection via _discover_game() and OVERWRITES the value set
            # above. For objects with no armature (or an armature lacking the
            # internal PYN_GAME_PROP marker) _discover_game() falls through to
            # a hard-coded "SKYRIM" default, regardless of what we pass in --
            # this was silently downgrading every unskinned export (static
            # props, architecture, furniture, weapons) to Skyrim's legacy
            # NiTriShape format and is why FO4-only blocks like
            # bhkNPCollisionObject/bhkPhysicsSystem never made it into those
            # files. Must explicitly opt out so our target_game sticks.
            if "intuit_defaults" in prop_keys:
                kwargs["intuit_defaults"] = False

            # ── Apply modifiers ───────────────────────────────────────────────
            # V27 defaults to False — must override to True so decimation,
            # triangulate, and other pre-export modifiers are baked in.
            for mod_key in ("export_modifiers", "apply_modifiers"):
                if mod_key in prop_keys:
                    kwargs[mod_key] = True
                    break

            # ── Vertex colours ────────────────────────────────────────────────
            # V27 defaults True; explicit so CK gets vertex colour for LOD and
            # alpha blending.
            if "export_colors" in prop_keys:
                kwargs["export_colors"] = True

            # ── Coordinate system ─────────────────────────────────────────────
            # blender_xf=False: export raw Blender coordinates as-is to the NIF.
            # FO4 NIFs are imported by PyNifly at 1:1 scale (no transform applied),
            # so no inverse transform is needed on export either.
            if "blender_xf" in prop_keys:
                kwargs["blender_xf"] = False

            # ── Bone naming ───────────────────────────────────────────────────
            # rename_bones converts Blender .L/.R suffixes back to NIF L/R
            # prefix convention.  Must match the setting used on import.
            if "rename_bones" in prop_keys:
                kwargs["rename_bones"] = True

            # ── Animations ───────────────────────────────────────────────────
            # V27 defaults True — override to False so no empty animation data
            # is embedded in static prop / LOD NIFs.
            if "export_animations" in prop_keys:
                kwargs["export_animations"] = False

            # ── BODYTRI extra data ────────────────────────────────────────────
            # V27 defaults True — override to False for static prop / LOD
            # exports.  Only character meshes with BodySlide morphs need it.
            if "write_bodytri" in prop_keys:
                kwargs["write_bodytri"] = False

            # ── Pose position ─────────────────────────────────────────────────
            # New in V27.  False = export at bind/rest pose (correct for FO4
            # armor and clothing — the CK binds at rest).  Explicitly set to
            # block V27's intuit_defaults logic from overriding this on armature
            # exports.
            if "export_pose" in prop_keys:
                kwargs["export_pose"] = False

            # ── Scale (legacy property, dropped in V27) ───────────────────────
            for scale_key in ("scale_factor", "scale_correction"):
                if scale_key in prop_keys:
                    kwargs[scale_key] = 1.0
                    break

        except Exception:
            pass  # fall back to minimal kwargs on any introspection error

        return kwargs

    @staticmethod
    def _snapshot_and_fix_emissive_for_export(objects):
        """Work around PyNifly exporting Emissive Color/Multiple as flat white.

        PyNifly's exporter (io_scene_nifly/nif/shader_io.py) reads a shape's
        Emissive Color/Multiple straight from whichever node is linked into
        the material's Material Output socket::

            shape.shader.properties.Emissive_Mult = self.shader_node.inputs['Emission Strength'].default_value
            shape.shader.properties.Emissive_Color[i] = self.shader_node.inputs[em].default_value[i]

        For our materials that node is the outer Principled BSDF, not the
        "Fallout 4 MTS" node group PyNifly itself builds on import. Our BSDF's
        "Emission Color" input is wired to the Glow Map texture (a deliberate
        repurposing for an in-Blender glow preview) and its "Emission
        Strength" sits at Blender's plain default of 1.0 — neither carries the
        mesh's real baked tint. That tint is still sitting untouched on the
        "Fallout 4 MTS" group node's own unconnected "Emission Color"/
        "Emission Strength" inputs (that's what PyNifly used to populate them
        on import), but PyNifly's export never looks there.

        This snapshots each exported material's current wiring, temporarily
        unlinks/overwrites the outer node's Emission Color/Strength with the
        group's true values so PyNifly's export reads the right numbers, and
        returns a list of restore-info dicts. Always pair this with a
        ``_restore_emissive_after_export`` call in a ``finally`` block so the
        artist's actual node graph (Glow Map link included) is put back
        exactly as it was, regardless of whether the export succeeded.
        """
        restore = []
        seen_materials = set()
        for obj in objects or []:
            mat = getattr(obj, 'active_material', None)
            if mat is None or not getattr(mat, 'use_nodes', False) or mat.node_tree is None:
                continue
            if mat.name in seen_materials:
                continue
            seen_materials.add(mat.name)
            nt = mat.node_tree
            try:
                mo = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'), None)
                if mo is None or not mo.inputs[0].is_linked:
                    continue
                shader_node = mo.inputs[0].links[0].from_node

                # The "Fallout 4 MTS" (or "... - Face") group node holding the
                # real, never-overwritten tint values.
                grp = next(
                    (n for n in nt.nodes
                     if n.type == 'GROUP' and n.node_tree
                     and 'Fallout 4 MTS' in n.node_tree.name),
                    None)
                if grp is None or grp is shader_node:
                    continue  # nothing to copy from, or already the source of truth

                if 'Emission Color' in shader_node.inputs:
                    em_key = 'Emission Color'
                elif 'Emission' in shader_node.inputs:
                    em_key = 'Emission'
                else:
                    continue
                if 'Emission Strength' not in shader_node.inputs:
                    continue
                if 'Emission Color' not in grp.inputs or 'Emission Strength' not in grp.inputs:
                    continue

                color_in = shader_node.inputs[em_key]
                strength_in = shader_node.inputs['Emission Strength']

                entry = {
                    'material': mat,
                    'color_input_name': em_key,
                    'color_was_linked': color_in.is_linked,
                    'color_link_from': None,
                    'color_default': tuple(color_in.default_value[:]),
                    'strength_was_linked': strength_in.is_linked,
                    'strength_link_from': None,
                    'strength_default': strength_in.default_value,
                }
                if color_in.is_linked:
                    link = color_in.links[0]
                    entry['color_link_from'] = (link.from_node.name, link.from_socket.name)
                    nt.links.remove(link)
                if strength_in.is_linked:
                    link = strength_in.links[0]
                    entry['strength_link_from'] = (link.from_node.name, link.from_socket.name)
                    nt.links.remove(link)

                color_in.default_value = tuple(grp.inputs['Emission Color'].default_value[:])
                strength_in.default_value = grp.inputs['Emission Strength'].default_value

                restore.append(entry)
            except Exception as _e:
                print(f"[FO4Export] Emissive pre-export fix skipped for "
                      f"'{mat.name if mat else '?'}': {_e}")
        return restore

    @staticmethod
    def _restore_emissive_after_export(restore):
        """Undo ``_snapshot_and_fix_emissive_for_export``'s temporary rewiring."""
        for entry in restore or []:
            try:
                mat = entry['material']
                nt = mat.node_tree
                mo = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'), None)
                if mo is None or not mo.inputs[0].is_linked:
                    continue
                shader_node = mo.inputs[0].links[0].from_node
                em_key = entry['color_input_name']
                if em_key not in shader_node.inputs or 'Emission Strength' not in shader_node.inputs:
                    continue
                color_in = shader_node.inputs[em_key]
                strength_in = shader_node.inputs['Emission Strength']

                color_in.default_value = entry['color_default']
                strength_in.default_value = entry['strength_default']

                if entry['color_was_linked'] and entry['color_link_from']:
                    from_node_name, from_socket_name = entry['color_link_from']
                    from_node = nt.nodes.get(from_node_name)
                    if from_node is not None and from_socket_name in from_node.outputs:
                        nt.links.new(from_node.outputs[from_socket_name], color_in)
                if entry['strength_was_linked'] and entry['strength_link_from']:
                    from_node_name, from_socket_name = entry['strength_link_from']
                    from_node = nt.nodes.get(from_node_name)
                    if from_node is not None and from_socket_name in from_node.outputs:
                        nt.links.new(from_node.outputs[from_socket_name], strength_in)
            except Exception as _e:
                print(f"[FO4Export] Emissive post-export restore failed: {_e}")

    @staticmethod
    def _prepare_mesh_for_nif(obj):
        """Prepare a mesh object so it meets Fallout 4 / PyNifly / Niftools v0.1.1 requirements.

        Performs (in order):
          1. Apply pending scale and rotation transforms – unapplied transforms
             are the single most common cause of distorted geometry in-game.
             This step is ALWAYS performed regardless of the "Apply Transforms"
             setting in the Export panel; leaving scale unapplied previously
             caused exports to silently land at the wrong (÷70) in-game size
             (see the note below), so the checkbox there is display-only for
             this path and export always applies transforms for safety.
          1.5. Remove doubles at the "Remove Doubles Threshold" set in the
             Export panel (mirrors ``mesh_helpers.optimize_mesh``'s own
             remove-doubles step so the two don't drift apart). Skipped when
             the threshold is 0. Honors "Preserve UVs" by only merging verts
             whose UVs also match, so texture seams are never collapsed.
          2. Ensure at least one UV map exists – both exporters require UV
             coordinates on every exported mesh.
          3. Add a temporary ``_FO4_Triangulate`` modifier when the mesh
             contains quads or n-gons, because FO4 BSTriShape only stores
             triangles.  PyNifly can handle quads internally, but the
             modifier guarantees a clean result for both exporters.
          4. Enable Auto Smooth for consistent tangent/normal export (skipped
             silently on Blender 4.x where the attribute was removed).

        Returns a list of modifier names that were added.  The caller MUST
        remove them after export so the user's mesh is not permanently altered.
        """
        added_modifiers = []

        if bpy.context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)

        # Force single-user mesh data BEFORE any of the mutating steps below
        # (unweighted-vertex fix, Remove Doubles, UV creation, etc.) -- not
        # just before the scale/rotation apply, which used to be the only
        # place this was checked (and only conditionally, inside `if
        # needs_scale or needs_rot:`). Confirmed as a real, reproducible bug:
        # when an object arrives here with scale/rotation already at
        # identity (e.g. a mesh laid out via linked/Alt-D duplicates for
        # variation, which deliberately share one mesh datablock across
        # several objects), that scale/rotation branch never ran its own
        # single-user copy, so the Remove Doubles step below rewrote the
        # SHARED datablock in place via bmesh -- silently mutating every
        # other object still pointing at that same data, not just this one.
        # This reproduced independent of Blender version (it's not an
        # engine bug, just a missing guard in our own code) and explains a
        # real user report: a duplicated/rearranged mesh plus its
        # separately-generated collision object ending up with corrupted/
        # mismatched geometry after export, even though the collision
        # generator's own code was already careful about single-user data.
        try:
            if obj.data and obj.data.users > 1:
                obj.data = obj.data.copy()
        except Exception:
            pass

        # 0. Fix unweighted vertices ------------------------------------------
        #    Vertices with zero total bone weight cause NIF exporters to produce
        #    corrupted or missing geometry.  For skinned meshes: assign to nearest
        #    bone.  For vegetation (no armature): generate a Wind weight gradient.
        from . import mesh_helpers as _mh_prep
        _mh_prep.MeshHelpers.fix_unweighted_vertices(obj)

        # 1. Apply scale and rotation -----------------------------------------
        #    Unapplied scale causes geometry to arrive at the wrong size in FO4;
        #    unapplied rotation causes normals to point in the wrong direction.
        try:
            needs_scale = obj.scale[:] != (1.0, 1.0, 1.0)
            needs_rot = obj.rotation_euler[:] != (0.0, 0.0, 0.0)
        except Exception:
            needs_scale = needs_rot = True

        # A mirrored placement -- an odd number of negative scale axes, the
        # standard way modders flip a linked-duplicate instance (Alt-D) left/
        # right for variety without re-unwrapping it -- has a negative
        # transform determinant. Applying that scale below correctly bakes
        # the mirrored geometry and flips the triangle winding to match (so
        # the mesh keeps looking right in Blender's own viewport), but it
        # does NOT touch the UV data -- UVs are plain 2D coordinates with no
        # concept of the 3D mirror. The result is a mesh whose 3D winding is
        # now mirrored but whose UVs still assume the original, unmirrored
        # orientation. That mismatch is invisible in Blender (which derives
        # backface/shading from the transform at render time) but corrupts
        # the tangent basis PyNifly bakes into the NIF, which is read out as
        # a flipped/wrong texture in Creation Kit and in-game. Confirmed as a
        # real, reproducible cause of "the UV came out wrong on export" for
        # objects placed via Alt-D + negative scale even when the source
        # mesh and its UVs were never directly touched.
        try:
            is_mirrored = (obj.scale.x * obj.scale.y * obj.scale.z) < 0.0
        except Exception:
            is_mirrored = False

        if needs_scale or needs_rot:
            # transform_apply raises "Cannot apply to a multi-user data" when the
            # mesh datablock is shared (a common result of Alt-D / imported dupes).
            # That failure was silently swallowed, leaving scale unapplied so
            # validate_before_export blocked the whole export ("scale not
            # applied") — forcing the user onto PyNifly's raw export, which then
            # writes the mesh at its downsized ÷70 size.  Make the data single-
            # user first so the apply actually succeeds.
            try:
                if obj.data and obj.data.users > 1:
                    obj.data = obj.data.copy()
            except Exception:
                pass
            # Isolate `obj` before transform_apply -- it operates on EVERY
            # currently selected object, not just the one passed in here.
            # export_scene_as_single_nif calls _prepare_mesh_for_nif() once
            # per mesh in a loop that deliberately leaves each earlier
            # mesh selected afterward (see that loop's own comment: clearing
            # the selection there used to make "Export Entire Scene as NIF"
            # silently drop all but one shape). Without isolating here too,
            # this transform_apply call used to also re-apply to every one
            # of those still-selected earlier meshes. When several of them
            # were still sharing mesh data with each other (Alt-D linked
            # duplicates -- the normal way to tile repeated building
            # pieces), Blender bakes each selected object's own transform
            # into that SAME shared datablock in sequence, collapsing every
            # instance onto one blended position -- reproducing exactly
            # "everything I add ends up in the middle" on export. Save and
            # restore the prior selection instead of a plain
            # select_all(DESELECT), so this doesn't reintroduce the
            # single-shape-dropped bug that comment already fixed once.
            _prev_selected = [so for so in bpy.context.selected_objects if so is not obj]
            try:
                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True)
                bpy.context.view_layer.objects.active = obj
                try:
                    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
                except Exception:
                    # Last resort: fold rotation+scale into the mesh via matrix math
                    # when the operator context rejects the call.  Location is kept on
                    # the object (matching transform_apply(location=False)).
                    try:
                        import mathutils
                        loc, rot, scale = obj.matrix_basis.decompose()
                        rs = rot.to_matrix().to_4x4() @ mathutils.Matrix.Diagonal(scale.to_4d())
                        obj.data.transform(rs)
                        obj.matrix_basis = mathutils.Matrix.Translation(loc)
                    except Exception:
                        pass  # continue; validation will surface any remaining issue
            finally:
                for so in _prev_selected:
                    try:
                        so.select_set(True)
                    except Exception:
                        pass

            if is_mirrored:
                # Compensate for the mirror bake above by flipping every UV's
                # U coordinate, so the exported UV winding stays correctly
                # paired with the now-mirrored 3D winding instead of silently
                # exporting a mismatched tangent basis for this instance.
                try:
                    _uv_layer_mirror = obj.data.uv_layers.active
                    if _uv_layer_mirror is not None:
                        for _loop_mirror in _uv_layer_mirror.data:
                            _loop_mirror.uv.x = 1.0 - _loop_mirror.uv.x
                        print(
                            f"[FO4 Add-on] '{obj.name}' had a mirrored (negative "
                            "scale) transform — flipped its UVs to compensate "
                            "before export."
                        )
                except Exception:
                    pass

        # 1.5. Remove doubles ----------------------------------------------------
        #    The Export panel's "Remove Doubles Threshold" slider previously had
        #    no effect on the actual export -- it only ever fed the separate
        #    "Optimize for FO4" operator family (mesh_helpers.optimize_mesh).
        #    Read the same addon preferences that operator uses so the two
        #    stay in sync, and actually run it here.
        try:
            from . import preferences as _prefs_mod
            _prefs = _prefs_mod.get_preferences()
            _threshold = getattr(_prefs, "optimize_remove_doubles_threshold", 0.0001) if _prefs else 0.0001
            _preserve_uvs = getattr(_prefs, "optimize_preserve_uvs", True) if _prefs else True
        except Exception:
            _threshold, _preserve_uvs = 0.0001, True

        if _threshold and _threshold > 0.0:
            try:
                # Pre-check for near-duplicate vertices with a lightweight
                # KD-tree BEFORE touching the mesh at all. Remove Doubles used
                # to run its full bmesh from_mesh/to_mesh round-trip
                # unconditionally on EVERY export, even a freshly-imported,
                # completely unedited mesh that already has clean topology
                # (Billy's own repro: import a stock FO4 NIF, change nothing,
                # export -- UV comes out wrong via our export, but not via
                # PyNifly's raw export, which never touches the mesh at all).
                # That made this bmesh surgery the one remaining structural
                # difference between our export and PyNifly's raw export on
                # an otherwise byte-identical mesh. Skip it entirely when
                # there is nothing to merge, so an already-clean mesh reaches
                # the exporter completely untouched, exactly like PyNifly's
                # own path.
                import mathutils as _mu_dd
                _verts = obj.data.vertices
                _kd = _mu_dd.kdtree.KDTree(len(_verts))
                for _i, _v in enumerate(_verts):
                    _kd.insert(_v.co, _i)
                _kd.balance()
                _has_dupes = False
                for _i, _v in enumerate(_verts):
                    for (_co, _idx, _dist) in _kd.find_range(_v.co, _threshold):
                        if _idx != _i:
                            _has_dupes = True
                            break
                    if _has_dupes:
                        break

                if _has_dupes:
                    import bmesh as _bmesh_dd
                    _bm = _bmesh_dd.new()
                    _bm.from_mesh(obj.data)
                    _uv_layer = _bm.loops.layers.uv.active
                    _kwargs = {'verts': _bm.verts, 'dist': _threshold}
                    if _preserve_uvs and _uv_layer is not None:
                        _kwargs['use_uvs'] = True
                    try:
                        _bmesh_dd.ops.remove_doubles(_bm, **_kwargs)
                    except TypeError:
                        _kwargs.pop('use_uvs', None)
                        _bmesh_dd.ops.remove_doubles(_bm, **_kwargs)
                    _bm.normal_update()
                    _bm.to_mesh(obj.data)
                    _bm.free()
                    obj.data.update()
                    # Force a full dependency-graph refresh. Without this, the
                    # exporter (which reads the evaluated mesh) could still
                    # see stale pre-merge topology/UV state on some Blender
                    # builds -- surfacing as garbled UVs even though the
                    # merge itself succeeded.
                    try:
                        bpy.context.view_layer.update()
                        bpy.context.evaluated_depsgraph_get().update()
                    except Exception:
                        pass
            except Exception as _dd_err:
                print(f"[FO4 Add-on] Remove Doubles skipped for '{obj.name}': {_dd_err}")

        # 2. Ensure UV map -------------------------------------------------------
        #    Both Niftools v0.1.1 and PyNifly require at least one UV map.
        #
        #    If the mesh already has a UV map (e.g. imported from an existing NIF
        #    with textures assigned), we keep it and repair it instead of
        #    overwriting it with a smart-project that won't align to the texture.
        #
        #    If there truly is no UV map, create one via smart_project as a
        #    placeholder so the exporter doesn't crash; the user should unwrap
        #    properly before final export.
        if not obj.data.uv_layers:
            obj.data.uv_layers.new(name="UVMap")
            try:
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.uv.smart_project(angle_limit=66.0)
                bpy.ops.object.mode_set(mode='OBJECT')
                print(
                    f"[FO4 Add-on] '{obj.name}' had no UV map — created one via "
                    "smart_project. For textured meshes, unwrap manually and align "
                    "UVs to the existing texture before exporting."
                )
            except Exception:
                try:
                    bpy.ops.object.mode_set(mode='OBJECT')
                except Exception:
                    pass
        else:
            # Mesh already has UVs — silently repair any flipped islands.
            # Flipped UV islands (CW winding in Blender space) become CCW after
            # the NIF V-flip, inverting the tangent basis and making those faces
            # appear black or invisible in the Creation Kit preview / in-game.
            try:
                from . import fo4_uv_tools as _uv
                uv_warnings = _uv.auto_fix_uv_before_export(obj)
                for w in uv_warnings:
                    print(f"[FO4 UV] {obj.name}: {w}")
            except Exception as _uv_err:
                print(f"[FO4 UV] UV auto-fix skipped for '{obj.name}': {_uv_err}")

        # 3. Triangulate ---------------------------------------------------------
        #    FO4 BSTriShape / BSSubIndexTriShape nodes only store triangles.
        #    Add a temporary Triangulate modifier (removed after export) to
        #    guarantee a clean tri-only mesh reaches the exporter.
        has_non_tris = any(len(p.vertices) > 3 for p in obj.data.polygons)
        if has_non_tris:
            mod = obj.modifiers.new(name="_FO4_Triangulate", type='TRIANGULATE')
            mod.quad_method = 'BEAUTY'
            mod.ngon_method = 'BEAUTY'
            try:
                mod.keep_custom_normals = True
            except AttributeError:
                pass  # older Blender builds lack this flag
            added_modifiers.append(mod.name)

        # 4. Auto Smooth ---------------------------------------------------------
        #    Ensures exported tangent vectors are coherent with mesh normals.
        #    The attribute was removed in Blender 4.x; skip silently.
        try:
            obj.data.use_auto_smooth = True
            obj.data.auto_smooth_angle = 3.14159265358979  # 180° – smooth all
        except AttributeError:
            pass

        return added_modifiers
    
    @staticmethod
    def _infer_fo4_scale_for_untagged(obj):
        """Best-effort ×70 scale factor for a mesh that never got the
        ``fo4_unit_scale`` tag -- i.e. it never went through this addon's own
        import pipeline (a BlenderKit asset, hand-modeled content, anything
        added via Blender's native File > Append/Link, etc.).

        Without this, such an object silently skips the ×70 restoration
        entirely and gets written to the NIF at its native Blender-metre
        scale directly -- confirmed on a real case: an untagged BlenderKit
        bush exported at its native ~0.56-unit height with zero restoration,
        ending up ~70x too small in-game/NifSkope (exactly the same class of
        bug as the FBX-import-side version of this issue, just showing up on
        export instead of import for content that bypassed our import
        tagging altogether).

        Blender's own native convention is metres; assume the standard ×70
        conversion applies unless the object is already suspiciously large
        (i.e. plausibly already at Havok scale for some other reason --
        genuine FO4-scale content runs into the hundreds of units for a full
        character, vs. single digits to a few tens for typical props/
        vegetation modeled at real-world metre scale).

        Returns the scale factor to use, or None if the object looks like it
        might already be at Havok scale (left untouched, matching the
        pre-existing behaviour for tagged objects with no scale needed).
        """
        try:
            dims = obj.dimensions
            if max(dims.x, dims.y, dims.z) < 20.0:
                return _FO4_UNIT_SCALE_INV
        except Exception:
            pass
        return None

    @staticmethod
    def _find_collision_mesh(obj):
        """Return a collision object associated with *obj*, if any.

        Checks (in order):
        1. Direct children of *obj* that carry the ``fo4_collision`` flag AND
           whose name matches ``UCX_{obj.name}`` (most specific / fastest).
        2. Any direct child with the ``fo4_collision`` flag (less specific).
        3. Scene siblings whose name matches ``UCX_{name}`` (Fallout 4 / FBX
           convention) or the legacy ``{name}_COLLISION`` suffix.
        4. A single unclaimed native-collision object (``pynRigidBody`` set --
           PyNifly's own import-time marker for a ``bhkPhysicsSystem`` shape,
           the same property our own authoring tools set) sharing *obj*'s
           ``fo4_source_nif``. Real FO4 assets imported with pre-existing
           native Havok collision arrive this way -- unparented, no
           ``fo4_collision`` flag, no UCX_ name -- so none of checks 1-3 ever
           matched them, and a plain reimport-then-reexport of such an asset
           silently dropped its collision entirely (confirmed on
           gsfungusbarnacletrap01.nif). Only fires when exactly one such
           object exists for this source NIF -- with more than one, we can't
           tell which real mesh each collision shape belongs to (no parent/
           spatial data survives import), and guessing wrong would attach
           the wrong collision rather than none at all.
        """
        ucx_name = f"UCX_{obj.name}".upper()
        legacy_name = f"{obj.name}_COLLISION".upper()

        # Fastest path: parented children – prefer exact name match
        for child in obj.children:
            if child.get("fo4_collision") and child.name.upper() == ucx_name:
                return child
        for child in obj.children:
            if child.get("fo4_collision"):
                return child

        for scene in getattr(obj, 'users_scene', []):
            for o in scene.objects:
                if o is obj:
                    continue
                oname = o.name.upper()
                if oname == ucx_name or oname == legacy_name:
                    return o

        source_nif = obj.get("fo4_source_nif")
        if source_nif:
            for scene in getattr(obj, 'users_scene', []):
                candidates = [
                    o for o in scene.objects
                    if o is not obj and o.get("pynRigidBody")
                    and o.get("fo4_source_nif") == source_nif
                ]
                if len(candidates) == 1:
                    return candidates[0]
        return None

    @staticmethod
    def _find_collision_meshes(obj):
        """Return ALL collision objects associated with *obj*, as a list.

        ``fo4.generate_multi_convex_collision`` ("Multi-Piece Convex
        Collision" / the custom-collision generator) creates several
        ``UCX_<name>_00``, ``UCX_<name>_01``, ... children, each tagged
        ``fo4_collision`` -- but :meth:`_find_collision_mesh` (singular)
        only ever returns the FIRST parented child it finds. Every export
        call site that built its export selection from that single result
        (NIF export, scale round-trip, batch export) silently dropped every
        piece but one, so a multi-piece collision generated in Blender
        shipped in-game with just one convex hull instead of the whole set
        -- confirmed as a real bug matching a user report of custom
        collision "not working correctly". This returns every parented
        ``fo4_collision`` child, falling back to :meth:`_find_collision_mesh`
        (single-piece / legacy / native-import cases) when there are none.
        """
        pieces = [c for c in obj.children if c.get("fo4_collision")]
        if pieces:
            return pieces
        single = ExportHelpers._find_collision_mesh(obj)
        return [single] if single is not None else []

    @staticmethod
    def _find_connect_points(obj):
        """Return all connect-point child objects (parent and child kinds)
        anywhere under *obj*, so they survive being included in the export
        selection.

        PyNifly identifies connect points purely by Blender object name --
        ``BSConnectPointParents::P-<name>`` (an attachment point FOR other
        things, e.g. a scope/magazine attaching to this weapon) or
        ``BSConnectPointChildren::C-<name>`` (how this object attaches to
        something else, e.g. a weapon attaching to a hand bone) -- gathered
        from whatever is in ``bpy.context.selected_objects`` at export time
        (see ``nif/connectpoint.py``'s ``is_parent``/``is_child``). Without
        this, connect points a user hand-built under the weapon object were
        silently excluded every export, since export_mesh_to_nif explicitly
        deselects everything and selects only ``[obj, collision]``.
        """
        found = []
        stack = list(obj.children)
        while stack:
            child = stack.pop()
            name = child.name
            if name.startswith("BSConnectPointParents") or name.startswith("BSConnectPointChildren"):
                found.append(child)
            stack.extend(child.children)
        return found

    @staticmethod
    def _find_skinned_armature(obj):
        """Return the armature *obj* is skinned to (via an Armature modifier,
        falling back to a direct ARMATURE-type parent), or None."""
        for m in obj.modifiers:
            if m.type == 'ARMATURE' and m.object is not None:
                return m.object
        if obj.parent is not None and obj.parent.type == 'ARMATURE':
            return obj.parent
        return None

    @staticmethod
    def _scale_armature_bones(armature_obj, factor):
        """Multiply every edit-bone head/tail by *factor*, in place — used to
        move a skinned armature's bind-pose bones in lockstep with a mesh's
        own ×70 export-scale restoration.

        Deliberately does NOT touch the armature object's own ``.scale`` /
        ``transform_apply`` — a skinned mesh is normally parented to its
        armature (standard Blender "Parent > Armature Deform"), and scaling
        the parent object's own transform double-applies through that
        parent-child relationship (confirmed empirically: Blender's
        ``transform_apply`` does not reliably compensate the child's world
        transform when the operation is isolated to just the armature via a
        context override). Editing edit-bone coordinates directly never
        touches the object-level transform, so it can't leak into children.
        """
        prev_active = bpy.context.view_layer.objects.active
        _was_hidden = armature_obj.hide_get()
        if _was_hidden:
            armature_obj.hide_set(False)
        bpy.context.view_layer.objects.active = armature_obj
        bpy.ops.object.mode_set(mode='EDIT')
        for eb in armature_obj.data.edit_bones:
            eb.head = eb.head * factor
            eb.tail = eb.tail * factor
        bpy.ops.object.mode_set(mode='OBJECT')
        if _was_hidden:
            armature_obj.hide_set(True)
        if prev_active is not None:
            bpy.context.view_layer.objects.active = prev_active

    @staticmethod
    def _write_kit_credits(obj, filepath):
        """If ``obj`` (or, for a merged multi-object export, any of the
        objects that fed into it) has kit/creator/license metadata set via
        the fo4_kit_name / fo4_kit_creator / fo4_kit_license custom
        properties, append a de-duplicated credit line to a CREDITS.txt file
        next to the exported NIF. This is the one thing every export call
        site funnels through, so it is the single place to hook this without
        touching a dozen separate operators.

        Nothing is written when no kit metadata is set on the object -- an
        original, non-kit mesh produces no CREDITS.txt at all.
        """
        kit_name = str(getattr(obj, "fo4_kit_name", "") or "").strip()
        kit_creator = str(getattr(obj, "fo4_kit_creator", "") or "").strip()
        kit_license = str(getattr(obj, "fo4_kit_license", "") or "").strip()
        if not (kit_name or kit_creator or kit_license):
            return

        out_dir = os.path.dirname(str(filepath)) or "."
        credits_path = os.path.join(out_dir, "CREDITS.txt")

        parts = []
        parts.append(kit_name if kit_name else "(unnamed kit)")
        if kit_creator:
            parts.append(f"by {kit_creator}")
        if kit_license:
            parts.append(f"— license: {kit_license}")
        line = "Asset kit: " + "  ".join(parts)

        existing = ""
        if os.path.exists(credits_path):
            try:
                with open(credits_path, "r", encoding="utf-8") as f:
                    existing = f.read()
            except Exception:
                existing = ""

        # Same kit credited more than once (multiple meshes from one pack) —
        # only need the line in the file once.
        if line.strip() in [l.strip() for l in existing.splitlines()]:
            return

        header = (
            "Asset credits for this mod\n"
            "Generated by Mossy's Blender addon from per-object kit metadata.\n"
            "Include this information in your mod page / readme as required by each kit's license.\n"
            + ("-" * 70) + "\n"
        )
        try:
            with open(credits_path, "a", encoding="utf-8") as f:
                if not existing.strip():
                    f.write(header)
                f.write(line + "\n")
        except Exception as e:
            print(f"[FO4Export] Could not write {credits_path}: {e}")

    @staticmethod
    def export_mesh_to_nif(obj, filepath):
        """Export mesh to NIF format, preferring PyNifly then Niftools v0.1.1,
        falling back to FBX when neither is installed.

        Exporter priority:
          1. **PyNifly** (BadDogSkyrim) - preferred for Blender 4.x / 5.x
          2. **Niftools v0.1.1**        - legacy, Blender 3.6 LTS only
          3. **FBX fallback**           - when no NIF exporter is available

        Pre-export preparation (applied automatically, reversed after export):
          - Scale and rotation transforms are applied so geometry arrives at the
            correct size and orientation in Fallout 4.
          - A UV map is created (smart-unwrapped) if the mesh has none.
          - A temporary Triangulate modifier is added when the mesh has quads /
            n-gons because FO4 BSTriShape nodes require triangles only.

        The NIF/FBX exporters are notoriously sensitive to stray vertex groups.
        If a mesh contains weights but isn't skinned to an armature the export
        can produce collapsed or otherwise corrupted geometry.  We fail early in
        that case so the user can clean up the mesh.
        """

        if not filepath or not str(filepath).strip():
            return False, "Export filepath is empty — choose a destination file first"

        filepath = str(filepath).strip()

        # PyNifly-imported NIFs name multiple NiTriShapes that share a base
        # name with a ":<index>" suffix (e.g. "GSFungusTreeCluster01:39",
        # confirmed directly from a real PYNIFLY IMPORT NIF log). When a LOD/
        # export tool builds a filename straight from such an obj.name (e.g.
        # "GSFungusTreeCluster01:39_LOD_0.nif"), the colon lands INSIDE the
        # filename, not just after a drive letter. NTFS treats "name:rest" as
        # an Alternate Data Stream reference -- Windows silently creates an
        # empty 0-byte file named only the part before the first colon
        # ("GSFungusTreeCluster01") with no extension, while the actual NIF
        # bytes get written into an invisible, useless ADS. Confirmed
        # directly against a user's mod folder: PyNifly's own export log
        # showed "to file: ...\GSFungusTreeCluster01:39_LOD_0.nif" and
        # "completed SUCCESSFULLY", yet the file on disk was an empty,
        # extensionless "GSFungusTreeCluster01" -- the export silently
        # "succeeded" into a stream nothing can read.  Sanitize only the
        # final path component (never the drive letter's own colon, e.g.
        # "E:\...") by replacing every Windows-reserved character.
        import re as _re
        _dirname, _basename = os.path.split(filepath)
        _sanitized_basename = _re.sub(r'[<>:"/\\|?*]', '_', _basename)
        if _sanitized_basename != _basename:
            filepath = os.path.join(_dirname, _sanitized_basename)

        # PyNifly writes to exactly the path given, so a filename typed without
        # an extension (e.g. "test") produces an extensionless file that Windows
        # and NifSkope don't recognise as a NIF.  Force a .nif extension.
        _root, _ext = os.path.splitext(filepath)
        if _ext.lower() == ".fbx":
            pass  # explicit FBX request — leave as-is for the FBX fallback
        elif _ext.lower() != ".nif":
            filepath = filepath + ".nif" if not _ext else _root + ".nif"

        if obj.type != 'MESH':
            return False, "Object is not a mesh"
        # ...existing code...

        try:
            _diag_coll = ExportHelpers._find_collision_mesh(obj)
            print(f"[FO4Export][ScaleDiag] ENTRY obj={obj.name} obj.scale={tuple(obj.scale)} "
                  f"collision={_diag_coll.name if _diag_coll else None} "
                  f"collision.scale={tuple(_diag_coll.scale) if _diag_coll else None}")
        except Exception as _diag_exc:
            print(f"[FO4Export][ScaleDiag] ENTRY diag failed: {_diag_exc}")

        # Do not export collision meshes created by the addon
        if obj.get("fo4_collision") or obj.name.upper().endswith("_COLLISION") or obj.name.upper().startswith("UCX_"):
            return False, "Collision meshes are not intended for export; select the source mesh instead"
        # ...existing code...

        # Reject meshes with orphaned skin weights that would corrupt the export,
        # but allow FO4-native non-armature vertex groups:
        #   - Wind/WindWeight/WindStiff  — procedural vegetation wind (no armature needed)
        #   - LOD0/LOD1/LOD2/LOD3/LOD4  — renderer LOD face culling (no armature needed)
        #   - FO4 Seg NNN / SBP_NNN     — dismemberment partition groups (no armature needed)
        if obj.vertex_groups and not ExportHelpers._has_armature(obj):
            # A vertex group with literally zero weighted vertices is inert --
            # it has no effect on the exported mesh either way. Confirmed as a
            # real cause of this check blocking a perfectly good vegetation
            # export over a stray empty "Group" (Blender's own default name
            # for a vertex group created with nothing assigned to it -- an
            # accidental "+" click, a modifier's placeholder, or an import
            # leftover) that carried no actual weight data. Purge those
            # before judging, so only groups that would ACTUALLY corrupt the
            # export -- real skin weight data with nowhere to bind -- still
            # block it.
            _group_has_weight = {vg.index: False for vg in obj.vertex_groups}
            for _v in obj.data.vertices:
                for _ve in _v.groups:
                    if _ve.weight > 1e-6:
                        _group_has_weight[_ve.group] = True

            unknown = [
                vg.name for vg in obj.vertex_groups
                if vg.name not in ExportHelpers._ARMATURE_FREE_GROUP_NAMES
                and not ExportHelpers._is_partition_group(vg.name)
                and _group_has_weight.get(vg.index, False)
            ]
            if unknown:
                return False, (
                    f"Mesh has vertex group(s) {unknown[:3]} but no armature "
                    "– remove weights or parent to an armature before exporting"
                )

        exporter, nif_available, nif_message = ExportHelpers.get_nif_exporter_info()
        from . import mesh_helpers as _mh

        # Try native NIF export first when available
        if nif_available:
            added_mods = []
            restore_scale_objs = []      # (obj, orig_scale) pairs — restored in finally
            restore_armature_scales = [] # (armature_obj, factor) pairs — restored in finally
            wind_token = None        # author-only wind rig detached during export
            weapon_token = None      # author-only weapon animation rig detached during export

            try:
                # ── Strip author-only procedural-wind rig ──────────────────────
                # apply_wind_animation() parents vegetation to a preview-only
                # "Wind" armature.  Left attached, PyNifly skins the mesh to a
                # bogus bone and corrupts it.  Detach now, restore in finally.
                wind_token = ExportHelpers._detach_wind_authoring_rig(obj)

                # ── Strip author-only weapon animation rig ──────────────────────
                # build_weapon_rig() parents a weapon mesh to a preview-only
                # animation-authoring armature. Real weapons are never skinned
                # (has_skin_instance=0) -- detach now, restore in finally.
                weapon_token = ExportHelpers._detach_weapon_authoring_rig(obj)

                # ── FO4 unit scale round-trip ──────────────────────────────────
                # Objects imported via our "Import Asset" button are stored at
                # Blender-metre scale (÷70) and tagged with fo4_unit_scale=69.99125.
                # Before exporting we multiply back to FO4 game units (×70), apply
                # the scale so PyNifly sees correct geometry, then restore in finally.
                #
                # The UCX_ collision child is scaled AFTER the main mesh so that
                # the parent's 70× contribution is neutralised first — both objects
                # end up at independent 500-unit world positions as PyNifly requires.
                fo4_scale = obj.get("fo4_unit_scale", None)
                if fo4_scale is None:
                    fo4_scale = ExportHelpers._infer_fo4_scale_for_untagged(obj)
                if fo4_scale:
                    # Collect: main mesh first, then any collision child -- it
                    # must move in lockstep with obj regardless of whether it
                    # carries its own fo4_unit_scale tag. Blender's
                    # duplicate() DOES copy custom properties (verified), so
                    # a collision mesh generated from a TAGGED source
                    # already inherits the tag and this used to happen to
                    # work by accident. But when obj itself was untagged and
                    # fo4_scale came from the dimension-based
                    # _infer_fo4_scale_for_untagged() fallback just above,
                    # the collision mesh (also untagged, being a duplicate
                    # of an untagged source) was silently excluded here --
                    # the main mesh got scaled/restored but its collision
                    # hull didn't, leaving it ~70x too small relative to the
                    # now-correctly-scaled visual geometry. Gate on fo4_scale
                    # itself (already resolved above, tag-or-inferred) not on
                    # the collision object's own tag.
                    _scale_targets = [obj]
                    _scale_targets.extend(ExportHelpers._find_collision_meshes(obj))

                    # The skinned armature's bind-pose bones must move in lockstep
                    # with the mesh's own ×70 restoration, or the exported NIF ends
                    # up with mesh geometry at real Havok scale but skin-bind bones
                    # still at Blender-metre scale — confirmed via direct NIF
                    # inspection: a mismatch this severe reads in NifSkope/in-game
                    # as an essentially-invisible, comically tiny mesh. Scaled via
                    # edit-bone data directly (see _scale_armature_bones) rather
                    # than the object's own .scale, since the mesh is normally
                    # parented to this same armature and object-level scaling
                    # double-applies through that relationship.
                    _skel_early = ExportHelpers._find_skinned_armature(obj)
                    if _skel_early is not None:
                        ExportHelpers._scale_armature_bones(_skel_early, fo4_scale)
                        restore_armature_scales.append((_skel_early, fo4_scale))

                    print(f"[FO4Export][ScaleDiag] _scale_targets = {[t.name for t in _scale_targets]} fo4_scale={fo4_scale}")
                    # ROOT CAUSE (confirmed via direct console instrumentation on a
                    # real asset): the UCX_ collision child is PARENTED to obj
                    # (add_custom_collision()/add_collision_mesh() do this so the
                    # two travel together). bpy.ops.object.transform_apply() on a
                    # PARENT automatically compensates every PARENTED CHILD's own
                    # .scale by the same factor being removed from the parent, so
                    # the child's WORLD size doesn't visibly jump when the parent's
                    # scale resets to 1 -- this is normal, sanctioned Blender
                    # behaviour, not a bug in Blender. But reading `o.scale` fresh
                    # at the top of EACH loop iteration means that by the time this
                    # loop reaches the collision object (processed after obj), its
                    # .scale has ALREADY been silently multiplied by fo4_scale as
                    # Blender's own compensation for obj's transform_apply a few
                    # lines above -- confirmed directly: a freshly-created collision
                    # (scale=1.0 at creation) read orig_scale=69.99 here, purely
                    # from processing obj first, before this loop ever touched the
                    # collision object at all. This code then multiplied that
                    # already-inflated 69.99 by fo4_scale AGAIN (~4899x total),
                    # which is exactly the ~70x-too-large collision bug that no
                    # restore-step fix could ever have corrected, since the
                    # over-scaling happens here, at UP time, not at restore time.
                    # Fix: snapshot every target's TRUE starting scale up front,
                    # before ANY of them (parent included) are touched, so a
                    # child's later Blender-compensated scale is never mistaken
                    # for its real original value.
                    _orig_scales = {o.name: tuple(o.scale) for o in _scale_targets}
                    for o in _scale_targets:
                        orig_scale = _orig_scales[o.name]
                        o.scale = (
                            orig_scale[0] * fo4_scale,
                            orig_scale[1] * fo4_scale,
                            orig_scale[2] * fo4_scale,
                        )
                        bpy.context.view_layer.objects.active = o
                        bpy.ops.object.select_all(action='DESELECT')
                        o.select_set(True)
                        print(f"[FO4Export][ScaleDiag] UP  o={o.name} orig_scale={orig_scale} "
                              f"data_users_before={o.data.users if o.data else None} "
                              f"hidden={o.hide_get()} in_view_layer={o.name in bpy.context.view_layer.objects}")
                        # Make the mesh single-user so transform_apply succeeds
                        # even when the object is an Alt-D linked duplicate.
                        try:
                            if o.data and o.data.users > 1:
                                o.data = o.data.copy()
                        except Exception:
                            pass
                        # bpy.ops.object.transform_apply() communicates failure
                        # through a Blender operator report + a non-FINISHED
                        # return value, NOT a Python exception -- a bare
                        # try/except around the call alone treats "the
                        # operator silently did nothing" the same as "it
                        # succeeded". Confirmed directly on a real multi-piece
                        # armor rig: this exact call left o.scale sitting at
                        # ×fo4_scale (~70) with no exception raised, and
                        # validate_before_export() below (which runs before
                        # the restore-in-finally undoes this ×70) then
                        # correctly, but confusingly, rejected the mesh for
                        # "scale not applied" -- the export-time ×70 bake
                        # itself had silently failed, not anything upstream.
                        applied = False
                        try:
                            _sc_result = bpy.ops.object.transform_apply(
                                location=False, rotation=False, scale=True
                            )
                            applied = 'FINISHED' in _sc_result and tuple(o.scale) == (1.0, 1.0, 1.0)
                        except Exception:
                            applied = False
                        print(f"[FO4Export][ScaleDiag] UP  o={o.name} applied={applied} scale_after={tuple(o.scale)} dims_after={tuple(o.dimensions)}")
                        if not applied:
                            # Fallback: bake scale directly into mesh vertices.
                            try:
                                import mathutils as _mu
                                _sc = o.scale
                                _mat = _mu.Matrix.Diagonal(
                                    (_sc.x, _sc.y, _sc.z, 1.0)
                                ).to_4x4()
                                o.data.transform(_mat)
                                o.scale = (1.0, 1.0, 1.0)
                                applied = True
                            except Exception as _ue_exc:
                                print(f"[FO4Export][ScaleDiag] UP  o={o.name} FALLBACK FAILED: {_ue_exc}")
                                o.scale = orig_scale  # undo the ×70 if all else fails
                            else:
                                print(f"[FO4Export][ScaleDiag] UP  o={o.name} fallback bake used, scale_after={tuple(o.scale)} dims_after={tuple(o.dimensions)}")
                        if applied:
                            restore_scale_objs.append((o, orig_scale))
                    print(f"[FO4Export][ScaleDiag] restore_scale_objs (after UP) = {[t[0].name for t in restore_scale_objs]}")

                # Auto-prepare FIRST (applies transforms, creates UV map, triangulates).
                # Validation runs afterwards so it sees the corrected state and does not
                # block on issues that the prep step has already resolved.
                added_mods = ExportHelpers._prepare_mesh_for_nif(obj)

                # Validate after prep – only hard errors (poly limit, non-manifold,
                # missing materials) will stop the export at this point.
                success, issues = ExportHelpers.validate_before_export(obj)
                if not success:
                    return False, f"Validation failed: {', '.join(issues)}"

                # gather objects to export (main mesh + optional collision)
                selection = [obj]
                # only include collision object(s) if the mesh is expected to have one --
                # ALL pieces of a multi-convex collision, not just the first
                ctype = getattr(obj, 'fo4_collision_type', 'DEFAULT')
                if ctype not in ('NONE', 'GRASS', 'MUSHROOM'):
                    selection.extend(ExportHelpers._find_collision_meshes(obj))
                # include any weapon-mod/attachment connect points the user
                # has built under this object -- PyNifly only finds these by
                # scanning the export-time selection, so they'd be silently
                # dropped otherwise.
                selection.extend(ExportHelpers._find_connect_points(obj))

                bpy.ops.object.select_all(action='DESELECT')
                for o in selection:
                    o.select_set(True)
                bpy.context.view_layer.objects.active = obj

                # Dispatch to the appropriate exporter.
                import time as _time_mod
                _export_start = _time_mod.time()
                # PyNifly reads each shape's Emissive Color/Multiple straight from
                # whichever node feeds Material Output (usually the outer Principled
                # BSDF), not from the "Fallout 4 MTS" group's own inputs -- see
                # _snapshot_and_fix_emissive_for_export for the full explanation.
                _emissive_restore = (
                    ExportHelpers._snapshot_and_fix_emissive_for_export(selection)
                    if exporter == "pynifly" else [])
                try:
                    if exporter == "pynifly":
                        kwargs = ExportHelpers._build_pynifly_export_kwargs(filepath)
                        result = ExportHelpers._call_nif_export(
                            bpy.ops.export_scene.pynifly, kwargs,
                            active_object=obj, selected_objects=selection,
                        )
                        exporter_label = "PyNifly"
                    else:
                        kwargs = ExportHelpers._build_nif_export_kwargs(filepath)
                        result = ExportHelpers._call_nif_export(
                            bpy.ops.export_scene.nif, kwargs,
                            active_object=obj, selected_objects=selection,
                        )
                        exporter_label = "Niftools v0.1.1"
                finally:
                    ExportHelpers._restore_emissive_after_export(_emissive_restore)

                # PyNifly returns an empty set() on SUCCESS — it never adds
                # 'FINISHED' (see ExportNIF.execute:
                # `return res.intersection({'CANCELLED','FINISHED'})`, where res
                # only ever gains 'CANCELLED', on failure). But a non-CANCELLED
                # result is not proof the file was actually written — confirmed
                # directly on a real multi-piece armor rig, calling this in a
                # headless (-b) session via the active_object/selected_objects
                # context-override fallback (no real window/area available):
                # the operator returned a non-CANCELLED result and this code
                # reported success, yet no .nif ever appeared on disk. Always
                # verify the file actually landed rather than trusting a
                # merely-non-CANCELLED result. after_time=_export_start makes
                # this exact — otherwise a PREVIOUS piece's .nif written
                # moments earlier in the same output folder (e.g. exporting
                # a multi-piece armor set one after another) can be mistaken
                # for this call's own output purely for being "recent".
                _out_ok = ExportHelpers._export_output_written(filepath, after_time=_export_start)

                if _out_ok:
                    ctype = getattr(obj, 'fo4_collision_type', 'DEFAULT')
                    sound = obj.get("fo4_collision_sound")
                    weight = obj.get("fo4_collision_weight")
                    extras = []
                    if ctype:
                        extras.append(f"type={ctype}")
                    if sound:
                        extras.append(f"sound={sound}")
                    if weight:
                        extras.append(f"weight={weight}")
                    note = " (" + ", ".join(extras) + ")" if extras else ""
                    # Send event to desktop tutorial server
                    try:
                        from . import desktop_tutorial_client
                        event_data = {
                            'mesh_name': obj.name,
                            'filepath': filepath,
                            'extras': extras,
                            'exporter': exporter_label,
                        }
                        desktop_tutorial_client.DesktopTutorialClient.send_event('mesh_exported', event_data)
                    except Exception as e:
                        print(f"[DesktopTutorialClient] Failed to send mesh export event: {e}")

                    # ── Auto-export BGSM alongside the NIF ───────────────────
                    # The NIF references texture paths but FO4 also needs the
                    # .bgsm material file to correctly apply alpha, smoothness,
                    # and specular settings.  Derive the Materials folder from
                    # the NIF path: if the path contains a 'Meshes' segment
                    # replace it with 'Materials'; otherwise use a 'Materials'
                    # subfolder next to the NIF.
                    _bgsm_note = ""
                    try:
                        from . import bgsm_helpers as _bh_exp
                        if _bh_exp and obj.data.materials:
                            import pathlib as _pl
                            _nif_p = _pl.Path(filepath).resolve()
                            _parts = _nif_p.parts
                            _mesh_idx = next(
                                (i for i, p in enumerate(_parts)
                                 if p.lower() == 'meshes'),
                                None
                            )
                            # NOTE: LOD NIFs are exported by fo4_lod_generator.py
                            # to a literal ".../Meshes/LOD/<sub>/..." path (see
                            # its own comment on nif_path), so this plain
                            # Meshes->Materials mirror naturally carries that
                            # "LOD" segment through to
                            # ".../Materials/LOD/<sub>/..." with no special
                            # casing needed here -- mirroring the same
                            # Textures\LOD\... convention used for LOD
                            # textures (see _lod_texture_output_dir in
                            # fo4_lod_generator.py). Do NOT add an extra "LOD"
                            # segment here or LOD assets end up double-nested
                            # (Materials\LOD\LOD\...).
                            if _mesh_idx is not None:
                                # Mirror: Meshes\sub\path → Materials\sub\path
                                _mat_dir = _pl.Path(*_parts[:_mesh_idx]) / "Materials" / _pl.Path(*_parts[_mesh_idx + 1:-1])
                            else:
                                _mat_dir = _nif_p.parent / "Materials"
                            # fo4_lod_generator.py tags every LOD/billboard
                            # copy it creates with fo4_lod_level (LOD1/2/3 or
                            # BILLBOARD) -- reuse that same signal here so
                            # the exported BGSM gets the real LOD-specific
                            # overrides (see blender_mat_to_bgsm's is_lod
                            # docstring) instead of blindly carrying over
                            # the full-detail source material's fields.
                            _is_lod_export = bool(obj.get("fo4_lod_level"))
                            # Prefer the real materials\\...\\X.bgsm path baked
                            # into the NIF's own shader over guessing a name from
                            # the Blender material -- see bgsm_targets_from_
                            # exported_nif's docstring for why the guess can be
                            # wrong even when the folder mirroring is right.
                            _real_targets = _bh_exp.bgsm_targets_from_exported_nif(filepath, [obj])
                            if _real_targets:
                                _wm0, _bgsm_path0, _ = _real_targets[0]
                                _ok0, _msg0 = _bh_exp.export_bgsm_to_path(
                                    _wm0, str(_bgsm_path0), is_lod=_is_lod_export)
                                _bgsm_ok = [_msg0] if _ok0 else []
                                _bgsm_fail = [] if _ok0 else [_msg0]
                                if _bgsm_ok:
                                    _bgsm_note = f" + BGSM → {_bgsm_path0.parent.name}/"
                            else:
                                _bgsm_results = _bh_exp.export_bgsm_for_object(
                                    obj, str(_mat_dir), all_slots=False,
                                    is_lod=_is_lod_export,
                                )
                                _bgsm_ok = [m for ok, m in _bgsm_results if ok]
                                _bgsm_fail = [m for ok, m in _bgsm_results if not ok]
                                if _bgsm_ok:
                                    _bgsm_note = f" + BGSM → {_mat_dir.name}/"
                            if _bgsm_fail:
                                for _fm in _bgsm_fail:
                                    print(f"[FO4Export] BGSM warning: {_fm}")
                    except Exception as _be:
                        print(f"[FO4Export] BGSM auto-export skipped: {_be}")

                    # ── Auto-export textures alongside the NIF ───────────────
                    # A material can reference a texture that's still sitting
                    # wherever it was originally loaded from (a Downloads
                    # folder, source-art path, etc.) if it never went through
                    # install_texture's Data-folder copy. Ship the actual
                    # texture files with the export too, mirroring the same
                    # Meshes -> Textures folder convention already used for
                    # BGSM above, so exporting an asset produces everything
                    # FO4 needs in one step.
                    _tex_note = ""
                    try:
                        from . import bgsm_helpers as _bh_tex
                        if _bh_tex and obj.data.materials:
                            import pathlib as _pl
                            # Prefer the same NIF-derived target the BGSM
                            # block above just used, so textures always land
                            # next to the BGSM that references them instead
                            # of being computed independently from a
                            # possibly-intermediate `filepath` value (which
                            # can produce a doubled/incorrect subpath -- see
                            # bgsm_targets_from_exported_nif's docstring).
                            _real_targets_tex = _bh_tex.bgsm_targets_from_exported_nif(filepath, [obj])
                            if _real_targets_tex:
                                _tex_dir = _real_targets_tex[0][2]
                            else:
                                _nif_p2 = _pl.Path(filepath).resolve()
                                _parts2 = _nif_p2.parts
                                _mesh_idx2 = next(
                                    (i for i, p in enumerate(_parts2)
                                     if p.lower() == 'meshes'),
                                    None
                                )
                                if _mesh_idx2 is not None:
                                    _tex_dir = _pl.Path(*_parts2[:_mesh_idx2]) / "Textures" / _pl.Path(*_parts2[_mesh_idx2 + 1:-1])
                                else:
                                    _tex_dir = _nif_p2.parent / "Textures"
                            _tex_results = _bh_tex.export_textures_for_object(
                                obj, str(_tex_dir), all_slots=False
                            )
                            _tex_ok = [m for ok, m in _tex_results if ok]
                            _tex_fail = [m for ok, m in _tex_results if not ok]
                            if _tex_ok:
                                _tex_note = f" + textures → {_tex_dir.name}/"
                            if _tex_fail:
                                for _tm in _tex_fail:
                                    print(f"[FO4Export] Texture warning: {_tm}")
                    except Exception as _te:
                        print(f"[FO4Export] Texture auto-export skipped: {_te}")

                    # ── Restore original-NIF fields the Blender scene has no
                    # representation for (name, alpha property, BSXFlags) ──
                    _roundtrip_note = ""
                    try:
                        _source_nif = obj.get("fo4_source_nif")
                        if _source_nif and os.path.isfile(_source_nif):
                            from . import nif_roundtrip as _nrt
                            _rt_report = _nrt.patch_exported_nif(filepath, _source_nif, obj)
                            _rt_summary = _nrt.summarize(_rt_report)
                            if _rt_summary:
                                _roundtrip_note = f"  [{_rt_summary}]"
                            # Wind-foliage root wrapper (BSLeafAnimNode) --
                            # PyNifly's export API can't create/relabel this
                            # block type itself (see nif_roundtrip.
                            # patch_wind_root_node's docstring), so restore
                            # it post-export when the original had one or
                            # the object went through the Wind pipeline.
                            _leaf_report = _nrt.patch_wind_root_node(
                                filepath, obj, _source_nif
                            )
                            _leaf_summary = _nrt.summarize(_leaf_report)
                            if _leaf_summary:
                                _roundtrip_note += f"  [{_leaf_summary}]"
                        else:
                            # No original NIF to round-trip from (a freshly
                            # authored asset, not a reimport) -- still write
                            # the correct BSXFlags for mesh types where we
                            # have a verified real value (e.g. Furniture).
                            from . import nif_roundtrip as _nrt
                            # fo4_mesh_type is a real RNA EnumProperty
                            # (bpy.types.Object.fo4_mesh_type = EnumProperty(...)),
                            # set via attribute assignment (obj.fo4_mesh_type = ...)
                            # everywhere it's actually used (Create Vegetation,
                            # generate_lod_chain, etc.) -- NOT a custom ID
                            # property, so obj.get(...) here always silently
                            # returned "" regardless of the real value. This
                            # meant patch_fresh_bsxflags never actually fired for
                            # any real object, only for test scripts that
                            # coincidentally set it as a raw custom property.
                            _fresh_report = _nrt.patch_fresh_bsxflags(
                                filepath, getattr(obj, "fo4_mesh_type", "")
                            )
                            _fresh_summary = _nrt.summarize(_fresh_report)
                            if _fresh_summary:
                                _roundtrip_note = f"  [{_fresh_summary}]"
                            # Real weapon root-node naming (every vanilla
                            # weapon file uses the literal name "WEAPON",
                            # confirmed across 10mmPistol/CombatShotgun/
                            # GaussRifle/Cryolator/Flamer/AssaultRifleProto).
                            _root_report = _nrt.patch_root_name(
                                filepath, getattr(obj, "fo4_mesh_type", "")
                            )
                            _root_summary = _nrt.summarize(_root_report)
                            if _root_summary:
                                _roundtrip_note += f"  [{_root_summary}]"
                            # Wind-foliage root wrapper -- no source NIF here
                            # (fresh/no-reimport export), so this only fires
                            # off the object's own "Wind" vertex group.
                            _leaf_report = _nrt.patch_wind_root_node(filepath, obj, None)
                            _leaf_summary = _nrt.summarize(_leaf_report)
                            if _leaf_summary:
                                _roundtrip_note += f"  [{_leaf_summary}]"
                    except Exception as _rte:
                        print(f"[FO4Export] NIF round-trip patch skipped: {_rte}")

                    # ── Attach real Havok collision (PyNifly's own export-side
                    # attachment silently fails to persist when the target is
                    # a leaf mesh shape rather than a NiNode -- see
                    # nif_roundtrip.patch_native_collision for the full story)
                    try:
                        _coll_for_patch = ExportHelpers._find_collision_mesh(obj)
                        if _coll_for_patch is not None:
                            from . import nif_roundtrip as _nrt
                            _coll_report = _nrt.patch_native_collision(filepath, _coll_for_patch)
                            _coll_summary = _nrt.summarize(_coll_report)
                            if _coll_summary:
                                _roundtrip_note += f"  [{_coll_summary}]"
                    except Exception as _ce:
                        print(f"[FO4Export] Native collision patch skipped: {_ce}")

                    try:
                        ExportHelpers._write_kit_credits(obj, filepath)
                    except Exception as _cre:
                        print(f"[FO4Export] Kit-credit write skipped: {_cre}")

                    return True, f"Exported NIF via {exporter_label}: {filepath}{note}{_bgsm_note}{_tex_note}{_roundtrip_note}"

                # Operator returned without FINISHED — treat as failure
                return False, f"NIF export did not complete (result={result}). Check the Blender console for details."
            except Exception as e:
                print(f"[FO4 Add-on] NIF export error for '{obj.name}':", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                return False, f"NIF export failed: {e}"
            finally:
                for mod_name in added_mods:
                    mod = obj.modifiers.get(mod_name)
                    if mod:
                        obj.modifiers.remove(mod)
                # Restore objects to Blender-metre scale after export.
                # This must be a UNIFORM 1/fo4_scale, NOT orig_scale[i]*inv --
                # orig_scale's own (possibly non-uniform, possibly non-unity)
                # shape info is already permanently baked into the mesh DATA
                # by the earlier scale-up-then-transform_apply step above, so
                # re-multiplying by any component of orig_scale here double-
                # applies it. Verified empirically: for orig_scale=(2,3,4),
                # the old orig_scale[0]*inv code left the object 2x too big
                # in every axis after restore; uniform inv alone reproduces
                # the exact pre-export dimensions.
                inv = 1.0 / _FO4_UNIT_SCALE_INV if restore_scale_objs else None
                print(f"[FO4Export][ScaleDiag] restore_scale_objs (at RESTORE) = {[t[0].name for t in restore_scale_objs]} inv={inv}")
                for o, orig_scale in restore_scale_objs:
                    o.scale = (inv, inv, inv)
                    bpy.context.view_layer.objects.active = o
                    bpy.ops.object.select_all(action='DESELECT')
                    o.select_set(True)
                    print(f"[FO4Export][ScaleDiag] DOWN o={o.name} data_users_before={o.data.users if o.data else None} "
                          f"hidden={o.hide_get()} dims_before={tuple(o.dimensions)}")
                    # Confirmed real bug (matches a live user report): unlike
                    # the scale-UP step above, this restore never checked
                    # whether transform_apply actually succeeded.
                    # transform_apply communicates failure through an
                    # operator return value (or just doing nothing), NOT
                    # always a Python exception -- a hidden/unselectable
                    # collision object (a very normal state for a collision
                    # mesh, which this add-on itself sets to WIRE display and
                    # hide_render) is exactly the kind of case where the
                    # operator can silently no-op. When that happens here,
                    # o.scale is left sitting at (inv, inv, inv) -- roughly
                    # 1/70 -- while the mesh DATA is still baked at the ×70
                    # FO4-scale size from the pre-export step, so the object
                    # never visibly shrinks back down: it reopens/redisplays
                    # at full Fallout-4 scale in Blender even though nothing
                    # is "wrong" with the file on disk. Add the same
                    # verify-and-fall-back-to-baking-the-matrix-directly
                    # safety net the scale-up step already has, instead of a
                    # bare try/except that treats silent failure as success.
                    #
                    # SECOND confirmed bug found by direct N-panel evidence
                    # (Scale reading 1.000 on a collision object whose
                    # Dimensions were still ~70x too big): transform_apply()
                    # on a MULTI-USER mesh datablock resets the calling
                    # object's own .scale to (1,1,1) and still reports
                    # 'FINISHED', but silently skips baking the transform
                    # into the shared mesh data (to avoid corrupting other
                    # users of that data). That made the `applied` check
                    # above (FINISHED + scale==1,1,1) pass even though the
                    # vertices were never actually shrunk back down. The
                    # scale-UP step already guards against exactly this by
                    # forcing single-user data BEFORE calling
                    # transform_apply(); this restore step only did that
                    # inside its fallback branch, never before the primary
                    # attempt. Force single-user here too, matching scale-UP.
                    try:
                        if o.data and o.data.users > 1:
                            o.data = o.data.copy()
                    except Exception:
                        pass
                    applied = False
                    try:
                        _restore_result = bpy.ops.object.transform_apply(
                            location=False, rotation=False, scale=True
                        )
                        applied = ('FINISHED' in _restore_result
                                   and tuple(o.scale) == (1.0, 1.0, 1.0))
                    except Exception as _re_exc:
                        print(f"[FO4Export][ScaleDiag] DOWN o={o.name} transform_apply EXCEPTION: {_re_exc}")
                        applied = False
                    print(f"[FO4Export][ScaleDiag] DOWN o={o.name} primary_applied={applied} "
                          f"scale_after_primary={tuple(o.scale)} dims_after_primary={tuple(o.dimensions)}")
                    if not applied:
                        try:
                            import mathutils as _mu_restore
                            _sc = o.scale
                            _mat = _mu_restore.Matrix.Diagonal(
                                (_sc.x, _sc.y, _sc.z, 1.0)
                            ).to_4x4()
                            if o.data and o.data.users > 1:
                                o.data = o.data.copy()
                            o.data.transform(_mat)
                            o.data.update()
                            o.scale = (1.0, 1.0, 1.0)
                            print(f"[FO4Export][ScaleDiag] DOWN o={o.name} fallback bake DONE, "
                                  f"scale_after={tuple(o.scale)} dims_after={tuple(o.dimensions)}")
                        except Exception as _fb_exc:
                            print(f"[FO4Export][ScaleDiag] DOWN o={o.name} FALLBACK FAILED: {_fb_exc}")
                # Restore the author-only wind rig removed before export.
                ExportHelpers._reattach_wind_authoring_rig(obj, wind_token)
                # Restore the author-only weapon animation rig removed before export.
                ExportHelpers._reattach_weapon_authoring_rig(obj, weapon_token)
                # Restore any skinned armature's bind-pose bones back down.
                for _arm, _factor in restore_armature_scales:
                    try:
                        ExportHelpers._scale_armature_bones(_arm, 1.0 / _factor)
                    except Exception:
                        pass
        else:
            return False, f"PyNifly not found. {nif_message} — install PyNifly to export NIF files."
    
    @staticmethod
    def _has_armature(obj):
        """Return True if *obj* is skinned to an armature (parent or modifier).
        """
        if obj.parent and obj.parent.type == 'ARMATURE':
            return True
        for mod in getattr(obj, 'modifiers', []):
            if mod.type == 'ARMATURE':
                return True
        return False

    @staticmethod
    def _is_wind_authoring_armature(arm_obj):
        """Return True when *arm_obj* is the author-only procedural-wind rig
        created by ``AnimationHelpers.apply_wind_animation`` (a single "Wind"
        bone), NOT a real FO4 skeleton.

        FO4 vegetation wind is procedural in-engine and driven by the mesh's
        "Wind" vertex group — the rig exists only for in-Blender preview.  If it
        is left attached at export, PyNifly skins the mesh to the bogus "Wind"
        bone, which corrupts the geometry (edges-only on re-import, magenta /
        missing texture in-game).  A genuine skeleton has many bones and must
        never be treated this way.
        """
        if not arm_obj or getattr(arm_obj, 'type', None) != 'ARMATURE':
            return False
        try:
            bone_names = {b.name for b in arm_obj.data.bones}
        except Exception:
            return False
        if 'Wind' not in bone_names:
            return False
        # Authoring rig = just the Wind bone (optionally a root).  Real FO4
        # skeletons have dozens of bones; never strip those.
        return len(bone_names) <= 2

    @staticmethod
    def _is_weapon_authoring_armature(arm_obj):
        """Return True when *arm_obj* is the author-only animation rig built
        by ``fo4_weapon_animation.build_weapon_rig`` for previewing/authoring
        fire/reload/melee poses in Blender before conversion to HKX, NOT a
        real skeleton.

        Real FO4 weapons are never skinned (``has_skin_instance=0`` on every
        shape, verified against real 10mmPistol.nif/CombatShotgun.nif) --
        moving parts are separate rigid nodes animated by their own keyframed
        transforms, not vertex weights. If this rig is left attached at NIF
        export, PyNifly would skin the mesh to it, producing an
        incorrectly-skinned weapon NIF that doesn't match how FO4 actually
        loads weapons.
        """
        if not arm_obj or getattr(arm_obj, 'type', None) != 'ARMATURE':
            return False
        return bool(arm_obj.get("fo4_weapon_type"))

    @staticmethod
    def _detach_weapon_authoring_rig(obj):
        """Temporarily remove the author-only weapon animation rig (modifier
        and/or parent) so the mesh exports as the rigid, unskinned geometry
        real weapon NIFs actually use.

        Returns a restore token for :func:`_reattach_weapon_authoring_rig`,
        or ``None`` when nothing was detached.
        """
        token = {'mods': [], 'parent': None, 'matrix': None}
        detached = False

        for mod in list(getattr(obj, 'modifiers', [])):
            if mod.type == 'ARMATURE' and ExportHelpers._is_weapon_authoring_armature(
                    getattr(mod, 'object', None)):
                token['mods'].append({
                    'name': mod.name,
                    'object': mod.object,
                    'use_vertex_groups': getattr(mod, 'use_vertex_groups', True),
                    'use_bone_envelopes': getattr(mod, 'use_bone_envelopes', False),
                })
                try:
                    obj.modifiers.remove(mod)
                    detached = True
                except Exception:
                    pass

        if obj.parent and ExportHelpers._is_weapon_authoring_armature(obj.parent):
            token['parent'] = obj.parent
            try:
                token['matrix'] = obj.matrix_world.copy()
                obj.parent = None
                obj.matrix_world = token['matrix']
                detached = True
            except Exception:
                token['parent'] = None

        return token if detached else None

    @staticmethod
    def _reattach_weapon_authoring_rig(obj, token):
        """Restore the author-only weapon rig removed by
        :func:`_detach_weapon_authoring_rig` so the user's scene is unchanged
        after export.
        """
        if not token:
            return
        parent = token.get('parent')
        if parent is not None:
            try:
                obj.parent = parent
                if token.get('matrix') is not None:
                    obj.matrix_world = token['matrix']
            except Exception:
                pass
        for info in token.get('mods', []):
            try:
                if info['name'] in obj.modifiers:
                    continue
                m = obj.modifiers.new(name=info['name'], type='ARMATURE')
                m.object = info['object']
                m.use_vertex_groups = info.get('use_vertex_groups', True)
                m.use_bone_envelopes = info.get('use_bone_envelopes', False)
            except Exception:
                pass

    @staticmethod
    def _detach_wind_authoring_rig(obj):
        """Temporarily remove the author-only wind armature (modifier and/or
        parent) so the mesh exports as a static vegetation mesh carrying only
        its Wind vertex group.

        Returns a restore token for :func:`_reattach_wind_authoring_rig`, or
        ``None`` when nothing was detached.
        """
        token = {'mods': [], 'parent': None, 'matrix': None}
        detached = False

        # 1. Remove ARMATURE modifiers that point at an authoring wind rig.
        for mod in list(getattr(obj, 'modifiers', [])):
            if mod.type == 'ARMATURE' and ExportHelpers._is_wind_authoring_armature(
                    getattr(mod, 'object', None)):
                token['mods'].append({
                    'name': mod.name,
                    'object': mod.object,
                    'use_vertex_groups': getattr(mod, 'use_vertex_groups', True),
                    'use_bone_envelopes': getattr(mod, 'use_bone_envelopes', False),
                })
                try:
                    obj.modifiers.remove(mod)
                    detached = True
                except Exception:
                    pass

        # 2. Clear an authoring-wind parent, preserving world transform.
        if obj.parent and ExportHelpers._is_wind_authoring_armature(obj.parent):
            token['parent'] = obj.parent
            try:
                token['matrix'] = obj.matrix_world.copy()
                obj.parent = None
                obj.matrix_world = token['matrix']
                detached = True
            except Exception:
                token['parent'] = None

        return token if detached else None

    @staticmethod
    def _reattach_wind_authoring_rig(obj, token):
        """Restore the author-only wind armature removed by
        :func:`_detach_wind_authoring_rig` so the user's scene is unchanged
        after export.
        """
        if not token:
            return
        parent = token.get('parent')
        if parent is not None:
            try:
                obj.parent = parent
                if token.get('matrix') is not None:
                    obj.matrix_world = token['matrix']
            except Exception:
                pass
        for info in token.get('mods', []):
            try:
                if info['name'] in obj.modifiers:
                    continue
                m = obj.modifiers.new(name=info['name'], type='ARMATURE')
                m.object = info['object']
                m.use_vertex_groups = info.get('use_vertex_groups', True)
                m.use_bone_envelopes = info.get('use_bone_envelopes', False)
            except Exception:
                pass

    @staticmethod
    def safe_export_filename(name: str) -> str:
        """Sanitize an object name for use as a filesystem filename.

        PyNifly's own shape-naming convention embeds a literal colon (e.g.
        "BaseMaleBody_Fitted:0", "Body:0") -- a real, common case on
        ordinary vanilla FO4 NIFs, not an edge case. On NTFS a colon in a
        path is the Alternate Data Stream separator, so an unsanitized
        f"{name}.nif" silently writes into a hidden ADS on a 0-byte visible
        file instead of a real, discoverable .nif. Confirmed directly on a
        real export: PyNifly reported "successfully exported" and the
        calling code reported "Exported N meshes" while every affected
        file was an empty 0-byte placeholder with the real NIF data hidden
        in an invisible alternate stream -- a fabricated-success shape
        indistinguishable from success unless someone checks with
        Get-Item -Stream. Replaces every character illegal in a Windows
        filename with '_'.
        """
        illegal = '<>:"/\\|?*'
        cleaned = "".join('_' if c in illegal else c for c in name).strip()
        return cleaned or "unnamed"

    @staticmethod
    def _export_output_written(filepath, since_seconds: float = 300.0, after_time: float = None,
                               retry_window: float = 2.0, retry_interval: float = 0.2):
        """Return True when a NIF that looks like this export was written to disk
        within *since_seconds* (or, if *after_time* is given, strictly after
        that timestamp — see below).

        PyNifly V27 frequently prints "Export successful" yet returns an empty
        ``set()`` from ``bpy.ops`` instead of ``{'FINISHED'}`` — so the operator
        return code cannot be trusted.  The file on disk is the real arbiter.

        Checks (path/extension tolerant):
          1. the exact filepath and the filepath with a ``.nif`` extension,
          2. any ``*.nif`` in the target directory sharing the filepath's stem,
          3. as a last resort, the most-recently modified ``*.nif`` in that
             directory.

        *after_time* should be a timestamp captured immediately before the
        export operator was invoked (e.g. ``time.time()``). Without it, check
        3 only requires a file to be "fresh" relative to *since_seconds*
        (a fixed 300 s window) — confirmed directly to false-positive when
        exporting several pieces to the same output folder in a short span
        (e.g. an armor set with multiple pieces): the second piece's failed
        export was reported as successful purely because the FIRST piece's
        .nif, written moments earlier, was still within that window and
        happened to be the most recently modified .nif in the directory.
        Passing *after_time* makes check 3 exact — a file from an earlier,
        unrelated export call can never be mistaken for this one's output,
        regardless of how "fresh" it superficially looks.

        *retry_window*/*retry_interval*: on a real multi-piece scene export
        (e.g. an architecture asset with a NiSwitchNode LOD hierarchy),
        PyNifly's actual disk write can land a beat after the ``bpy.ops``
        call returns -- confirmed directly: an identical export re-run
        immediately afterward succeeded on the first try, meaning the
        original attempt's file genuinely wasn't on disk yet at check time,
        not that the export itself had failed. A same-process retry loop
        would have caught it. Without this, that timing gap silently
        degrades a real NIF export into an FBX fallback. Polls up to
        *retry_window* seconds (default 2s) before giving up -- negligible
        cost on the common instant-success path (returns on the first
        check), bounded cost on a genuine failure.
        """
        import time as _t

        def _check_once(floor_time) -> bool:
            def _fresh(p):
                try:
                    return os.path.exists(p) and os.path.getmtime(p) > floor_time
                except Exception:
                    return False

            base, ext = os.path.splitext(filepath)
            candidates = [filepath]
            if ext.lower() != ".nif":
                candidates.append(filepath + ".nif")
                candidates.append(base + ".nif")
            for c in candidates:
                if _fresh(c):
                    return True

            try:
                target_dir = os.path.dirname(filepath) or "."
                stem = os.path.splitext(os.path.basename(filepath))[0].lower()
                newest = None
                for name in os.listdir(target_dir):
                    if not name.lower().endswith(".nif"):
                        continue
                    full = os.path.join(target_dir, name)
                    if not _fresh(full):
                        continue
                    if os.path.splitext(name)[0].lower() == stem:
                        return True
                    try:
                        mt = os.path.getmtime(full)
                    except Exception:
                        continue
                    if newest is None or mt > newest[1]:
                        newest = (full, mt)
                if newest is not None:
                    return True
            except Exception:
                pass
            return False

        now = _t.time()
        floor_time = after_time if after_time is not None else (now - since_seconds)

        deadline = now + max(retry_window, 0.0)
        while True:
            if _check_once(floor_time):
                return True
            if _t.time() >= deadline:
                return False
            _t.sleep(retry_interval)

    @staticmethod
    def _call_nif_export(op_callable, kwargs, active_object=None, selected_objects=None):
        """Invoke a NIF export operator (``bpy.ops.export_scene.pynifly`` /
        ``.nif``) in a context equivalent to a normal ``File > Export``.

        Our export operators use ``fileselect_add()``, so their ``execute()``
        runs inside a FILE_BROWSER modal context.  Calling PyNifly from there
        makes PyNifly's own ``execute()`` reference an incompatible context
        (``space_data`` / ``area``), which raises *inside* the operator —
        Blender swallows the exception, prints the traceback to the system
        console, and ``bpy.ops`` returns an empty ``set()`` with no file
        written.  That is the "Export successful … result=set()" symptom.

        Overriding to the main window's 3D viewport reproduces the working
        manual-export context.  When no viewport is available at all (e.g. a
        headless ``-b`` session, which never has any window/screen/area),
        falls back to overriding just ``active_object``/``selected_objects``
        instead of a completely bare call — confirmed directly that a bare
        call can fail with "Operator ... .poll() failed, context is
        incorrect" in that scenario even though the object/selection state
        set up by the caller is otherwise entirely correct.
        """
        win = area = region = None
        try:
            for w in bpy.context.window_manager.windows:
                scr = getattr(w, 'screen', None)
                if not scr:
                    continue
                for a in scr.areas:
                    if a.type == 'VIEW_3D':
                        win, area = w, a
                        region = next((r for r in a.regions if r.type == 'WINDOW'), None)
                        break
                if area:
                    break
        except Exception:
            pass

        if win and area:
            try:
                with bpy.context.temp_override(window=win, area=area, region=region):
                    return op_callable(**kwargs)
            except Exception:
                # temp_override unsupported (very old Blender) or rejected —
                # fall through to a direct call so export is still attempted.
                pass

        if active_object is not None:
            try:
                override = {"active_object": active_object}
                if selected_objects is not None:
                    override["selected_objects"] = override["selected_editable_objects"] = selected_objects
                with bpy.context.temp_override(**override):
                    return op_callable(**kwargs)
            except Exception:
                pass
        return op_callable(**kwargs)

    @staticmethod
    def export_mesh_with_collision(obj, filepath, simplify_ratio: float = 0.25):
        """Helper to generate a collision mesh and then export the pair to NIF.

        This mirrors the behaviour of the ``fo4.export_mesh_with_collision`` operator
        by creating a new collision mesh (or replacing an existing one) then calling
        :func:`export_mesh_to_nif` on the original object.

        Parameters
        ----------
        obj : bpy.types.Object
            Source mesh object
        filepath : str
            Destination NIF file path
        simplify_ratio : float, optional
            Simplification ratio for the collision mesh, by default 0.25
        """
        from . import mesh_helpers

        # ensure source object is ok
        if obj.type != 'MESH':
            return False, "Object is not a mesh"

        # generate or update collision mesh
        collision = mesh_helpers.MeshHelpers.add_collision_mesh(obj, simplify_ratio=simplify_ratio)
        if not collision:
            return False, "Failed to create collision mesh"

        # now export both
        return ExportHelpers.export_mesh_to_nif(obj, filepath)

    @staticmethod
    def export_scene_as_single_nif(scene, filepath, objects=None):
        """Export scene meshes and their collision meshes as one NIF file.

        This implements the intended workflow: import a NIF, add collision to the
        meshes that need it, then export them back out as a single NIF that is
        ready to go straight into the game.

        Each mesh object (that is not itself a collision proxy) is included
        in the export.  For each mesh, any associated collision mesh (identified by
        the UCX_ prefix, the fo4_collision flag on a parented child, or the
        legacy _COLLISION suffix) is also selected so that it travels along in the
        same NIF node hierarchy.

        Exporter priority: PyNifly > Niftools v0.1.1 > FBX fallback.

        Parameters
        ----------
        scene : bpy.types.Scene
            The Blender scene to export.
        filepath : str
            Destination ``.nif`` file path (or ``.fbx`` for the FBX fallback).
        objects : list[bpy.types.Object] or None
            If given, combine exactly these objects (plus each one's own
            collision mesh) instead of every mesh in the scene. Pass the
            current selection so a user can pick a specific subset (e.g. the
            handful of shapes that originally came from one multi-shape NIF)
            without unrelated scene content — other LOD copies, unrelated
            props — getting pulled into the same file. Falls back to every
            mesh in the scene when None or empty, matching the prior
            behaviour.

        Returns
        -------
        tuple[bool, str]
            ``(True, message)`` on success, ``(False, error_message)`` on failure.
        """
        # Collect all exportable (non-collision) mesh objects, from the
        # explicit *objects* subset when given, else the whole scene.
        source_objs = objects if objects else scene.objects
        meshes = [
            obj for obj in source_objs
            if obj.type == 'MESH' and not ExportHelpers._is_collision_mesh(obj)
            and not obj.get("fo4_reference")
        ]

        if not meshes:
            return False, "No exportable meshes found in the scene" if not objects else (
                "None of the selected objects are exportable meshes "
                "(collision proxies are excluded automatically — select their source mesh instead)"
            )

        multi_obj_note = ""

        # Force a .nif extension so a name typed without one (e.g. "test") does
        # not produce an extensionless file that isn't recognised as a NIF.
        if filepath and str(filepath).strip():
            filepath = str(filepath).strip()
            _root, _ext = os.path.splitext(filepath)
            if _ext.lower() not in (".nif", ".fbx"):
                filepath = filepath + ".nif" if not _ext else _root + ".nif"

        exporter, nif_available, nif_message = ExportHelpers.get_nif_exporter_info()

        # Track temporary modifiers added during preparation so we can remove
        # them when we're done, regardless of whether export succeeds or fails.
        added_mods_per_obj = {}
        wind_tokens_per_obj = {}  # author-only wind rigs detached during export
        weapon_tokens_per_obj = {}  # author-only weapon animation rigs detached during export
        restore_scale_objs = []  # (obj, orig_scale) pairs — restored in finally
        restore_armature_scales = []  # (armature_obj, factor) pairs — restored in finally
        _scaled_armatures = set()  # armature objects already ×70'd this batch — avoid re-scaling a shared skeleton per sibling mesh
        try:
            bpy.ops.object.select_all(action='DESELECT')

            for obj in meshes:
                # ── Strip author-only procedural-wind rig ─────────────────────
                # Detach any preview-only "Wind" armature so PyNifly exports the
                # mesh as static vegetation instead of skinning it to a bogus
                # bone.  Restored in the finally block.
                wt = ExportHelpers._detach_wind_authoring_rig(obj)
                if wt:
                    wind_tokens_per_obj[obj.name] = wt

                # ── Strip author-only weapon animation rig ────────────────────
                # build_weapon_rig() parents a weapon mesh to a preview-only
                # animation-authoring armature. Real weapons are never skinned.
                weap_t = ExportHelpers._detach_weapon_authoring_rig(obj)
                if weap_t:
                    weapon_tokens_per_obj[obj.name] = weap_t

                # ── FO4 unit scale round-trip ─────────────────────────────────
                # Objects imported via our tools are stored at Blender-metre scale
                # (÷70) and tagged with fo4_unit_scale=69.99125.  Multiply back to
                # FO4 game units (×70) before exporting so PyNifly sees the correct
                # geometry, then restore in the finally block.
                fo4_scale = obj.get("fo4_unit_scale", None)
                if fo4_scale is None:
                    fo4_scale = ExportHelpers._infer_fo4_scale_for_untagged(obj)
                if fo4_scale:
                    # See the matching comment in export_mesh_to_nif: gate on
                    # fo4_scale (tag-or-inferred) rather than the collision
                    # object's own tag, or an untagged source's collision
                    # mesh silently gets excluded from the scale round-trip.
                    _scale_targets = [obj]
                    _scale_targets.extend(ExportHelpers._find_collision_meshes(obj))

                    # Move the skinned armature's bind-pose bones in lockstep —
                    # see the matching comment in export_mesh_to_nif for why
                    # this uses edit-bone data rather than the object's own
                    # .scale. Multiple sibling meshes in this batch
                    # (shirt/pants/etc.) commonly share one skeleton — only
                    # scale it once.
                    _skel_early = ExportHelpers._find_skinned_armature(obj)
                    if _skel_early is not None and _skel_early not in _scaled_armatures:
                        ExportHelpers._scale_armature_bones(_skel_early, fo4_scale)
                        restore_armature_scales.append((_skel_early, fo4_scale))
                        _scaled_armatures.add(_skel_early)

                    # See the matching fix + comment in export_mesh_to_nif: a
                    # parented UCX_ collision child's .scale gets silently
                    # multiplied by fo4_scale as Blender's own compensation
                    # the moment its PARENT (obj, processed earlier in this
                    # same loop) has transform_apply() run on it -- so re-
                    # reading o.scale fresh at the top of each iteration
                    # picks up that already-inflated value for the child and
                    # multiplies it by fo4_scale a second time. Snapshot every
                    # target's true starting scale up front instead.
                    _orig_scales = {o.name: tuple(o.scale) for o in _scale_targets}
                    for o in _scale_targets:
                        orig_scale = _orig_scales[o.name]
                        o.scale = (
                            orig_scale[0] * fo4_scale,
                            orig_scale[1] * fo4_scale,
                            orig_scale[2] * fo4_scale,
                        )
                        # Isolate `o` for transform_apply WITHOUT touching the
                        # selection state of meshes already processed earlier
                        # in this loop — select_all(DESELECT) here would wipe
                        # out every prior object's selection, leaving only the
                        # last-processed mesh selected by the time the final
                        # export call runs (this was the cause of "Export
                        # Entire Scene as NIF" silently dropping all but one
                        # shape).  Save/restore the pre-existing selection
                        # instead of clearing it.
                        _prev_selected = [
                            so for so in bpy.context.selected_objects if so is not o
                        ]
                        bpy.context.view_layer.objects.active = o
                        bpy.ops.object.select_all(action='DESELECT')
                        o.select_set(True)
                        try:
                            if o.data and o.data.users > 1:
                                o.data = o.data.copy()
                        except Exception:
                            pass
                        _applied = False
                        try:
                            bpy.ops.object.transform_apply(
                                location=False, rotation=False, scale=True
                            )
                            _applied = True
                        except Exception:
                            try:
                                import mathutils as _mu
                                _sc = o.scale
                                _mat = _mu.Matrix.Diagonal(
                                    (_sc.x, _sc.y, _sc.z, 1.0)
                                ).to_4x4()
                                o.data.transform(_mat)
                                o.scale = (1.0, 1.0, 1.0)
                                _applied = True
                            except Exception:
                                o.scale = orig_scale
                        if _applied:
                            restore_scale_objs.append((o, orig_scale))
                        # Restore the selection state from before this
                        # isolated transform_apply so earlier meshes in the
                        # outer loop stay selected for the final export call.
                        for so in _prev_selected:
                            so.select_set(True)

                # Prepare each mesh (apply transforms, UV, triangulate).
                added_mods = ExportHelpers._prepare_mesh_for_nif(obj)
                added_mods_per_obj[obj.name] = added_mods
                obj.select_set(True)

                # Include the associated collision mesh(es) so they are embedded
                # in the same NIF file rather than being left behind -- ALL
                # pieces of a multi-convex collision, not just the first.
                ctype = getattr(obj, 'fo4_collision_type', 'DEFAULT')
                if ctype not in ('NONE', 'GRASS', 'MUSHROOM'):
                    for _coll in ExportHelpers._find_collision_meshes(obj):
                        _coll.select_set(True)
                # Include any weapon-mod/attachment connect points built
                # under this object -- see _find_connect_points for why.
                for cp in ExportHelpers._find_connect_points(obj):
                    cp.select_set(True)

            # Final defensive re-selection: several per-object helpers called
            # above (fix_unweighted_vertices, transform_apply fallbacks, etc.)
            # call select_all(DESELECT) internally for their own isolation
            # needs.  Rather than audit every nested helper for selection side
            # effects, guarantee correctness here with one authoritative pass
            # right before the exporter reads bpy.context.selected_objects.
            bpy.ops.object.select_all(action='DESELECT')
            for obj in meshes:
                obj.select_set(True)
                ctype = getattr(obj, 'fo4_collision_type', 'DEFAULT')
                if ctype not in ('NONE', 'GRASS', 'MUSHROOM'):
                    for _coll in ExportHelpers._find_collision_meshes(obj):
                        _coll.select_set(True)
                for cp in ExportHelpers._find_connect_points(obj):
                    cp.select_set(True)

            # Set the first mesh as the active object so the exporter has a
            # valid context even when no object was explicitly activated.
            bpy.context.view_layer.objects.active = meshes[0]

            if nif_available:
                # Dispatch to the appropriate exporter.
                if exporter == "pynifly":
                    kwargs = ExportHelpers._build_pynifly_export_kwargs(filepath)
                    exporter_label = "PyNifly"
                    call = bpy.ops.export_scene.pynifly
                else:
                    kwargs = ExportHelpers._build_nif_export_kwargs(filepath)
                    exporter_label = "Niftools v0.1.1"
                    call = bpy.ops.export_scene.nif
                # See _snapshot_and_fix_emissive_for_export: PyNifly reads
                # Emissive Color/Multiple from whichever node feeds Material
                # Output, not from the "Fallout 4 MTS" group's own inputs.
                _emissive_restore = (
                    ExportHelpers._snapshot_and_fix_emissive_for_export(meshes)
                    if exporter == "pynifly" else [])
                try:
                    import time as _time_mod2
                    _export_start2 = _time_mod2.time()
                    result = ExportHelpers._call_nif_export(
                        call, kwargs,
                        active_object=meshes[0], selected_objects=list(bpy.context.selected_objects),
                    )
                    # PyNifly returns empty set() on success; only {'CANCELLED'}
                    # signals failure. But that's not proof the file was
                    # actually written (see export_mesh_to_nif's identical
                    # note) — always verify on disk rather than trusting a
                    # merely-non-CANCELLED result. after_time makes the check
                    # exact so an earlier export's .nif in the same folder
                    # can't be mistaken for this one's output.
                    _ok = ExportHelpers._export_output_written(filepath, after_time=_export_start2)
                    if _ok:
                        mesh_count = len(meshes)
                        _coll_note = ""
                        try:
                            # Attach real Havok collision the same way
                            # export_mesh_to_nif does (PyNifly's own
                            # export-side attachment doesn't persist when
                            # targeting a leaf shape -- see
                            # nif_roundtrip.patch_native_collision). Only
                            # handled when exactly one collision mesh is
                            # involved -- patch_native_collision writes a
                            # single root-level collision block, so a scene
                            # combining multiple independently-collided
                            # meshes isn't supported here.
                            _colls = {
                                id(c): c for m in meshes
                                if (c := ExportHelpers._find_collision_mesh(m)) is not None
                            }
                            if len(_colls) == 1:
                                from . import nif_roundtrip as _nrt
                                _coll_report = _nrt.patch_native_collision(
                                    filepath, next(iter(_colls.values())))
                                _coll_summary = _nrt.summarize(_coll_report)
                                if _coll_summary:
                                    _coll_note = f"  [{_coll_summary}]"
                        except Exception as _ce:
                            print(f"[FO4Export] Native collision patch skipped: {_ce}")
                        _bgsm_note = ""
                        try:
                            # export_scene_as_single_nif (this combine path) used to
                            # skip BGSM/texture export entirely -- only the single-
                            # object export path below (~line 1615) had it. Real-world
                            # symptom: every object routed through the recombine/batch
                            # path (i.e. everything fo4.batch_export touches) shipped
                            # with NO .bgsm file at all, so FO4/Creation Kit couldn't
                            # resolve the shape's material -- and CK doesn't just show
                            # a purple/missing-texture shape for that, it can flat-out
                            # crash (access violation) the moment you try to view the
                            # affected NIF. Mirror the same per-object BGSM + texture
                            # export used by the single-object path, once per mesh in
                            # this combined NIF.
                            from . import bgsm_helpers as _bh_exp2
                            import pathlib as _pl2
                            _nif_p3 = _pl2.Path(filepath).resolve()
                            _parts3 = _nif_p3.parts
                            _mesh_idx3 = next(
                                (i for i, p in enumerate(_parts3)
                                 if p.lower() == 'meshes'),
                                None
                            )
                            if _mesh_idx3 is not None:
                                _mat_dir3 = _pl2.Path(*_parts3[:_mesh_idx3]) / "Materials" / _pl2.Path(*_parts3[_mesh_idx3 + 1:-1])
                                _tex_dir3 = _pl2.Path(*_parts3[:_mesh_idx3]) / "Textures" / _pl2.Path(*_parts3[_mesh_idx3 + 1:-1])
                            else:
                                _mat_dir3 = _nif_p3.parent / "Materials"
                                _tex_dir3 = _nif_p3.parent / "Textures"
                            _bgsm_ok_any = False
                            _tex_ok_any = False
                            # Prefer the real materials\\...\\X.bgsm path(s)
                            # baked into the combined NIF's own shader
                            # properties -- see bgsm_targets_from_exported_nif's
                            # docstring: the old mat.name-based guess put files
                            # in the wrong folder AND under the wrong filename
                            # (e.g. "Flower01_0.Mat.bgsm" in a flat "Materials/"
                            # folder instead of "materials\\landscape\\plants\\
                            # flower01.bgsm", which is what the shader and CK
                            # actually look for).
                            _real_targets3 = _bh_exp2.bgsm_targets_from_exported_nif(filepath, meshes)
                            if _real_targets3:
                                for _wm2, _bgsm_path2, _tex_dir2 in _real_targets3:
                                    _is_lod_export2 = bool(_wm2.get("fo4_lod_level"))
                                    _ok2, _m2 = _bh_exp2.export_bgsm_to_path(
                                        _wm2, str(_bgsm_path2), is_lod=_is_lod_export2)
                                    if _ok2:
                                        _bgsm_ok_any = True
                                    else:
                                        print(f"[FO4Export] BGSM warning: {_m2}")
                                    for _ok2b, _m2b in _bh_exp2.export_textures_for_object(
                                        _wm2, str(_tex_dir2), all_slots=False
                                    ):
                                        if _ok2b:
                                            _tex_ok_any = True
                                        else:
                                            print(f"[FO4Export] Texture warning: {_m2b}")
                            else:
                                for _wm2 in meshes:
                                    if not _wm2.data.materials:
                                        continue
                                    _is_lod_export2 = bool(_wm2.get("fo4_lod_level"))
                                    for _ok2, _m2 in _bh_exp2.export_bgsm_for_object(
                                        _wm2, str(_mat_dir3), all_slots=False,
                                        is_lod=_is_lod_export2,
                                    ):
                                        if _ok2:
                                            _bgsm_ok_any = True
                                        else:
                                            print(f"[FO4Export] BGSM warning: {_m2}")
                                    for _ok2, _m2 in _bh_exp2.export_textures_for_object(
                                        _wm2, str(_tex_dir3), all_slots=False
                                    ):
                                        if _ok2:
                                            _tex_ok_any = True
                                        else:
                                            print(f"[FO4Export] Texture warning: {_m2}")
                            if _bgsm_ok_any:
                                _bgsm_note += f" + BGSM \u2192 {_mat_dir3.name}/"
                            if _tex_ok_any:
                                _bgsm_note += f" + textures \u2192 {_tex_dir3.name}/"
                        except Exception as _be2:
                            print(f"[FO4Export] BGSM/texture auto-export skipped: {_be2}")
                        _leaf_note = ""
                        try:
                            from . import nif_roundtrip as _nrt
                            for _wm in meshes:
                                _wm_source = _wm.get("fo4_source_nif")
                                _leaf_report = _nrt.patch_wind_root_node(
                                    filepath, _wm,
                                    _wm_source if _wm_source and os.path.isfile(_wm_source) else None,
                                )
                                _leaf_summary = _nrt.summarize(_leaf_report)
                                if _leaf_summary:
                                    _leaf_note = f"  [{_leaf_summary}]"
                                    break  # root is patched once for the whole file
                        except Exception as _le:
                            print(f"[FO4Export] Wind root patch skipped: {_le}")
                        return True, (f"Exported {mesh_count} mesh(es) as single NIF via "
                                      f"{exporter_label}: {filepath}{multi_obj_note}{_coll_note}{_leaf_note}{_bgsm_note}")
                    fallback_msg = f"NIF export did not finish ({result}); falling back to FBX."
                except Exception as e:
                    print(
                        f"[FO4 Add-on] Scene NIF export error: {e}",
                        file=sys.stderr,
                    )
                    traceback.print_exc(file=sys.stderr)
                    fallback_msg = f"NIF export failed ({e}); falling back to FBX."
                finally:
                    ExportHelpers._restore_emissive_after_export(_emissive_restore)
            else:
                fallback_msg = f"{nif_message}; exporting FBX."

            # FBX fallback – keeps the UCX_ collision meshes in the selection so
            # they are embedded in the FBX and paired with visual meshes by the
            # NIF-conversion step.
            base_path = os.path.splitext(filepath)[0]
            fbx_path = base_path + ".fbx"
            # bpy.ops calls don't raise when an operator declines to run --
            # a failed poll(), no valid selection, bad filepath, etc. all
            # come back as {'CANCELLED'}, never an exception. This call's
            # return value used to be discarded entirely, so a declined FBX
            # export here (the *fallback* path, reached only after NIF
            # export already failed) still returned True with an "Exported
            # ... as FBX" message even though nothing was written -- the
            # same silent-success trap _call_nif_export() above was already
            # hardened against, just missed on this branch. Check it and
            # confirm the file actually landed on disk, same as the
            # documented PyNifly result-code caveat.
            fbx_result = bpy.ops.export_scene.fbx(
                filepath=fbx_path,
                use_selection=True,
                apply_scale_options='FBX_SCALE_ALL',
                mesh_smooth_type='FACE',
                use_mesh_modifiers=True,
            )
            fbx_result_set = set(fbx_result) if fbx_result else set()
            if 'CANCELLED' in fbx_result_set or not os.path.isfile(fbx_path):
                return False, (
                    f"{fallback_msg} FBX export also did not complete "
                    f"(result={fbx_result!r}); nothing was written to {fbx_path}."
                )
            mesh_count = len(meshes)
            return True, f"{fallback_msg} Exported {mesh_count} mesh(es) as FBX: {fbx_path}{multi_obj_note}"

        except Exception as e:
            return False, f"Scene export failed: {str(e)}"
        finally:
            # Always restore the mesh objects to their original state by removing
            # any temporary modifiers that were added during preparation.
            for obj_name, mods in added_mods_per_obj.items():
                obj = scene.objects.get(obj_name)
                if obj:
                    for mod_name in mods:
                        mod = obj.modifiers.get(mod_name)
                        if mod:
                            obj.modifiers.remove(mod)
            # Restore objects to Blender-metre scale after export.
            # See the matching comment in export_mesh_to_nif: this must be a
            # UNIFORM 1/fo4_scale, not orig_scale[i]*inv -- orig_scale's own
            # shape info is already baked into the mesh data by the earlier
            # scale-up-then-apply step, so reusing it here double-applies it
            # (verified empirically to distort non-unity pre-export scale).
            if restore_scale_objs:
                inv = 1.0 / _FO4_UNIT_SCALE_INV
                for o, orig_scale in restore_scale_objs:
                    o.scale = (inv, inv, inv)
                    bpy.context.view_layer.objects.active = o
                    bpy.ops.object.select_all(action='DESELECT')
                    o.select_set(True)
                    # See the matching fix in export_mesh_to_nif: verify
                    # transform_apply actually succeeded (a hidden/
                    # unselectable collision object can make it silently
                    # no-op) and fall back to baking the inverse scale
                    # directly into the mesh data, instead of a bare
                    # try/except that treats silent failure as success and
                    # leaves the object stuck at ~1/70 scale with its
                    # geometry still baked at full FO4 scale.
                    #
                    # SECOND confirmed bug (see the matching comment in
                    # export_mesh_to_nif): transform_apply() on a
                    # MULTI-USER mesh datablock resets .scale to (1,1,1)
                    # and reports 'FINISHED' while silently skipping the
                    # actual vertex bake, which passed the check below even
                    # though nothing was actually restored. Force
                    # single-user data BEFORE the primary attempt, matching
                    # the scale-UP step's own guard.
                    try:
                        if o.data and o.data.users > 1:
                            o.data = o.data.copy()
                    except Exception:
                        pass
                    applied = False
                    try:
                        _restore_result = bpy.ops.object.transform_apply(
                            location=False, rotation=False, scale=True
                        )
                        applied = ('FINISHED' in _restore_result
                                   and tuple(o.scale) == (1.0, 1.0, 1.0))
                    except Exception:
                        applied = False
                    if not applied:
                        try:
                            import mathutils as _mu_restore
                            _sc = o.scale
                            _mat = _mu_restore.Matrix.Diagonal(
                                (_sc.x, _sc.y, _sc.z, 1.0)
                            ).to_4x4()
                            if o.data and o.data.users > 1:
                                o.data = o.data.copy()
                            o.data.transform(_mat)
                            o.data.update()
                            o.scale = (1.0, 1.0, 1.0)
                        except Exception:
                            pass
            # Restore any author-only wind rigs detached before export.
            for obj_name, wt in wind_tokens_per_obj.items():
                obj = scene.objects.get(obj_name)
                if obj:
                    ExportHelpers._reattach_wind_authoring_rig(obj, wt)
            # Restore any author-only weapon animation rigs detached before export.
            for obj_name, weap_t in weapon_tokens_per_obj.items():
                obj = scene.objects.get(obj_name)
                if obj:
                    ExportHelpers._reattach_weapon_authoring_rig(obj, weap_t)
            # Restore any skinned armatures' bind-pose bones back down.
            for _arm, _factor in restore_armature_scales:
                try:
                    ExportHelpers._scale_armature_bones(_arm, 1.0 / _factor)
                except Exception:
                    pass

    @staticmethod
    def export_complete_mod(scene, output_dir):
        """Export complete mod with all assets"""
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        results = {
            'meshes': [],
            'textures': [],
            'animations': [],
            'errors': [],
            'skipped': []  # collision or otherwise excluded meshes
        }
        
        # Create directory structure
        mesh_dir = os.path.join(output_dir, "meshes")
        texture_dir = os.path.join(output_dir, "textures")
        
        os.makedirs(mesh_dir, exist_ok=True)
        os.makedirs(texture_dir, exist_ok=True)
        
        # Export all mesh objects
        for obj in scene.objects:
            if obj.type == 'MESH':
                # skip collision meshes generated by the add-on
                if obj.get("fo4_collision") or obj.name.upper().endswith("_COLLISION") or obj.name.upper().startswith("UCX_"):
                    results['skipped'].append(obj.name)
                    continue

                mesh_path = os.path.join(mesh_dir, f"{ExportHelpers.safe_export_filename(obj.name)}.nif")
                success, message = ExportHelpers.export_mesh_to_nif(obj, mesh_path)

                if success:
                    results['meshes'].append(obj.name)
                else:
                    results['errors'].append(f"{obj.name}: {message}")
            
            elif obj.type == 'ARMATURE':
                # Export armature animation
                if obj.animation_data and obj.animation_data.action:
                    anim_name = obj.animation_data.action.name
                    results['animations'].append(anim_name)
        
        # Create manifest file
        manifest_path = os.path.join(output_dir, "manifest.json")
        with open(manifest_path, 'w') as f:
            json.dump(results, f, indent=2)
        
        # Send event to desktop tutorial server after mod export
        try:
            from . import desktop_tutorial_client
            event_data = {
                'output_dir': output_dir,
                'results': results
            }
            desktop_tutorial_client.DesktopTutorialClient.send_event('mod_exported', event_data)
        except Exception as e:
            print(f"[DesktopTutorialClient] Failed to send mod export event: {e}")
        return True, results
    
    @staticmethod
    def create_mod_structure(mod_name, output_dir):
        """Create basic Fallout 4 mod directory structure"""
        if not mod_name:
            return False, "Mod name cannot be empty"
        
        mod_dir = os.path.join(output_dir, mod_name)
        
        # Create directory structure
        directories = [
            mod_dir,
            os.path.join(mod_dir, "meshes"),
            os.path.join(mod_dir, "textures"),
            os.path.join(mod_dir, "materials"),
            os.path.join(mod_dir, "animations"),
        ]
        
        for directory in directories:
            os.makedirs(directory, exist_ok=True)
        
        # Create README
        readme_path = os.path.join(mod_dir, "README.txt")
        with open(readme_path, 'w') as f:
            f.write(f"Fallout 4 Mod: {mod_name}\n")
            f.write("=" * 50 + "\n\n")
            f.write("Created with Blender Fallout 4 Tutorial Add-on\n\n")
            f.write("Directory Structure:\n")
            f.write("- meshes/: 3D mesh files (.nif)\n")
            f.write("- textures/: Texture files (.dds)\n")
            f.write("- materials/: Material files (.bgsm, .bgem)\n")
            f.write("- animations/: Animation files (.hkx)\n")
        
        return True, f"Mod structure created at: {mod_dir}"


# ---------------------------------------------------------------------------
# Mossy AI export delegation
# ---------------------------------------------------------------------------


def _delegate_to_mossy(operator_id: str, params: dict = None) -> tuple:
    """Delegate a heavy export operation to Mossy via the bridge operator call.

    Mossy can run ck-cmd, Havok tools, NVTT and other external processes
    without requiring them on the local PATH.  Returns (success, message).
    """
    try:
        from . import mossy_link
        ok, msg = mossy_link.check_bridge()
        if not ok:
            return False, f"Mossy bridge offline: {msg}"
        result = mossy_link.install_package_via_mossy(
            package=operator_id,
            github_url=None,
            timeout=120,
        )
        return result
    except Exception as exc:
        return False, f"Mossy delegation error: {exc}"


def register():
    pass


def unregister():
    pass


def _safe_subprocess(cmd: list, timeout: int = 120, cwd: str = None) -> tuple:
    """Run a subprocess with proper timeout and error handling.

    Returns (success, stdout+stderr combined, returncode).
    """
    import subprocess
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, cwd=cwd,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output, result.returncode
    except subprocess.TimeoutExpired:
        return False, f"Process timed out after {timeout}s", -1
    except FileNotFoundError:
        return False, f"Executable not found: {cmd[0]}", -1
    except Exception as exc:
        return False, str(exc), -1
