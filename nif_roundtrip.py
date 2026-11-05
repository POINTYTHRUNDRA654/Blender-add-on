"""
nif_roundtrip.py

Restore original-NIF fields that PyNifly's Blender-scene export can't
reconstruct on its own, so importing a finished FO4 asset, tweaking it in
Blender, and exporting it back doesn't silently strip data the addon has no
UI for (see MEMORY/plan: Creosote01.nif round-trip investigation).

This only patches fields that have a simple, confirmed settable API on
PyNifly's own Python object model (root/shape name, NiShape.has_alpha_property,
node .flags / BSXFlags). Shader BLOCK TYPE (Effect vs Lighting) is NOT
attempted here -- it has no simple setter (shader_block_name is read-only and
fixed at block-creation time) and would need real block construction. That is
reported in ``could_not_restore`` instead of being silently dropped, so callers
can warn the user honestly rather than pretend nothing was lost.

Havok collision reattachment IS handled, but not via round-tripping -- see
``patch_native_collision`` below. PyNifly's own export-side collision
attachment (``CollisionHandler.export_collisions``) creates the
bhkNPCollisionObject/bhkPhysicsSystem blocks correctly in the DLL's live model
when the export target is a NiNode (e.g. an armature/skeleton root), but the
new blocks silently fail to persist through save()/reload when the target is
a leaf mesh shape (BSTriShape/BSSubIndexTriShape) -- confirmed by direct
testing: the block IDs are created, but the shape's own collisionID field
never gets linked at the DLL level, so the collision is orphaned and pruned on
save. Real FO4 static/architecture/furniture files always attach collision at
the NiNode root anyway (confirmed via reference-library reverse-engineering),
so ``patch_native_collision`` builds the collision directly on the exported
file's root node using PyNifly's public NifFile API, sidestepping the
shape-level bug entirely rather than depending on a PyNifly-side fix.

Guarded import: PyNifly (``io_scene_nifly``) is a separate Blender add-on.
If it isn't installed/enabled, every public function here is a no-op that
reports everything as not-restorable.
"""

from __future__ import annotations

import os
import struct

try:
    from io_scene_nifly.pyn import pynifly as _pynifly
except Exception:
    _pynifly = None

try:
    from io_scene_nifly.pyn import bhk_autounpack as _bau
    from io_scene_nifly.pyn import nifconstants as _nc
except Exception:
    _bau = None
    _nc = None

# ── Fallout 4 Havok materials ────────────────────────────────────────────────
# FO4's collision material ID is Bethesda's CRC32 (zero start value, no final
# XOR) of the lowercase material name from the MATT record's MNAM field, e.g.
# "Metal" -> 0x064003D4, "Wood" -> 0x1DD9C611, "Plastic" -> 0x7DFA6E05.
# Verified 2026-11-04 against the real game: all 147 MATT records were read
# from Fallout4.esm, and their IDs matched the material values in vanilla NIF
# collision (16 of 16 checked, e.g. AmmoBox -> MetalHollow, GraveStone -> Stone).
# Only a few names are shared with Skyrim's table (Stone, Wood, Grass, Glass,
# Cloth, Organic, Skin, Dirt); Skyrim names such as HEAVY_STONE or LIGHT_WOOD
# are not FO4 materials at all.
FO4_HAVOK_MATERIAL_NAMES = frozenset({
    'ActorArmored', 'ActorArmoredCrab', 'ActorGeneric', 'ActorGhost', 'ActorGlowing',
    'ActorInsect', 'ActorInsectSmall', 'ActorMetal', 'ActorMetalArmoredPower',
    'ActorMetalLarge', 'ActorMetalSmall', 'ActorSkeleton', 'ActorSkin', 'ActorSkinLarge',
    'ActorSkinSmall', 'ArmorHeavy', 'ArmorLight', 'Arrow', 'Axe1Hand', 'Basket', 'Book',
    'Bottle', 'BottleSmall', 'Brick', 'Carpet', 'CeramicMedium', 'CHAIN', 'Cloth',
    'ClothCushion', 'Coin', 'Concrete', 'ConcreteStairs', 'debug', 'Dirt', 'DirtStairs',
    'Generic', 'Glass', 'GlassStairs', 'Grass', 'GrassStairs', 'Gravel', 'Ice', 'Insect',
    'Insect Wing', 'InsectFloor', 'MaterialActorSynthGen1', 'MaterialBaseball',
    'MaterialBodyBone', 'MaterialCeramicCoffeeMug', 'MaterialClipboard',
    'MaterialGroundDirtLeaves', 'MaterialGroundGrassBush', 'MaterialGroundMetalA',
    'MaterialGroundTileVinyl', 'MaterialGroundTileVinylSanctuaryHills', 'MaterialMetalAuto',
    'MaterialMetalBucket', 'MaterialMetalCanTin', 'MaterialMetalChairFolding',
    'MaterialMetalGasLine', 'MaterialMetalGateVault', 'MaterialMetalLunchbox',
    'MaterialMetalShoppingCart', 'MaterialMetalSign', 'MaterialMetalStairs',
    'MaterialMetalTray', 'MaterialMetalVertibirdChassisLarge',
    'MaterialMetalVertibirdChassisSmall', 'MaterialMetalWeight', 'MaterialOtherBone',
    'MaterialPaperBoxCardboard', 'MaterialPaperBoxSmall', 'MaterialPaperMagazine',
    'MaterialPlasticBin', 'MaterialPlasticBowlingBall', 'MaterialPlasticLarge',
    'MaterialPlasticTrafficCone', 'MaterialRubberBall', 'MaterialRubberTire',
    'MaterialStoneSubwayPillarLarge', 'MaterialSubwayGate', 'MaterialWeaponBlockBladeMedium',
    'MaterialWeaponBlockBladeSmall', 'MaterialWeaponCasing', 'MaterialWeaponCasingMinigun',
    'MaterialWeaponCasingPistol', 'MaterialWeaponCasingRifle', 'MaterialWeaponCasingShotgun',
    'MaterialWeaponMetalBlade1Hand', 'MaterialWeaponMetalBlade1HandSmall',
    'MaterialWeaponMetalGrenade', 'MaterialWeaponMetalMine', 'MaterialWeaponPistol',
    'MaterialWeaponRifle', 'MaterialWeaponRifleLarge', 'MaterialWoodBoard', 'MaterialWoodBroom',
    'MaterialWoodCrate', 'Meat', 'Metal', 'MetalBarrel', 'MetalBarrelTrashCan',
    'MetalBarrelTrashCanOffice', 'MetalHeavy', 'MetalHollow', 'MetalLight', 'MetalSolid',
    'MetalStairs', 'Mud', 'Organic', 'OrganicLarge', 'OtherParent', 'Paper', 'Plastic',
    'PotsPans', 'Sand', 'Sandbag', 'ShieldHeavy', 'ShieldLight', 'Skin', 'Snow', 'SnowStairs',
    'Stone', 'StoneAsStairs', 'StoneBoulderLarge', 'StoneBoulderMedium', 'StoneBoulderSmall',
    'StoneBroken', 'StoneBrokenStairs', 'StoneHeavy', 'StoneStairs', 'WARD', 'Water',
    'WaterPuddle', 'Weapon', 'WeaponBlade2Hand', 'WeaponBlockBlunt', 'WeaponBlunt1Hand',
    'WeaponBlunt2Hand', 'WeaponBowsStaves', 'Web', 'Wood', 'WoodAsStairs', 'WoodBarrel',
    'WoodHeavy', 'WoodLight', 'WoodStairs'
})
_FO4_HAVOK_MATERIAL_BY_LOWER = {n.lower(): n for n in FO4_HAVOK_MATERIAL_NAMES}


