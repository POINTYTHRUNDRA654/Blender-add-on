"""
merged_structure_fix.py
=======================
Added 2026-11-23 (Billy's Glowing Sea muck pools: GSMuckPool_M01 / S01, GSMuckPool_M01BluffLarge01,
GSMuckPoolMoundFull03 -- collision and wind problems after merging meshes, adding collision, leaf cards
and wind).

Post-export repair of three layouts that no vanilla mesh has (survey of 1,799 vanilla Landscape NIFs incl.
DLC03, and the 17,153-mesh scan already recorded in fo4_game_rules._raw_structure_findings):

1. Collision present but the root's BSXFlags lack the Havok bit (2).  Vanilla: every file with collision
   has it (130 / 138 / 162 / 643 ...), none has 0.  Billy's files had BSXFlags = 0 with collision, because
   the fresh-BSXFlags patch never overwrites an existing block and re-export restores the old value.
   Fix: flags |= 130 (Havok + Articulated, the vanilla value for collision statics), other bits kept.
2. BSXFlags on nodes other than the root (left by merging whole NIFs).  Vanilla: 0 of 17,153.
   Fix: unlinked from those nodes (the game reads only the root's); their Havok bit is folded into the root.
3. A wind node (BSLeafAnimNode) below the root.  Vanilla: 0 of 17,153 (231 Landscape files have it as the
   root).  Fix: the inner ones become plain NiNodes and the root becomes the BSLeafAnimNode.

Nothing else is touched, and a file without any of these patterns is not rewritten at all -- so every earlier
regression case (vanilla re-exports, single plants, trees) passes through byte for byte.
A plain NiNode root over wind materials is NOT changed: 56 vanilla Landscape files have exactly that.
"""
import struct

try:
    from .nif_blocks import Nif, NODE_TYPES
except ImportError:  # standalone use (tests, scripts)
    from nif_blocks import Nif, NODE_TYPES

HAVOK_BIT = 2
COLLISION_BSX = 130


def _bsx_value(nif, i):
    return struct.unpack_from("<I", nif.blocks[i][1], 4)[0]


def _set_bsx_value(nif, i, v):
    struct.pack_into("<I", nif.blocks[i][1], 4, v)


def fix_merged_structure(path: str, dry_run: bool = False) -> dict:
    """Repair the three layouts above in the NIF at *path* (in place unless dry_run). Returns a report."""
    report = {"changed": [], "warnings": []}
    data = open(path, "rb").read()
    try:
        nif = Nif(data)
    except Exception as exc:
        report["warnings"].append(f"could not read the file: {exc}")
        return report
    if not nif.roots:
        return report
    root = nif.roots[0]
    if nif.blocks[root][0] not in NODE_TYPES:
        return report
    strings = nif.strings()

    def name_of(i):
        k = struct.unpack_from("<i", nif.blocks[i][1], 0)[0]
        return strings[k] if 0 <= k < len(strings) else f"block {i}"

    has_collision = any(t == "bhkNPCollisionObject" for t, _ in nif.blocks)

    # 2 + 3: inner BSXFlags and inner wind nodes
    inner_flags = 0
    inner_leaf = []
    for i, (t, _d) in enumerate(nif.blocks):
        if i == root or t not in NODE_TYPES:
            continue
        f = nif.node(i)
        bsx = [e for e in f["extras"] if 0 <= e < len(nif.blocks) and nif.blocks[e][0] == "BSXFlags"]
        if bsx:
            for e in bsx:
                inner_flags |= _bsx_value(nif, e)
            f["extras"] = [e for e in f["extras"] if e not in bsx]
            nif.set_node(i, f)
            report["changed"].append(f"removed BSXFlags from inner node '{name_of(i)}'")
        if t == "BSLeafAnimNode":
            nif.set_type(i, "NiNode")
            inner_leaf.append(name_of(i))
    if inner_leaf:
        report["changed"].append(f"inner wind node(s) made plain: {', '.join(inner_leaf[:4])}")
        if nif.blocks[root][0] in ("NiNode", "BSFadeNode"):
            nif.set_type(root, "BSLeafAnimNode")
            report["changed"].append("root made the wind node (BSLeafAnimNode)")

    # 1: Havok bit on the root's BSXFlags
    rf = nif.node(root)
    root_bsx = [e for e in rf["extras"] if 0 <= e < len(nif.blocks) and nif.blocks[e][0] == "BSXFlags"]
    if root_bsx:
        e = root_bsx[0]
        old = _bsx_value(nif, e)
        new = old
        if has_collision and not (old & HAVOK_BIT):
            new |= COLLISION_BSX
        if inner_flags & HAVOK_BIT and has_collision:
            new |= HAVOK_BIT
        if new != old:
            _set_bsx_value(nif, e, new)
            report["changed"].append(f"root BSXFlags {old} -> {new} (file has collision)")
    elif has_collision:
        report["warnings"].append("the file has collision but its root has no BSXFlags; add one (value 130)")

    if report["changed"] and not dry_run:
        open(path, "wb").write(nif.to_bytes())
    return report
