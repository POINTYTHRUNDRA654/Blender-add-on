"""
fo4_bone_names.py
Canonical Fallout 4 character skeleton bone names, read from the vanilla
Meshes/Actors/Character/CharacterAssets/skeleton.nif (129 nodes) with
PyNifly on 2026-10-27. All animation modules should import from here (and
look bones up with find_bone()) rather than hard-coding bone-name strings.

History: until 2026-10-27 this table held Skyrim's "NPC <name> [abbr]" names
(e.g. "NPC L Hand [LHnd]") while claiming to come from the FO4 skeleton. None
of them exist in Fallout 4, so every module that built or searched a rig by
these names silently did nothing on a real FO4 skeleton. to_fo4() still
accepts the old Skyrim-style names so existing call sites keep working.

A FO4 bone can appear in Blender under two spellings:
  - the raw NIF name ("LArm_Hand", "RLeg_Thigh", "SPINE1") -- PyNifly with
    "rename bones" off, and what the game files use;
  - PyNifly's default renamed form ("Arm_Hand.L", "Leg_Thigh.R"), which it
    converts back to the raw name on export.
find_bone() accepts either, plus the old Skyrim-style names.

Usage:
    from . import fo4_bone_names as _bn
    hand = _bn.find_bone(armature_obj.pose.bones, _bn.NPC["L_HAND"])
"""
import re as _re

# ---------------------------------------------------------------------------
# Canonical character bone names (raw NIF names from vanilla skeleton.nif).
# The spine is Pelvis -> SPINE1 -> SPINE2 -> Chest -> Neck -> HEAD; arms
# have three forearm segments (ForeArm1-3) -- ForeArm1 is used as "the"
# forearm here.
# ---------------------------------------------------------------------------
NPC = {
    # Root / centre of mass
    "ROOT":        "Root",
    "COM":         "COM",
    "PELVIS":      "Pelvis",

    # Spine chain
    "SPINE0":      "SPINE1",
    "SPINE1":      "SPINE2",
    "SPINE2":      "Chest",

    # Head / neck
    "NECK":        "Neck",
    "HEAD":        "HEAD",

    # Left arm
    "L_CLAVICLE":  "LArm_Collarbone",
    "L_UPPER_ARM": "LArm_UpperArm",
    "L_FOREARM":   "LArm_ForeArm1",
    "L_HAND":      "LArm_Hand",

    # Right arm
    "R_CLAVICLE":  "RArm_Collarbone",
    "R_UPPER_ARM": "RArm_UpperArm",
    "R_FOREARM":   "RArm_ForeArm1",
    "R_HAND":      "RArm_Hand",

    # Left leg
    "L_THIGH":     "LLeg_Thigh",
    "L_CALF":      "LLeg_Calf",
    "L_FOOT":      "LLeg_Foot",
    "L_TOE":       "LLeg_Toe1",

    # Right leg
    "R_THIGH":     "RLeg_Thigh",
    "R_CALF":      "RLeg_Calf",
    "R_FOOT":      "RLeg_Foot",
    "R_TOE":       "RLeg_Toe1",
}

# The Skyrim-style names this table used to hold, mapped to the real FO4
# bone, so old call sites and saved data still resolve.
SKYRIM_STYLE_TO_FO4 = {
    "NPC Root [Root]": "Root", "NPC COM [COM ]": "COM", "NPC Pelvis [Pelv]": "Pelvis",
    "NPC Spine [Spn0]": "SPINE1", "NPC Spine1 [Spn1]": "SPINE2", "NPC Spine2 [Spn2]": "Chest",
    "NPC Neck [Neck]": "Neck", "NPC Head [Head]": "HEAD",
    "NPC L Clavicle [LClv]": "LArm_Collarbone", "NPC L UpperArm [LUar]": "LArm_UpperArm",
    "NPC L Forearm [LLar]": "LArm_ForeArm1", "NPC L Hand [LHnd]": "LArm_Hand",
    "NPC R Clavicle [RClv]": "RArm_Collarbone", "NPC R UpperArm [RUar]": "RArm_UpperArm",
    "NPC R Forearm [RLar]": "RArm_ForeArm1", "NPC R Hand [RHnd]": "RArm_Hand",
    "NPC L Thigh [LThg]": "LLeg_Thigh", "NPC L Calf [LClf]": "LLeg_Calf",
    "NPC L Foot [Lft ]": "LLeg_Foot", "NPC L Toe0 [LToe]": "LLeg_Toe1",
    "NPC R Thigh [RThg]": "RLeg_Thigh", "NPC R Calf [RClf]": "RLeg_Calf",
    "NPC R Foot [Rft ]": "RLeg_Foot", "NPC R Toe0 [RToe]": "RLeg_Toe1",
}

_SIDED = _re.compile(r'^([LR])(Arm|Leg)_(.+)$')
_PYNIFLY_SIDED = _re.compile(r'^(Arm|Leg)_(.+)\.([LR])$')