def fo4_havok_material_id(name: str):
    """FO4 Havok material ID for a MATT material name (case-insensitive), or
    None when *name* is not a Fallout 4 material."""
    real = _FO4_HAVOK_MATERIAL_BY_LOWER.get(str(name or "").replace("_", "").lower()) \
        or _FO4_HAVOK_MATERIAL_BY_LOWER.get(str(name or "").lower())
    if real is None:
        return None
    import zlib
    return zlib.crc32(real.lower().encode("ascii"), 0xFFFFFFFF) ^ 0xFFFFFFFF


# Shape classes whose hknpShape userData (+0x18, 64-bit, upper half always 0
# in vanilla: 3,246 of 3,246 convex shapes checked) holds the material ID.
# hknpCompressedMeshShape is not listed: its material lives in the
# hknpBSMaterialProperties block instead (+0x34).
_HAVOK_SHAPES_WITH_MATERIAL = frozenset({
    "hknpConvexPolytopeShape", "hknpScaledConvexShape", "hknpDynamicCompoundShape",
    "hknpStaticCompoundShape", "hknpCapsuleShape", "hknpSphereShape",
})


_ALWAYS_UNRESTORABLE = [
    "Havok collision",
    "shader block type (Effect/Lighting)",
]


def available() -> bool:
    """True if PyNifly's low-level Python API (not just the bpy.ops wrapper)
    could be imported."""
    return _pynifly is not None


def _load(path):
    return _pynifly.NifFile(str(path))


def _match_shapes(orig_shapes, exp_shapes):
    """Pair up original <-> exported shapes by name, falling back to
    positional order when names don't line up (e.g. PyNifly de-duplicated
    a name on export)."""
    pairs = []
    exp_by_name = {getattr(s, "name", None): s for s in exp_shapes}
    used = set()
    for o in orig_shapes:
        name = getattr(o, "name", None)
        e = exp_by_name.get(name)
        if e is not None:
            pairs.append((o, e))
            used.add(id(e))
    if not pairs and len(orig_shapes) == len(exp_shapes):
        pairs = list(zip(orig_shapes, exp_shapes))
    return pairs


def patch_exported_nif(exported_path, original_path, obj) -> dict:
    """Restore what we can from *original_path* onto the just-written
    *exported_path*, using *obj* (the Blender mesh object) to decide whether
    the user intentionally renamed it.

    Returns ``{"restored": [...], "could_not_restore": [...]}`` -- always
    include the un-restorable items so callers can surface an honest warning.
    """
    report = {"restored": [], "could_not_restore": list(_ALWAYS_UNRESTORABLE)}

    if not available():
        report["could_not_restore"].insert(0, "name/alpha/flags (PyNifly Python API not available)")
        return report

    if not original_path or not os.path.isfile(original_path):
        return report
    if not exported_path or not os.path.isfile(exported_path):
        return report

    try:
        orig = _load(original_path)
        exp = _load(exported_path)
    except Exception as exc:
        report["could_not_restore"].append(f"(failed to open NIFs for patching: {exc})")
        return report

    # With both files open, name only what the original really had and the
    # export lost, instead of the blanket _ALWAYS_UNRESTORABLE list (which
    # told users a vanilla body "could not restore Havok collision" although
    # it never had any).
    report["could_not_restore"] = []
    try:
        if orig.root.collision_object is not None and exp.root.collision_object is None:
            report["could_not_restore"].append(
                "Havok collision (the original NIF had collision; add a UCX_ collision object to export it)")
    except Exception:
        report["could_not_restore"].append("Havok collision (could not compare)")

    dirty = False

    # ---- root name -------------------------------------------------------
    try:
        stored_original_name = obj.get("fo4_original_name") if obj is not None else None
        current_name = obj.name if obj is not None else None
        user_renamed = bool(
            stored_original_name and current_name and current_name != stored_original_name
        )
        target_name = current_name if user_renamed else (stored_original_name or orig.rootName)
        if target_name and exp.root.name != target_name:
            exp.root.name = target_name
            dirty = True
            report["restored"].append("name")
    except Exception as exc:
        report["could_not_restore"].append(f"name ({exc})")

    # ---- per-shape alpha property ----------------------------------------
    try:
        pairs = _match_shapes(orig.shapes, exp.shapes)
    except Exception as exc:
        pairs = []
        report["could_not_restore"].append(f"shape matching ({exc})")

    for o_shape, e_shape in pairs:
        try:
            o_sh = o_shape.shader.blockname if o_shape.shader else None
            e_sh = e_shape.shader.blockname if e_shape.shader else None
            if o_sh and e_sh and o_sh != e_sh:
                report["could_not_restore"].append(
                    f"shader block type on {e_shape.name} ({o_sh} in the original, {e_sh} exported)")
        except Exception:
            pass

    alpha_restored = False
    for o_shape, e_shape in pairs:
        try:
            if getattr(o_shape, "has_alpha_property", False) and not getattr(
                e_shape, "has_alpha_property", False
            ):
                e_shape.has_alpha_property = True
                # has_alpha_property's setter only creates the in-memory
                # wrapper -- the block is only actually written to the file
                # by save_alpha_property() (NifFile.save() does not call it
                # for us).
                e_shape.save_alpha_property()
                dirty = True
                alpha_restored = True
        except Exception:
            pass

    if alpha_restored:
        report["restored"].append("alpha property")

    # ---- BSXFlags (root-level NiExtraData block, not a shape property) ---
    try:
        o_bsx = orig.root.get_extra_data(blockname="BSXFlags")
        if o_bsx is not None:
            e_bsx = exp.root.get_extra_data(blockname="BSXFlags")
            if e_bsx is not None:
                if e_bsx.flags != o_bsx.flags:
                    e_bsx.flags = o_bsx.flags
                    dirty = True
                    report["restored"].append("BSXFlags")
            else:
                _pynifly.BSXFlags.New(
                    file=exp, name="BSX", flags=int(o_bsx.flags), parent=exp.root
                )
                dirty = True
                report["restored"].append("BSXFlags")
    except Exception as exc:
        report["could_not_restore"].append(f"BSXFlags ({exc})")

    if dirty:
        try:
            exp.save()
        except Exception as exc:
            report["could_not_restore"].append(f"(failed to save patched NIF: {exc})")

    return report


