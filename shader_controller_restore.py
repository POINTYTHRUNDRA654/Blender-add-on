"""Put a source NIF's shader animations back after re-export (added 2026-11-23).

Why: GSMuckPool_S01 (muck pool water + top muck) has 7 BSEffectShaderPropertyFloatController blocks -- UV scroll
and glow pulse. PyNifly imports them as ONE action with a slot per material and, on export, skips a shader action
that has more than one slot, so a re-exported pool lost all its water animation. This copies each shape's
controller chain (controllers, interpolators, key data) from the source NIF onto the shape of the same name in the
exported NIF, when that shape's shader has no controller yet, and sets the root BSXFlags Animated bit (bit 0) --
637 of 669 vanilla non-actor meshes whose only animation is shader controllers have it on (2026-11-23 scan).

Only the block types below are copied; a chain with anything else is skipped with a warning, so the file is never
written with a block whose links were not remapped. Block layouts checked against GSMuckPool_S01.nif:
controller 34 bytes (next @0, target @22, interpolator @26), NiFloatInterpolator 8 bytes (data @4)."""
import struct

from .nif_blocks import Nif, SHAPE_TYPES

# block type -> offsets of its block links
_CTRL = ("BSEffectShaderPropertyFloatController", "BSEffectShaderPropertyColorController",
         "BSLightingShaderPropertyFloatController", "BSLightingShaderPropertyColorController")
_LINKS = {t: (0, 22, 26) for t in _CTRL}                 # next controller, target, interpolator
_LINKS.update({"NiFloatInterpolator": (4,),              # value f32, data
               "NiPoint3Interpolator": (12,),            # value 3 x f32, data
               "NiFloatData": (), "NiPosData": ()})
_SHADERS = ("BSEffectShaderProperty", "BSLightingShaderProperty")


def _shader_ctrl_offset(nif, i):
    t, d = nif.blocks[i]
    base = 8 if t == "BSEffectShaderProperty" else 12      # Lighting: shader type u32 first
    ne = struct.unpack_from("<I", d, base - 4)[0]
    return base + 4 * ne


def _shapes_by_name(nif):
    """{shape name: shader block index} for shapes whose shader is an effect/lighting shader."""
    names = nif.strings()
    out = {}
    for i, (t, d) in enumerate(nif.blocks):
        if t not in SHAPE_TYPES:
            continue
        try:
            sh = struct.unpack_from("<i", d, nif.ref_offsets(i)[-2])[0]
            si = struct.unpack_from("<i", d, 0)[0]
        except Exception:
            continue
        if 0 <= sh < len(nif.blocks) and nif.blocks[sh][0] in _SHADERS and 0 <= si < len(names):
            out.setdefault(names[si], sh)
    return out


def _chain(nif, shader):
    """Block indices of the shader's controller chain, or (None, reason)."""
    first = struct.unpack_from("<i", nif.blocks[shader][1], _shader_ctrl_offset(nif, shader))[0]
    if first < 0:
        return [], ""
    seen, todo = [], [first]
    while todo:
        b = todo.pop()
        if b < 0 or b in seen:
            continue
        t, d = nif.blocks[b]
        if t not in _LINKS:
            return None, f"{t} in the chain is not handled"
        seen.append(b)
        for off in _LINKS[t]:
            r = struct.unpack_from("<i", d, off)[0]
            if t in _CTRL and off == 22:
                continue                                   # target: the shader itself
            todo.append(r)
    return sorted(seen), ""


def restore_shader_controllers(exported_path, source_path, dry_run=False):
    """Copy shader controller chains from *source_path* to *exported_path*. Returns
    {'changed': [...], 'warnings': [...]}; writes only when something was copied."""
    rep = {"changed": [], "warnings": []}
    src = Nif(open(source_path, "rb").read())
    dst = Nif(open(exported_path, "rb").read())
    s_shapes, d_shapes = _shapes_by_name(src), _shapes_by_name(dst)
    for name, s_sh in s_shapes.items():
        chain, why = _chain(src, s_sh)
        if chain is None:
            rep["warnings"].append(f"{name}: shader animation not copied ({why})")
            continue
        if not chain:
            continue
        d_sh = d_shapes.get(name)
        if d_sh is None:
            rep["warnings"].append(f"{name}: has shader animation in the source but no shape of that name was exported")
            continue
        if dst.blocks[d_sh][0] != src.blocks[s_sh][0]:
            rep["warnings"].append(f"{name}: shader type changed, animation not copied")
            continue
        coff = _shader_ctrl_offset(dst, d_sh)
        if struct.unpack_from("<i", dst.blocks[d_sh][1], coff)[0] >= 0:
            continue                                       # already animated (PyNifly wrote it)
        base = len(dst.blocks)
        m = {s: base + k for k, s in enumerate(chain)}
        first = struct.unpack_from("<i", src.blocks[s_sh][1], _shader_ctrl_offset(src, s_sh))[0]
        for s in chain:
            t, d = src.blocks[s][0], bytearray(src.blocks[s][1])
            for off in _LINKS[t]:
                r = struct.unpack_from("<i", d, off)[0]
                if t in _CTRL and off == 22:
                    struct.pack_into("<i", d, off, d_sh)
                else:
                    struct.pack_into("<i", d, off, m.get(r, -1))
            if t not in dst.types:
                dst.types.append(t)
            dst.blocks.append([t, d])
        struct.pack_into("<i", dst.blocks[d_sh][1], coff, m[first])
        n_ctrl = sum(1 for s in chain if src.blocks[s][0] in _CTRL)
        rep["changed"].append(f"{name}: {n_ctrl} shader animation(s) restored")
    if rep["changed"]:
        try:
            root = dst.roots[0]
            f = dst.node(root)
            for e in f["extras"]:
                if dst.blocks[e][0] == "BSXFlags":
                    v = struct.unpack_from("<I", dst.blocks[e][1], 4)[0]
                    if not v & 1:
                        struct.pack_into("<I", dst.blocks[e][1], 4, v | 1)
                        rep["changed"].append(f"root BSXFlags {v} -> {v | 1} (Animated)")
        except Exception as exc:
            rep["warnings"].append(f"BSXFlags Animated bit not checked: {exc}")
        if not dry_run:
            data = dst.to_bytes()
            Nif(data)                                      # must still parse
            with open(exported_path, "wb") as fh:
                fh.write(data)
    return rep