def to_fo4(name):
    """Real FO4 bone name for any spelling (Skyrim-style, raw, or PyNifly .L/.R)."""
    if name in SKYRIM_STYLE_TO_FO4:
        return SKYRIM_STYLE_TO_FO4[name]
    m = _PYNIFLY_SIDED.match(name or '')
    if m:
        return f'{m.group(3)}{m.group(1)}_{m.group(2)}'
    return name


def pynifly_name(name):
    """The name PyNifly gives this bone in Blender with "rename bones" on
    ("LArm_Hand" -> "Arm_Hand.L"); unsided bones keep their name."""
    fo4 = to_fo4(name)
    m = _SIDED.match(fo4 or '')
    return f'{m.group(2)}_{m.group(3)}.{m.group(1)}' if m else fo4


def find_bone(bones, name):
    """Look a bone up in any Blender bone collection (pose.bones, data.bones,
    edit_bones) by any spelling. Returns the bone or None."""
    if bones is None or not name:
        return None
    for candidate in (name, to_fo4(name), pynifly_name(name)):
        b = bones.get(candidate)
        if b is not None:
            return b
    return None


def find_bone_name(bone_names, name):
    """Same as find_bone() for a plain collection of names; returns the name present or None."""
    names = set(bone_names or ())
    for candidate in (name, to_fo4(name), pynifly_name(name)):
        if candidate in names:
            return candidate
    return None


# Bones every FO4 character skeleton has (raw names); used to recognise a
# character rig in either spelling.
_CHARACTER_MARKERS = frozenset({"COM", "Pelvis", "SPINE1", "SPINE2", "HEAD",
                                "LArm_Hand", "RArm_Hand", "LLeg_Thigh", "RLeg_Thigh"})


def is_character_skeleton(bone_names):
    """True for a FO4 humanoid character rig (raw or PyNifly names). The
    old test was "any bone starts with 'NPC '", which no FO4 skeleton has."""
    found = {to_fo4(n) for n in (bone_names or ())}
    return len(found & _CHARACTER_MARKERS) >= 3 or any(str(n).startswith("NPC ") for n in (bone_names or ()))


# Kept for callers that still test for the old prefix; FO4 bones never have it.
NPC_PREFIX = "NPC "

# All canonical bone names as a frozenset (for O(1) membership tests)
NPC_ALL = frozenset(NPC.values())

# Root bone(s) -- the true skeleton root
NPC_ROOTS = frozenset({"Root"})

# Bones-per-skinned-shape guidance. NOT a hard engine limit: vanilla meshes
# go well past 80 (measured 2026-10-27 over 669 vanilla skinned shapes: 20 use
# 80 or more, Meshes/Clothes/Irma/MOutfit.nif uses 141). Kept as a soft
# warning level for performance, never as a reason to drop bones.
FO4_MAX_DEFORM_BONES_PER_MESH = 80
FO4_VANILLA_MAX_BONES_SEEN = 141

# ---------------------------------------------------------------------------
# Power Armor skeleton — separate from the regular NPC skeleton.
# PA uses HumanRacePowerArmor.nif; bone names are identical to NPC but the
# skeleton HKX and behaviour graph paths differ.
# ---------------------------------------------------------------------------
PA_SKELETON_SUBPATH = "Actors/PowerArmor"

# ---------------------------------------------------------------------------
# Creature / flora conventions:
#   - No "NPC " prefix
#   - Species-specific skeleton HKX under Actors/<creature>/
#   - Carnivorous plants use a creature-style rig; simple foliage uses
#     Wind vertex groups only (no skeleton)
# ---------------------------------------------------------------------------
CREATURE_NO_PREFIX = True   # reminder: creature bones must NOT start with "NPC "

# ---------------------------------------------------------------------------
# Weapon bone names (generic rig — 3rd-person world model)
#
# Verified against real vanilla weapon NIFs (10mmPistol.nif, CombatShotgun.nif)
# via PyNifly: the real root node is literally named "WEAPON" (not "Weapon"/
# "WeaponRoot"), the muzzle/projectile-origin node is "ProjectileNode" (not
# "Muzzle"), and TRIGGER/MAGAZINE/BOLT carry the "Weapon" prefix in the real
# files. GRIP/BARREL/HAMMER have no confirmed real NIF node equivalent --
# real weapons animate per-part shapes (e.g. "Pistol10mmHammer:0") as
# independently keyframed rigid nodes, not named joints on a shared rig --
# so these three remain Blender-only authoring aids, not real node names.
# ---------------------------------------------------------------------------
WEAPON = {
    "ROOT":     "WEAPON",
    "GRIP":     "Grip",
    "TRIGGER":  "WeaponTrigger",
    "BARREL":   "Barrel",
    "MUZZLE":   "ProjectileNode",
    "MAGAZINE": "WeaponMagazine",
    "BOLT":     "WeaponBolt",
    "HAMMER":   "Hammer",
    "SCOPE":    "WeaponOptics1",
}

# 1st-person arm skeleton attachment bones
WEAPON_1P = {
    "WEAPON":       "WEAPON",
    "ANIM_OBJ_A":  "AnimObjectA",
    "ANIM_OBJ_B":  "AnimObjectB",
}