# Real BSXFlags values verified against real vanilla NIFs via PyNifly this
# session (not the values previously documented/coded in this addon, which
# were wrong): furniture uses Havok(2)+Articulated(128)=130, confirmed
# against ModernDomesticLoungeChair01.nif/WorkshopMilitaryCot01.nif. Vegetation
# uses the same 130, confirmed against BlastedForestBurntTreeUpright01.nif.
# STATIC/ARCHITECTURE also use 130, confirmed against CrateDeathclaw01.nif and
# NCA2x1Wall01.nif/DecoBaseA1x1Wall01.nif respectively. DEBRIS uses 194
# (Havok+Dynamic+Articulated), confirmed against ModCrate.nif. FLORA uses a
# distinct value, 170, confirmed against FloraMutFruit01.nif -- NOT the same
# as VEGETATION's 130, despite both being plant-adjacent categories.
# ANIMATED is deliberately NOT included: real animated-prop files disagree
# with each other (139 on DLC04WW_WindMill01Anim.nif/IndustrialFan01.nif, 11
# on CeilingFan01.nif), so it isn't a flat per-type constant like the others
# and needs real design work (likely tied to the specific animation/controller
# setup) rather than a table entry.
FRESH_BSXFLAGS_BY_MESH_TYPE = {
    "FURNITURE": 130,
    "VEGETATION": 130,
    "STATIC": 130,
    "ARCHITECTURE": 130,
    "DEBRIS": 194,
    "FLORA": 170,
}


# Collision decides BSXFlags for plain statics, as in vanilla: of 543 vanilla
# Landscape/Architecture/SetDressing NIFs (surveyed 2026-11-04), 270 of the
# 324 with collision carry 130 (Havok + Articulated), and 183 of the 219
# without collision carry no BSXFlags at all (TreeSMarshRoots01: collision,
# 130; ForestRoots01 / VineCreeperLarge01: neither).
BSXFLAGS_WITH_COLLISION_DEFAULT = 130
_NO_BSX_WITHOUT_COLLISION = {"STATIC", "ARCHITECTURE", "VEGETATION"}


def patch_fresh_bsxflags(exported_path, mesh_type: str, has_collision=None) -> dict:
    """Write the correct BSXFlags value for *mesh_type* onto a freshly
    exported NIF that has no original reference file to round-trip from
    (see :func:`patch_exported_nif` for that case).

    With *has_collision* given, collision decides it for plain statics: a
    mesh with collision gets its type's value, or 130 when the type has none
    (DEFAULT/AUTO); Static/Architecture/Vegetation without collision get
    none. Flora/Debris/Furniture keep their values either way.

    Never overwrites a BSXFlags block the export already carries (e.g. from
    a prior round-trip patch).
    """
    report = {"restored": [], "could_not_restore": []}
    mt = (mesh_type or "").upper()
    flags = FRESH_BSXFLAGS_BY_MESH_TYPE.get(mt)
    if has_collision is True and flags is None:
        flags = BSXFLAGS_WITH_COLLISION_DEFAULT
    elif has_collision is False and mt in _NO_BSX_WITHOUT_COLLISION:
        flags = None
    if flags is None or _pynifly is None:
        return report

    try:
        exp = _load(exported_path)
        existing = exp.root.get_extra_data(blockname="BSXFlags")
        if existing is not None:
            return report  # don't clobber a value already restored/present
        _pynifly.BSXFlags.New(file=exp, name="BSX", flags=int(flags), parent=exp.root)
        exp.save()
        report["restored"].append(f"BSXFlags={flags}")
    except Exception as exc:
        report["could_not_restore"].append(f"BSXFlags ({exc})")

    return report


# Real, fixed root-node names verified against the vanilla reference
# library, keyed by fo4_mesh_type. Unlike BSXFlags (a per-category
# constant), most mesh types don't have a single "correct" root name -- real
# static props (e.g. CrateDeathclaw01.nif) name their root after the asset
# itself, which isn't something we can derive with confidence (varies per
# file, unclear whether it's functionally load-bearing or purely
# cosmetic/organizational) -- so only WEAPON is included here, since every
# real weapon file checked (10mmPistol, CombatShotgun, GaussRifle,
# Cryolator, Flamer, AssaultRifleProto) uses the exact same literal root
# name "WEAPON" regardless of the specific weapon, a genuine fixed
# convention rather than an asset-specific one.
FRESH_ROOT_NAME_BY_MESH_TYPE = {
    "WEAPON": "WEAPON",
}


def patch_root_name(exported_path, mesh_type: str) -> dict:
    """Rename a freshly exported NIF's root node to the real convention for
    *mesh_type*, when one is known (see :data:`FRESH_ROOT_NAME_BY_MESH_TYPE`).

    PyNifly's own export-time root-naming mechanism (a Blender object tagged
    with a ``pynRoot`` custom property) triggered a hard, low-level DLL
    crash (EXCEPTION_ACCESS_VIOLATION) when exercised directly -- renaming
    the root node AFTER export, via the public NifFile API only (the same
    "open the just-exported file, patch a value, resave" approach already
    used by :func:`patch_fresh_bsxflags`), avoids that code path entirely
    and was verified safe end-to-end.
    """
    report = {"restored": [], "could_not_restore": []}
    real_name = FRESH_ROOT_NAME_BY_MESH_TYPE.get((mesh_type or "").upper())
    if real_name is None or _pynifly is None:
        return report

    try:
        exp = _load(exported_path)
        if exp.root.name == real_name:
            return report  # already correct, nothing to do
        exp.root.name = real_name
        exp.save()
        report["restored"].append(f"root name={real_name}")
    except Exception as exc:
        report["could_not_restore"].append(f"root name ({exc})")

    return report


def _patch_havok_material(filepath, material_name: str) -> tuple:
    """Write the Fallout 4 Havok material for *material_name* into every
    collision shape in *filepath*.

    PyNifly's Havok packer (bhk_autopack.py) has no material parameter: a
    freshly built shape always carries the material from its fixed byte
    template (Plastic, 0x7DFA6E05, whatever was asked for). This raw post-save
    patch puts the real value in two places, matching vanilla:

    * hknpBSMaterialProperties +0x34 (compressed-mesh collision), and
    * the shape's userData at +0x18 for convex, scaled-convex, compound,
      capsule and sphere shapes. This is where vanilla keeps it for the
      convex hulls this add-on writes; before 2026-11-04 only the first
      place was patched, so every convex export stayed Plastic.

    *material_name* may be an FO4 MATT name ("Stone", "MetalHollow") or a
    Skyrim enum name that is also an FO4 material ("STONE", "WOOD"). Names
    that are not FO4 materials are refused rather than written.

    Returns (patched_count, message).
    """
    if _bau is None:
        return 0, "material patch skipped (PyNifly internals unavailable)"
    material_value = fo4_havok_material_id(material_name)
    if material_value is None:
        return 0, f"material patch skipped ({material_name!r} is not a Fallout 4 material)"

    data = bytearray(open(filepath, "rb").read())
    blocks, _num = _bau._parse_nif_blocks(bytes(data))
    patched = 0
    for b in blocks:
        if "bhk" not in b["type"] and "hkn" not in b["type"].lower():
            continue
        raw_blob = b["blob"]
        magic_pos = raw_blob.find(_bau.HAVOK_MAGIC)
        if magic_pos < 0:
            continue
        blob = raw_blob[magic_pos:]
        try:
            hdrs = _bau.parse_section_headers(blob)
        except Exception:
            continue
        if "__classnames__" not in hdrs or "__data__" not in hdrs:
            continue
        cn_start = hdrs["__classnames__"].abs_start
        data_hdr = hdrs["__data__"]
        try:
            objs = _bau.parse_virtual_fixups(blob, data_hdr, cn_start)
        except Exception:
            continue
        for rel_off, cls in objs:
            if cls == "hknpBSMaterialProperties":
                field = 0x34
            elif cls in _HAVOK_SHAPES_WITH_MATERIAL:
                field = 0x18
            else:
                continue
            # b["offset"] = this NIF block's absolute start in the file;
            # magic_pos = packfile start within the block's blob;
            # data_hdr.abs_start + rel_off = the instance's start within
            # the packfile's __data__ section; + field = the material.
            abs_file_off = b["offset"] + magic_pos + data_hdr.abs_start + rel_off + field
            if abs_file_off + 4 > len(data):
                continue
            data[abs_file_off:abs_file_off + 4] = material_value.to_bytes(4, "little")
            patched += 1

    if patched:
        with open(filepath, "wb") as fh:
            fh.write(data)
        real = _FO4_HAVOK_MATERIAL_BY_LOWER.get(str(material_name).replace("_", "").lower()) or material_name
        return patched, f"Havok material set to {real} ({patched} shape(s))"
    return 0, "material patch found no collision shape to patch"


_HULL_MAX_VERTS = 128


def _convex_hull_for_havok(collision_obj, sf):
    """World-space convex hull of *collision_obj* in Havok units, as
    (verts, triangle faces). Capped at _HULL_MAX_VERTS by decimating the
    source first, so the shape stays in the size range vanilla uses."""
    import bmesh
    import bpy

    def hull_of(mesh_data):
        bm = bmesh.new()
        bm.from_mesh(mesh_data)
        bm.transform(collision_obj.matrix_world)
        res = bmesh.ops.convex_hull(bm, input=bm.verts[:])
        bmesh.ops.delete(bm, geom=res.get("geom_interior", []) + res.get("geom_unused", []), context='VERTS')
        bmesh.ops.triangulate(bm, faces=bm.faces[:])
        bm.verts.index_update()
        out = ([tuple(v.co / sf) for v in bm.verts], [[v.index for v in f.verts] for f in bm.faces])
        bm.free()
        return out

    verts, faces = hull_of(collision_obj.data)
    if len(verts) <= _HULL_MAX_VERTS:
        return verts, faces
    tmp = collision_obj.copy()
    tmp.data = collision_obj.data.copy()
    bpy.context.scene.collection.objects.link(tmp)
    try:
        ratio = 1.0
        for _ in range(6):
            ratio = max(0.005, ratio * _HULL_MAX_VERTS / len(verts) * 0.9)
            mod = tmp.modifiers.new("hull_cap", 'DECIMATE')
            mod.ratio = ratio
            mesh = tmp.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh()
            verts, faces = hull_of(mesh)
            tmp.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh_clear()
            tmp.modifiers.remove(mod)
            if len(verts) <= _HULL_MAX_VERTS:
                break
    finally:
        me = tmp.data
        bpy.data.objects.remove(tmp, do_unlink=True)
        bpy.data.meshes.remove(me)
    return verts, faces


def _block_layout(data: bytes):
    """(types per block, data offset per block, size per block) of a FO4 NIF."""
    o = data.index(b'\n') + 1
    _ver, _endian, _user, nblocks = struct.unpack_from('<IBII', data, o)
    o += 13
    bsver = struct.unpack_from('<I', data, o)[0]
    o += 4
    for _ in range(4 if bsver >= 103 else 3):
        o += 1 + data[o]
    ntypes = struct.unpack_from('<H', data, o)[0]
    o += 2
    names = []
    for _ in range(ntypes):
        n = struct.unpack_from('<I', data, o)[0]
        names.append(data[o + 4:o + 4 + n].decode('latin1'))
        o += 4 + n
    type_idx = struct.unpack_from(f'<{nblocks}H', data, o)
    o += 2 * nblocks
    sizes = struct.unpack_from(f'<{nblocks}I', data, o)
    o += 4 * nblocks
    nstrings = struct.unpack_from('<I', data, o)[0]
    o += 8
    for _ in range(nstrings):
        o += 4 + struct.unpack_from('<I', data, o)[0]
    o += 4 + 4 * struct.unpack_from('<I', data, o)[0]
    offsets = []
    for s in sizes:
        offsets.append(o)
        o += s
    return [names[i] for i in type_idx], offsets, list(sizes)


def patch_native_collision_multi(exported_path, collision_objs) -> dict:
    """Attach several convex collision pieces, each as its own one-body
    bhkPhysicsSystem on its own bhkNPCollisionObject (piece 0 on the root
    node, piece i on a child NiNode). This is how openings (doorways, cave
    mouths) stay open: each piece is convex, the gaps between pieces are not
    filled.

    One system per piece is the layout vanilla precombined meshes use (up to
    590 one-body systems in one NIF, body_id 0, flags 128 each). A single
    shared N-body system (the HouseKGrndAddonDeckKPostCol02 layout) is NOT
    used: PyNifly's N-body writer (pack_multi_polytope) writes the
    referencedObjects array 16 bytes apart where Havok reads 8, and CK
    asserts "Instance should have already been created"
    (bhknpcollisionobject.cpp line 611) and crashes when such a mesh is
    placed -- seen 2026-11-02 on a 320-piece building. The one-body writer
    is the one CK already loaded on a plant.

    PyNifly writes body_id 0xFFFFFFFF on these collision objects; the binary
    patch below sets body_id 0 and checks each points at its own system.
    """
    objs = [o for o in (collision_objs or []) if o is not None and o.type == 'MESH']
    if len(objs) <= 1:
        return patch_native_collision(exported_path, objs[0] if objs else None)
    report = {"restored": [], "could_not_restore": []}
    if _pynifly is None:
        return report
    try:
        from io_scene_nifly.pyn.nifconstants import HAVOC_SCALE_FACTOR, game_collision_sf
        from io_scene_nifly.pyn.nifdefs import PynBufferTypes, TransformBuf
        from io_scene_nifly.pyn.bhk_autounpack import CollisionShape, PhysicsProps
    except Exception as exc:
        report["could_not_restore"].append(f"Havok collision (PyNifly internals unavailable: {exc})")
        return report

    try:
        exp = _load(exported_path)
        if exp.root.collision_object is not None:
            return report  # already has one -- don't clobber
        sf = HAVOC_SCALE_FACTOR * game_collision_sf.get(exp.game, 1.0)
        first = objs[0]
        rb = first.rigid_body
        physics = PhysicsProps(
            is_dynamic=False,
            friction=rb.friction if rb else 0.5,
            restitution=rb.restitution if rb else 0.4,
            linear_damping=rb.linear_damping if rb else 0.1,
            angular_damping=rb.angular_damping if rb else 0.05,
            gravity_factor=first.get('pynPhysGravityFactor', 1.0),
            max_linear_velocity=first.get('pynPhysMaxLinVel', 104.4),
            max_angular_velocity=first.get('pynPhysMaxAngVel', 31.57),
        )
        shapes = []
        for o in objs:
            verts, faces = _convex_hull_for_havok(o, sf)
            if len(verts) < 4:
                report["could_not_restore"].append(f"collision piece '{o.name}' (flat or empty, skipped)")
                continue
            shapes.append(CollisionShape(
                shape_type='polytope', name=o.name, transform=None, verts=verts,
                faces=faces, convex_radius=0.0, children=[], physics=physics))
        if len(shapes) <= 1:
            return patch_native_collision(exported_path, objs[0]) if shapes else report

        # flags 128: the value on all 1,401 vanilla landscape bhkNPCollisionObjects
        kwargs = {"flags": 128, "collision_type": PynBufferTypes.bhkNPCollisionObjectBufType}
        import inspect
        try:
            has_body_id = "body_id" in inspect.signature(exp.root.add_collision).parameters
        except (TypeError, ValueError):
            has_body_id = False
        coll_kwargs = dict(kwargs, body_id=0) if has_body_id else kwargs
        root_coll = exp.root.add_collision(None, **coll_kwargs)
        _pynifly.bhkPhysicsSystem.New(exp, shapes=shapes[:1], parent=root_coll)
        for i in range(1, len(shapes)):
            xf = TransformBuf()
            xf.set_identity()
            node = exp.add_node(f"{exp.root.name}_Collision{i:02d}", xf, parent=exp.root)
            coll = node.add_collision(None, **coll_kwargs)
            _pynifly.bhkPhysicsSystem.New(exp, shapes=shapes[i:i + 1], parent=coll)
        exp.save()

        # Each collision object must point at its own one-body system with
        # body_id 0. PyNifly writes the system right after its collision
        # object (as vanilla does); fall back to that if the ref is not set.
        with open(exported_path, "rb") as fh:
            data = bytearray(fh.read())
        types, offsets, _sizes = _block_layout(bytes(data))
        colls = [i for i, t in enumerate(types) if t == 'bhkNPCollisionObject']
        phys = [i for i, t in enumerate(types) if t == 'bhkPhysicsSystem']
        if len(phys) != len(shapes) or len(colls) != len(shapes):
            report["could_not_restore"].append(
                f"multi-piece collision layout ({len(phys)} physics blocks, {len(colls)} collision objects for {len(shapes)} pieces)")
            return report
        used = set()
        for ci in colls:
            off = offsets[ci]
            dref = struct.unpack_from('<i', data, off + 6)[0]
            if not (0 <= dref < len(types) and types[dref] == 'bhkPhysicsSystem') and ci + 1 < len(types) \
                    and types[ci + 1] == 'bhkPhysicsSystem':
                dref = ci + 1
            if not (0 <= dref < len(types) and types[dref] == 'bhkPhysicsSystem') or dref in used:
                report["could_not_restore"].append("multi-piece collision (could not link each piece to its own physics system)")
                return report
            used.add(dref)
            struct.pack_into('<H', data, off + 4, 128)     # flags (after target i32)
            struct.pack_into('<i', data, off + 6, dref)    # data ref
            struct.pack_into('<I', data, off + 10, 0)      # body_id
        with open(exported_path, "wb") as fh:
            fh.write(data)
        report["restored"].append(f"Havok collision ({len(shapes)} convex pieces, one physics system each)")
    except Exception as exc:
        report["could_not_restore"].append(f"Havok multi-piece collision ({exc})")
    return report


def patch_native_collision(exported_path, collision_obj) -> dict:
    """Attach a real FO4 native-physics Havok collision
    (bhkNPCollisionObject -> bhkPhysicsSystem) to a freshly exported NIF's
    root node, built directly from *collision_obj*'s Blender mesh data.

    Always targets the file's root node rather than the individual visual
    shape -- see the collision-reattachment note in this module's docstring
    for why: attaching to a leaf shape silently fails to persist, while
    attaching to the (NiNode) root works and matches real FO4 file layout.

    Skips (no-op) if the export already carries a root-level collision (e.g.
    a round-tripped original already restored one) so this never clobbers an
    existing block. Uses ``collision_obj['pynCollisionShapeType']``
    ('polytope' / 'compressed_mesh' / 'sphere', set by
    MeshHelpers.add_collision_mesh / collision_from_lod_mesh) to choose the
    Havok shape kind; defaults to 'polytope' (convex hull) if unset.
    """
    report = {"restored": [], "could_not_restore": []}
    if _pynifly is None or collision_obj is None:
        return report

    try:
        from io_scene_nifly.pyn.nifconstants import HAVOC_SCALE_FACTOR, game_collision_sf
        from io_scene_nifly.pyn.nifdefs import PynBufferTypes
        from io_scene_nifly.pyn.bhk_autounpack import CollisionShape, PhysicsProps
    except Exception as exc:
        report["could_not_restore"].append(f"Havok collision (PyNifly internals unavailable: {exc})")
        return report

    try:
        exp = _load(exported_path)
        if exp.root.collision_object is not None:
            return report  # already has one -- don't clobber

        sf = HAVOC_SCALE_FACTOR * game_collision_sf.get(exp.game, 1.0)
        shape_type = collision_obj.get('pynCollisionShapeType', 'polytope')
        if shape_type not in ('polytope', 'compressed_mesh', 'sphere'):
            shape_type = 'polytope'

        rb = collision_obj.rigid_body
        physics = PhysicsProps(
            is_dynamic=False,
            friction=rb.friction if rb else 0.5,
            restitution=rb.restitution if rb else 0.4,
            linear_damping=rb.linear_damping if rb else 0.1,
            angular_damping=rb.angular_damping if rb else 0.05,
            gravity_factor=collision_obj.get('pynPhysGravityFactor', 1.0),
            max_linear_velocity=collision_obj.get('pynPhysMaxLinVel', 104.4),
            max_angular_velocity=collision_obj.get('pynPhysMaxAngVel', 31.57),
        )

        world_mat = collision_obj.matrix_world
        if shape_type == 'sphere':
            import mathutils
            radius = max(collision_obj.dimensions) / 2.0 / sf
            verts_ws = [world_mat @ v.co for v in collision_obj.data.vertices]
            centroid = sum(verts_ws, mathutils.Vector()) / max(len(verts_ws), 1) / sf
            from io_scene_nifly.pyn.bhk_autounpack import BodyTransform
            shape = CollisionShape(
                shape_type='sphere', name=collision_obj.name,
                transform=BodyTransform(position=tuple(centroid),
                                         rotation=((1, 0, 0), (0, 1, 0), (0, 0, 1))),
                verts=[], faces=[], convex_radius=0.0, children=[],
                sphere_radius=radius, physics=physics,
            )
        else:
            if shape_type == 'compressed_mesh':
                # PyNifly (v27 and v28 alike) writes hknpCompressedMeshShape
                # with an empty search tree: 0 tree nodes and an empty
                # triangle bitfield. All 167,285 vanilla compressed-mesh
                # collisions have both filled, and Creation Kit crashes in
                # the Havok loader on the empty ones (write to address 0x18
                # in hknpPhysicsSystemData loading; GSFungusSpore11.nif,
                # 2026-11-02). Write a real convex hull instead -- the shape
                # most vanilla plants use (hknpConvexPolytopeShape).
                verts, faces = _convex_hull_for_havok(collision_obj, sf)
                shape_type = 'polytope'
                report["restored"].append(
                    "exact-mesh collision written as a convex hull "
                    "(PyNifly cannot build the compressed-mesh tree CK needs)")
            else:
                verts = [tuple(world_mat @ v.co / sf) for v in collision_obj.data.vertices]
                faces = [list(p.vertices) for p in collision_obj.data.polygons]
            radius = 0.0
            if rb and rb.use_margin:
                radius = rb.collision_margin / sf
            else:
                radius = collision_obj.get('pynCollisionRadius', 0.0) / sf
            shape = CollisionShape(
                shape_type=shape_type, name=collision_obj.name,
                transform=None, verts=verts, faces=faces,
                convex_radius=radius, children=[], physics=physics,
            )

        # PyNifly v28 added a required `body_id` param for
        # bhkNPCollisionObjectBufType: it indexes the body's position within
        # the bhkPhysicsSystem's packed body array (built just below from
        # `shapes=[shape]`, a single-body list, so index 0 is always
        # correct here). Leaving it unset defaults to a sentinel that the
        # game engine dereferences as a bad index in
        # bhkNPCollisionObject::CreateInstance and CRASHES on load -- this
        # is not just a PyNifly-side issue, it produces a broken NIF.
        # inspect the live signature rather than hard-coding a version
        # check so this keeps working whether the installed PyNifly is the
        # older (no body_id) or newer (requires it) build.
        import inspect
        # flags 128: the value on all 1,401 vanilla landscape bhkNPCollisionObjects
        add_collision_kwargs = {"flags": 128, "collision_type": PynBufferTypes.bhkNPCollisionObjectBufType}
        try:
            if "body_id" in inspect.signature(exp.root.add_collision).parameters:
                add_collision_kwargs["body_id"] = 0
        except (TypeError, ValueError):
            pass
        coll_node = exp.root.add_collision(None, **add_collision_kwargs)
        _pynifly.bhkPhysicsSystem.New(exp, shapes=[shape], parent=coll_node)
        exp.save()
        report["restored"].append(f"Havok collision ({shape_type})")

        # bhkPhysicsSystem.New() above has no material parameter at all (see
        # _patch_havok_material's docstring) -- every freshly-built shape
        # gets the same fixed template regardless of collision type. Patch
        # the real value in afterward, sourced from the same
        # pyn_collshape.bhkMaterial property MeshHelpers.add_*collision*()
        # already sets on collision_obj for PyNifly's own native-export path.
        material_name = str(collision_obj.get("fo4_havok_material", "") or "")
        if not material_name:
            try:
                material_name = collision_obj.pyn_collshape.bhkMaterial if hasattr(
                    collision_obj, 'pyn_collshape') else ""
            except Exception:
                material_name = ""
        if material_name:
            patched, mat_msg = _patch_havok_material(exported_path, material_name)
            if patched:
                report["restored"].append(f"Havok material ({material_name})")
            else:
                report["could_not_restore"].append(f"Havok material ({mat_msg})")
    except Exception as exc:
        report["could_not_restore"].append(f"Havok collision ({exc})")

    return report



def _nif_block_type_table(data: bytes):
    """Parse just enough of the BSStreamHeader to locate the block-type
    string table and the per-block type-index array, without needing
    PyNifly at all -- a raw binary read, same spirit as
    _patch_havok_material's direct byte patching.

    Layout (empirically verified against FO4 NIF version 20.2.0.7 /
    BSVersion 130 exports, the only version this addon produces): header
    string line -> version(4) + endian(1) + user version(4) -> num_blocks
    (4) -> bsversion(4) -> four 1-byte-length-prefixed strings (Author /
    ProcessScript / ExportScript / a 4th empty field) -> num_block_types
    (2) -> block_types[] (each a 4-byte-length-prefixed ASCII string) ->
    block_type_index[] (num_blocks * 2 bytes, one u16 per block).

    Returns a dict with everything patch_root_to_leafanim_bytes() and
    _root_block_type() need: numblocks, block_types (list[bytes]),
    block_type_index (list[int]), and the byte offsets bracketing the
    block-types table and the block-type-index array so a patch can splice
    in a replacement without touching anything else in the file.
    """
    nl = data.index(b'\n')
    off = nl + 1 + 4 + 1 + 4  # version, endian, user version
    numblocks = struct.unpack_from('<I', data, off)[0]
    off += 4

    off2 = off + 4  # skip bsversion
    for _ in range(4):
        ln = data[off2]
        off2 += 1 + ln
    num_block_types_pos = off2
    num_block_types = struct.unpack_from('<H', data, off2)[0]
    off2 += 2
    block_types_start = off2
    block_types = []
    for _ in range(num_block_types):
        ln = struct.unpack_from('<I', data, off2)[0]
        off2 += 4
        s = data[off2:off2 + ln]
        off2 += ln
        block_types.append(s)
    block_types_end = off2

    block_type_index = list(struct.unpack_from('<%dH' % numblocks, data, off2))
    block_type_index_start = off2
    block_type_index_end = off2 + 2 * numblocks

    return {
        "numblocks": numblocks,
        "num_block_types": num_block_types,
        "num_block_types_pos": num_block_types_pos,
        "block_types": block_types,
        "block_types_start": block_types_start,
        "block_types_end": block_types_end,
        "block_type_index": block_type_index,
        "block_type_index_start": block_type_index_start,
        "block_type_index_end": block_type_index_end,
    }


def _root_block_type(data: bytes):
    """Return the root (block 0) block-type name as bytes (e.g.
    ``b'NiNode'``, ``b'BSLeafAnimNode'``), or ``None`` if the file is empty
    or malformed."""
    try:
        table = _nif_block_type_table(data)
        if table["numblocks"] < 1:
            return None
        return table["block_types"][table["block_type_index"][0]]
    except Exception:
        return None


def _patch_root_to_leafanim_bytes(data: bytes) -> bytes:
    """Rewrite *data*'s block-type table so the root (block 0) is typed as
    ``BSLeafAnimNode`` instead of ``NiNode``.

    Safe because PyNifly's own Python object model defines
    ``BSLeafAnimNode`` (and ``BSFadeNode``) as pure aliases of ``NiNode`` --
    ``class BSLeafAnimNode(NiNode): pass``, identical NiNodeBufType, no
    extra fields -- so the distinction lives ONLY in this block-type name
    string, never in any field data. Reusing an existing ``BSLeafAnimNode``
    table entry if the file already has one, otherwise appending a new
    table entry, exactly mirrors how NifSkope/the game itself resolve a
    block's type: by index into this table. Every other byte of block data
    is left untouched.

    Raises ValueError if block 0 isn't actually typed NiNode (so callers
    don't silently mis-patch an unexpected file shape).
    """
    table = _nif_block_type_table(data)
    block_types = table["block_types"]
    block_type_index = list(table["block_type_index"])

    if b'NiNode' not in block_types:
        raise ValueError("no NiNode entry in block-type table")
    if block_type_index[0] != block_types.index(b'NiNode'):
        raise ValueError("root (block 0) isn't typed NiNode -- refusing to patch")

    if b'BSLeafAnimNode' in block_types:
        new_index = block_types.index(b'BSLeafAnimNode')
        new_block_types_bytes = data[table["block_types_start"]:table["block_types_end"]]
        new_num_block_types = table["num_block_types"]
    else:
        new_index = table["num_block_types"]
        new_block_types_bytes = (
            data[table["block_types_start"]:table["block_types_end"]]
            + struct.pack('<I', len(b'BSLeafAnimNode'))
            + b'BSLeafAnimNode'
        )
        new_num_block_types = table["num_block_types"] + 1

    block_type_index[0] = new_index

    return (
        data[:table["num_block_types_pos"]]
        + struct.pack('<H', new_num_block_types)
        + new_block_types_bytes
        + struct.pack('<%dH' % table["numblocks"], *block_type_index)
        + data[table["block_type_index_end"]:]
    )


def patch_wind_root_node(exported_path, obj=None, original_path=None) -> dict:
    """Reconstruct the ``BSLeafAnimNode`` root wrapper on a freshly exported
    wind-foliage NIF, when PyNifly's export dropped it in favor of a plain
    ``NiNode``.

    Root cause: PyNifly's public add_block()/NifFile API has no way to
    create or relabel a block as BSLeafAnimNode distinctly from NiNode --
    confirmed via source inspection (both are defined as pure NiNode
    aliases in pynifly.py) and via the raw NiflyDLL.dll CDLL (no header/
    block-type manipulation function exposed at all). So every PyNifly
    export of a wind-animated mesh writes a plain NiNode root, even though
    every real FO4 wind-foliage file (vanilla and this mod's own originals)
    uses a BSLeafAnimNode root paired with the Vertex_Colors|Tree_Anim
    shader flags PyNifly DOES write correctly. Verified via side-by-side
    reference comparison (Cedar01.nif, TreeMaplePreWar02.nif, and the
    original unpatched DeadShrubGS02.nif all use BSLeafAnimNode roots) that
    those flags are legitimate for wind foliage -- the missing root type is
    the actual defect, not the flags. Verified safe end-to-end: the patched
    root loads correctly through PyNifly's own NifFile parser (root class
    reports as BSLeafAnimNode) and through the real Blender import operator.

    This is a raw post-export binary patch (see _patch_root_to_leafanim_bytes)
    because there is no PyNifly API path to it; it does not require PyNifly
    to be installed at all.

    Only applies when there's real evidence this object is wind-animated
    foliage that should get the BSLeafAnimNode treatment, to avoid
    mistyping an unrelated NiNode-rooted asset:
      - the object still carries (or carried, before the pynifly-skinning
        cleanup) a "Wind" vertex group, i.e. AnimationHelpers.apply_wind_
        animation's procedural wind pipeline was used on it, or
      - *original_path* is a real source NIF whose own root is already
        BSLeafAnimNode (a round-trip case -- restore what the original had).

    Returns ``{"restored": [...], "could_not_restore": [...]}``, following
    this module's convention. An object with neither signal returns both
    lists empty (not applicable, not a failure).
    """
    report = {"restored": [], "could_not_restore": []}

    has_wind_group = False
    try:
        has_wind_group = bool(obj is not None and obj.vertex_groups.get("Wind"))
    except Exception:
        has_wind_group = False

    original_is_leafanim = False
    if original_path and os.path.isfile(original_path):
        try:
            with open(original_path, "rb") as fh:
                orig_data = fh.read()
            original_is_leafanim = _root_block_type(orig_data) == b'BSLeafAnimNode'
        except Exception:
            original_is_leafanim = False

    try:
        from .animation_helpers import AnimationHelpers as _AH
        wind_on = _AH.wind_enabled(obj) if obj is not None else False
        no_wind = bool(obj is not None and obj.get("fo4_no_wind"))
    except Exception:
        wind_on, no_wind = has_wind_group, False
    # A Wind group alone is not enough: without a tree material the root would
    # say "wind" while the shader does not. Marked no-wind never gets it.
    if no_wind or not (wind_on or original_is_leafanim):
        return report

    if not exported_path or not os.path.isfile(exported_path):
        return report

    try:
        with open(exported_path, "rb") as fh:
            data = fh.read()
        current_root_type = _root_block_type(data)
        if current_root_type == b'BSLeafAnimNode':
            return report  # already correct (e.g. round-tripped original)
        if current_root_type != b'NiNode':
            report["could_not_restore"].append(
                f"BSLeafAnimNode root (unexpected root type {current_root_type!r})"
            )
            return report
        patched = _patch_root_to_leafanim_bytes(data)
        with open(exported_path, "wb") as fh:
            fh.write(patched)
        report["restored"].append("root node -> BSLeafAnimNode (wind foliage)")
    except Exception as exc:
        report["could_not_restore"].append(f"BSLeafAnimNode root ({exc})")

    return report


def summarize(report: dict) -> str:
    """One-line human-readable summary for a report dict from
    :func:`patch_exported_nif`, suitable for appending to an export success
    message."""
    parts = []
    if report.get("restored"):
        parts.append("restored: " + ", ".join(report["restored"]))
    if report.get("could_not_restore"):
        parts.append("could not restore: " + ", ".join(report["could_not_restore"]))
    return "; ".join(parts)
