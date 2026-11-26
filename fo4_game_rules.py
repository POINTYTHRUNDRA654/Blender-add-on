"""Check materials, textures and exported NIFs against how the real game does it.

Every rule here comes from a read-only scan of the unpacked vanilla game
(see fo4_game_rules_data.SCAN): 9,024 BGSM/BGEM, 136,789 mesh shapes and the
22,266 textures they use. Each finding says what vanilla does and how many
files back it, so the modder can judge it; nothing here edits a file.

Severities: 'ERROR' = breaks or crashes in game / CK, 'WARN' = vanilla never
(or almost never) does this, 'INFO' = unusual for this kind of object.
"""
import os
import struct

try:
    from . import fo4_game_rules_data as _D
except ImportError:  # loaded outside the add-on package (tests, scripts)
    import fo4_game_rules_data as _D

ERROR, WARN, INFO = "ERROR", "WARN", "INFO"


def _on(data, name):
    return bool(getattr(data, name, False))


def _tex(data, name):
    return (getattr(data, name, "") or "").strip()


# ── 1. Material (BGSM) rules ─────────────────────────────────────────────────

def check_bgsm(data, label=""):
    """Feature/texture combinations vanilla always (or never) uses.

    *data* is a bgsm_helpers.BGSMData. Returns [(severity, message)].
    """
    out = []
    p = f"{label}: " if label else ""
    F = _D.BGSM_FEATURE
    if _on(data, "glowmap") and not _tex(data, "glow_texture"):
        out.append((ERROR, p + "Glow map is on but no glow texture is set. Every vanilla glow-map material "
                    f"has one ({F['glowmap']['n']} of {F['glowmap']['n']}); an empty glow slot has crashed the game "
                    "on cell load. Add a _g texture or turn the glow map off."))
    if _tex(data, "glow_texture") and not _on(data, "glowmap"):
        out.append((INFO, p + "A glow texture is set but the glow map is off, so it will not glow "
                    "(vanilla leaves it like that in 74 of 346 materials with a glow texture)."))
    if _on(data, "environment_mapping") and not _tex(data, "env_map_texture"):
        out.append((INFO, p + "Reflections (environment mapping) are on but no cube map is set; 3,001 of "
                    "3,030 vanilla materials with reflections name one (mostly "
                    "Shared\\Cubemaps\\mipblur_DefaultOutside1.dds)."))
    if _on(data, "grayscale_to_palette_color") and not _tex(data, "greyscale_texture"):
        out.append((ERROR, p + "Greyscale-to-palette colour is on but no palette (greyscale) texture is set. "
                    "Vanilla always has one (478 of 478)."))
    if _tex(data, "greyscale_texture") and not _on(data, "grayscale_to_palette_color"):
        out.append((INFO, p + "A greyscale (palette) texture is set but palette colouring is off. In vanilla "
                    "478 of 487 materials with a greyscale texture use it as a palette; it is not the glow slot."))
    if _on(data, "back_lighting") and not (_on(data, "two_sided") and _on(data, "alpha_test")):
        out.append((WARN, p + "Back lighting without two-sided + alpha test: 52 of 53 vanilla back-lit "
                    "materials are two-sided alpha-tested foliage."))
    if _on(data, "skin_tint") and not _on(data, "subsurface_lighting"):
        out.append((WARN, p + "Skin tint without subsurface lighting: all 27 vanilla skin-tint materials "
                    "have subsurface lighting."))
    if _on(data, "tree"):
        if _on(data, "environment_mapping"):
            out.append((WARN, p + "Tree (wind) material with reflections: no vanilla wind material has "
                        "environment mapping (0 of 93)."))
        if not _on(data, "alpha_test"):
            out.append((INFO, p + "Tree (wind) material without alpha test: 91 of 93 vanilla wind "
                        "materials are alpha-tested."))
    if _on(data, "decal_no_fade") and not _on(data, "decal"):
        out.append((INFO, p + "Decal-no-fade is on but decal is off; vanilla has both in 366 of 371."))
    if _on(data, "alpha_test"):
        ref = int(getattr(data, "alpha_test_ref", 128) or 0)
        if ref >= 250:
            out.append((WARN, p + f"Alpha test reference {ref} cuts away almost every pixel (the mesh "
                        "shows as an outline only); only right if it should be hidden (6 of 2,456 vanilla "
                        "materials, the 'turned off' ones). Vanilla uses 127/128 most, 90 for hair, 100 for grass."))
        elif ref <= 0:
            out.append((INFO, p + "Alpha test reference 0 keeps every pixel (no cut-out)."))
    if _on(data, "alpha_blend_enabled") and _on(data, "two_sided"):
        out.append((INFO, p + "Alpha blend together with two-sided: vanilla rarely combines them "
                    "(8 of 429 blended materials); faces can sort wrongly."))
    if not _tex(data, "diffuse_texture"):
        out.append((INFO, p + "No diffuse texture (8,278 of 8,336 vanilla materials have one)."))
    if not _tex(data, "normal_texture"):
        out.append((INFO, p + "No normal map (8,126 of 8,336 vanilla materials have one)."))
    if not _tex(data, "smooth_spec_texture") and _on(data, "environment_mapping"):
        out.append((INFO, p + "Reflections without a specular (_s) map (2,990 of 3,030 vanilla reflective "
                    "materials have one): reflection strength comes from the _s map, so without it the whole "
                    "surface reflects evenly."))
    return out


def material_category(bgsm_path):
    """'landscape\\plants' style key from a Materials\\... path ('' if none)."""
    parts = [x for x in str(bgsm_path).replace("/", "\\").split("\\") if x]
    low = [x.lower() for x in parts]
    if "materials" not in low:
        return ""
    parts = parts[len(low) - 1 - low[::-1].index("materials") + 1:]
    if parts and parts[0].lower().startswith("dlc0"):
        parts = parts[1:]
    if len(parts) > 2:
        return "\\".join(parts[:2]).lower()
    return parts[0].lower() if len(parts) > 1 else ""


_PROFILE_FIELDS = ("two_sided", "alpha_test", "alpha_blend_enabled", "decal", "glowmap", "environment_mapping",
                   "tree", "subsurface_lighting", "back_lighting", "rim_lighting", "non_occluder",
                   "grayscale_to_palette_color", "hair", "skin_tint", "model_space_normals")


def check_category(data, bgsm_path, label=""):
    """Compare a material with vanilla materials in the same Materials folder
    (only clear-cut cases: >= 90% of them do it, or < 5% do)."""
    cat = material_category(bgsm_path)
    prof = _D.BGSM_CATEGORY.get(cat)
    if not prof or prof["n"] < 30:
        return []
    p = f"{label}: " if label else ""
    out = []
    for f in _PROFILE_FIELDS:
        share = prof["on"].get(f, 0.0)
        if _on(data, f) and share < 0.05:
            out.append((INFO, p + f"{f.replace('_', ' ')} is on, but under Materials\\{cat} it is rare in "
                        f"vanilla ({int(round(share * 100))}% of {prof['n']})."))
        elif not _on(data, f) and share >= 0.9:
            out.append((INFO, p + f"{f.replace('_', ' ')} is off, but {int(round(share * 100))}% of the "
                        f"{prof['n']} vanilla materials under Materials\\{cat} have it on."))
    return out


def category_summary(bgsm_path):
    """One line describing vanilla materials of this folder, or ''."""
    cat = material_category(bgsm_path)
    prof = _D.BGSM_CATEGORY.get(cat)
    if not prof:
        return ""
    on = ", ".join(f"{k.replace('_', ' ')} {int(round(v * 100))}%" for k, v in sorted(prof["on"].items(), key=lambda kv: -kv[1])
                   if k not in ("cast_shadows", "z_buffer_test", "z_buffer_write", "specular_enabled"))
    med = prof["median"]
    return (f"Vanilla Materials\\{cat} ({prof['n']}): {on or 'plain'}; alpha ref {med.get('alpha_test_ref')}, "
            f"smoothness {med.get('smoothness')}, specular {med.get('specular_mult')}")


# ── 2. Texture (DDS) rules ───────────────────────────────────────────────────

_FOURCC = {b"DXT1": "BC1", b"DXT3": "BC2", b"DXT5": "BC3", b"ATI1": "BC4", b"BC4U": "BC4", b"ATI2": "BC5", b"BC5U": "BC5"}
_DXGI = {28: "RGBA8", 29: "RGBA8 sRGB", 71: "BC1", 72: "BC1 sRGB", 74: "BC2", 77: "BC3", 78: "BC3 sRGB", 80: "BC4",
         83: "BC5", 84: "BC5 signed", 95: "BC6H", 98: "BC7", 99: "BC7 sRGB", 87: "BGRA8", 91: "BGRA8 sRGB"}


def dds_info(path):
    """{'fmt', 'w', 'h', 'mips', 'full_mips', 'cube'} from a DDS header, or None."""
    try:
        with open(path, "rb") as f:
            b = f.read(148)
    except OSError:
        return None
    if len(b) < 128 or b[:4] != b"DDS ":
        return None
    h, w = struct.unpack_from("<II", b, 12)
    mips = struct.unpack_from("<I", b, 28)[0] or 1
    pf_flags = struct.unpack_from("<I", b, 80)[0]
    cc = b[84:88]
    caps2 = struct.unpack_from("<I", b, 112)[0]
    if pf_flags & 0x4:
        if cc == b"DX10" and len(b) >= 132:
            code = struct.unpack_from("<I", b, 128)[0]
            fmt = _DXGI.get(code, f"DXGI {code}")
        else:
            fmt = _FOURCC.get(cc, cc.decode("latin-1", "replace"))
    else:
        fmt = "uncompressed"
    return {"fmt": fmt, "w": w, "h": h, "mips": mips, "full_mips": mips >= max(w, h).bit_length(),
            "cube": bool(caps2 & 0x200)}


_SLOT_NAMES = {"diffuse_texture": "diffuse (_d)", "normal_texture": "normal (_n)",
               "smooth_spec_texture": "specular/smoothness (_s)", "glow_texture": "glow (_g)",
               "greyscale_texture": "palette (greyscale)", "env_map_texture": "cube map (_e)",
               "wrinkle_mask_texture": "wrinkle mask"}
_EXPECT = {"diffuse_texture": {"BC1", "BC1 sRGB", "BC3", "BC3 sRGB", "BC7", "BC7 sRGB"},
           "normal_texture": {"BC5", "BC5 signed", "BC7"},
           "smooth_spec_texture": {"BC5", "BC7"},
           "glow_texture": {"BC1", "BC1 sRGB", "BC3", "BC3 sRGB", "BC7", "BC7 sRGB"}}


def _typical(slot):
    v = _D.TEXTURE_SLOT.get(slot)
    if not v:
        return ""
    top = ", ".join(f"{f} {n}" for f, n in v["formats"][:2])
    return f"vanilla: {top} of {v['n']}"


def check_texture(path, slot, label=""):
    """Format/size/mips/cube checks for one texture file on disk."""
    p = f"{label}: " if label else ""
    info = dds_info(path)
    name = os.path.basename(path)
    nice = _SLOT_NAMES.get(slot, slot)
    if info is None:
        if os.path.isfile(path) and not path.lower().endswith(".dds"):
            return [(WARN, p + f"{nice} {name} is not a DDS file; FO4 only loads DDS.")]
        return []
    out = []
    w, h = info["w"], info["h"]
    if w & (w - 1) or h & (h - 1):
        out.append((WARN, p + f"{nice} {name} is {w}x{h}: not a power of two (vanilla textures always are)."))
    if not info["full_mips"] and not info["cube"]:
        out.append((WARN, p + f"{nice} {name} has {info['mips']} mip level(s), not a full chain; 99% of vanilla "
                    "textures have full mips (no mips = shimmering and slow at a distance)."))
    exp = _EXPECT.get(slot)
    if exp and info["fmt"] not in exp:
        sev = INFO
        extra = ""
        if slot == "normal_texture":
            extra = " The game reads only the red and green channels of a normal map; BC5 keeps them sharp."
        if slot == "smooth_spec_texture":
            extra = " Red = specular, green = smoothness; BC5 stores exactly those two."
        out.append((sev, p + f"{nice} {name} is {info['fmt']} ({_typical(slot)}).{extra}"))
    if slot == "env_map_texture" and not info["cube"]:
        out.append((ERROR, p + f"cube map slot holds {name}, which is not a cube map (vanilla: 96-100% cube maps)."))
    if slot == "greyscale_texture" and max(w, h) > 512:
        out.append((INFO, p + f"palette {name} is {w}x{h}; vanilla palettes are small (16-256 px, uncompressed)."))
    return out


def resolve_texture(ref, roots):
    """A BGSM/NIF texture reference -> existing file under one of *roots*, or ''."""
    ref = (ref or "").replace("/", "\\").strip()
    if not ref:
        return ""
    if os.path.isabs(ref) and os.path.isfile(ref):
        return ref
    low = ref.lower()
    i = low.find("textures\\")
    rel = ref[i:] if i >= 0 else os.path.join("Textures", ref)
    for r in roots:
        if r:
            cand = os.path.join(r, rel)
            if os.path.isfile(cand):
                return cand
    return ""


def check_bgsm_textures(data, roots, label=""):
    out = []
    for slot in ("diffuse_texture", "normal_texture", "smooth_spec_texture", "glow_texture", "greyscale_texture",
                 "env_map_texture"):
        f = resolve_texture(_tex(data, slot), roots)
        if f:
            out += check_texture(f, slot, label)
    return out


# ── 3. Exported NIF vs its BGSM ──────────────────────────────────────────────


def _pyn():
    try:
        from io_scene_nifly.pyn import pynifly
        from io_scene_nifly.pyn.nifconstants import ShaderFlags1FO4, ShaderFlags2FO4
        return pynifly, ShaderFlags1FO4, ShaderFlags2FO4
    except Exception:
        return None, None, None


def _raw_shape_flags(nif_path):
    """{shape name: (shader flags 1, shader flags 2, has vertex colours)} read
    from the file's own bytes. PyNifly fills a shader's properties from its
    BGSM when it finds one (GSFungusStock04, 2026-11-13: the file said
    Tree_Anim on, PyNifly reported the BGSM's Tree off), so comparing PyNifly's
    flags with the BGSM compared the material with itself and never found a
    mismatch. Returns {} for anything that is not a FO4 (BS stream 130) NIF."""
    try:
        try:
            from . import nif_roundtrip as _nrt
        except ImportError:
            import nif_roundtrip as _nrt
        with open(nif_path, "rb") as fh:
            data = fh.read()
        o = data.index(b'\n') + 1
        if struct.unpack_from('<I', data, o + 13)[0] != 130:
            return {}
        strings = _nrt._header_strings(data)
        types, offsets, _sizes = _nrt._block_layout(data)
    except Exception:
        return {}
    out = {}
    for t, off in zip(types, offsets):
        if t not in ("BSTriShape", "BSMeshLODTriShape", "BSSubIndexTriShape", "BSDynamicTriShape"):
            continue
        try:
            name_i, n_extra = struct.unpack_from('<iI', data, off)
            q = off + 8 + 4 * n_extra + 4 + 4 + 52 + 4 + 16     # NiAVObject + bounding sphere
            _skin, shader, _alpha = struct.unpack_from('<iii', data, q)
            vdesc = struct.unpack_from('<Q', data, q + 12)[0]
            if not (0 <= shader < len(types)) or types[shader] != "BSLightingShaderProperty":
                continue
            s_off = offsets[shader]
            s_extra = struct.unpack_from('<I', data, s_off + 8)[0]
            f1, f2 = struct.unpack_from('<II', data, s_off + 16 + 4 * s_extra)
            name = strings[name_i] if 0 <= name_i < len(strings) else ""
            out[name] = (f1, f2, bool(((vdesc >> 44) & 0xFFF) & 0x20))
        except struct.error:
            continue
    return out


def _raw_structure_findings(nif_path):
    """Node layout checks read from the file's bytes (FO4 only).

    Scan of 17,153 vanilla meshes (Landscape, SetDressing, Architecture, all
    DLC; 2026-11-13): BSXFlags on a node other than the root 0 times, a
    BSLeafAnimNode (wind node) other than the root 0 times, collision on a
    scaled child node once in 14,604 (a root). Billy's GSFungusStock04 had
    BSXFlags and its main collision on an inner node under a new wind root
    (walk-through collision in game) and GlowingSeaAshMound01, assembled from
    several meshes, had 3 inner BSXFlags and 4 nested wind nodes (froze the
    game; which of them did it is not proven)."""
    out = []
    try:
        try:
            from . import nif_roundtrip as _nrt
        except ImportError:
            import nif_roundtrip as _nrt
        with open(nif_path, "rb") as fh:
            data = fh.read()
        o = data.index(b'\n') + 1
        if struct.unpack_from('<I', data, o + 13)[0] != 130:
            return out
        strings = _nrt._header_strings(data)
        types, offsets, _sizes = _nrt._block_layout(data)
    except Exception:
        return out
    nodes = ("NiNode", "BSFadeNode", "BSLeafAnimNode", "BSOrderedNode", "BSMultiBoundNode")
    inner_bsx, inner_leaf, root_bsx, any_coll = [], [], False, False
    for i, (t, off) in enumerate(zip(types, offsets)):
        if t not in nodes:
            continue
        try:
            name_i, ne = struct.unpack_from('<iI', data, off)
            extras = struct.unpack_from(f'<{ne}i', data, off + 8)
            coll = struct.unpack_from('<i', data, off + 8 + 4 * ne + 8 + 52)[0]
        except struct.error:
            continue
        name = strings[name_i] if 0 <= name_i < len(strings) else f"block {i}"
        has_bsx = any(0 <= e < len(types) and types[e] == "BSXFlags" for e in extras)
        any_coll = any_coll or coll >= 0
        if i == 0:
            root_bsx = has_bsx
            continue
        if has_bsx:
            inner_bsx.append(name)
        if t == "BSLeafAnimNode":
            inner_leaf.append(name)
    if inner_bsx:
        out.append((WARN, f"BSXFlags on node(s) other than the root: {', '.join(inner_bsx[:4])}. The game reads "
                    "BSXFlags from the root; no vanilla mesh (0 of 17,153) has it anywhere else. Seen with "
                    "collision you can walk through and with a mesh that froze the game."))
    if any_coll and not root_bsx:
        out.append((WARN, "The file has collision but the root has no BSXFlags (every vanilla tree with "
                    "collision has BSXFlags on its root), so the game may not use the collision."))
    if inner_leaf:
        out.append((WARN, f"Wind node (BSLeafAnimNode) below the root: {', '.join(inner_leaf[:4])}. No vanilla "
                    "mesh (0 of 17,153) nests one; make the root the wind node and these plain NiNodes."))
    # 2026-11-23: root BSXFlags present but without the Havok bit while the file has collision (Billy's
    # muck pools had 0). Every one of 1,799 vanilla Landscape files with collision has the bit (130/138/162...).
    if any_coll and root_bsx:
        try:
            r_off = offsets[0]
            r_name, r_ne = struct.unpack_from('<iI', data, r_off)
            for e in struct.unpack_from(f'<{r_ne}i', data, r_off + 8):
                if 0 <= e < len(types) and types[e] == "BSXFlags":
                    v = struct.unpack_from('<I', data, offsets[e] + 4)[0]
                    if not v & 2:
                        out.append((WARN, f"The file has collision but the root's BSXFlags is {v}, without the Havok "
                                    "bit, so the game ignores the collision (every vanilla file with collision has "
                                    "it, e.g. 130). Exporting again with the add-on sets it."))
        except struct.error:
            pass
    return out


def data_roots_for(path):
    """Data folders to resolve Materials/Textures against for a file inside
    ...\\Data\\Meshes or ...\\Data\\Materials, plus the game Data folder."""
    roots = []
    parts = os.path.normpath(str(path)).split(os.sep)
    low = [x.lower() for x in parts]
    for key in ("meshes", "materials", "textures"):
        if key in low:
            i = len(low) - 1 - low[::-1].index(key)
            roots.append(os.sep.join(parts[:i]) or os.sep)
    try:
        from . import bgsm_helpers as _bh
        g = _bh._game_data_root()
        if g:
            roots.append(g)
    except Exception:
        pass
    seen, out = set(), []
    for r in roots:
        if r and r.lower() not in seen:
            seen.add(r.lower())
            out.append(r)
    return out


def check_nif(nif_path, read_bgsm=None):
    """Shader flags vs BGSM, wind flag vs vertex colours, for an exported NIF.

    *read_bgsm(path)* returns a BGSMData (bgsm_helpers.read_bgsm on the file);
    when given, each shape's BGSM is also checked with check_bgsm,
    check_category and its textures.
    """
    pyn, SF1, SF2 = _pyn()
    if pyn is None or not os.path.isfile(nif_path):
        return []
    if os.path.getsize(nif_path) == 0:
        return [(ERROR, f"{os.path.basename(nif_path)} is EMPTY (0 bytes): the file was not written. "
                 "Check that the drive has free space and the file is not open in another program.")]
    try:
        nif = pyn.NifFile(str(nif_path))
    except Exception as exc:
        return [(ERROR, f"{os.path.basename(nif_path)} could not be opened as a NIF ({exc}).")]
    roots = data_roots_for(nif_path)
    raw = _raw_shape_flags(nif_path)
    out, seen_mats = _raw_structure_findings(nif_path), set()
    for s in nif.shapes:
        sh = s.shader
        if sh is None:
            continue
        label = s.name
        if label in raw:
            f1, f2, has_vc = raw[label]
        else:
            pr = sh.properties
            f1, f2 = int(getattr(pr, "Shader_Flags_1", 0)), int(getattr(pr, "Shader_Flags_2", 0))
            has_vc = bool(getattr(s.properties, "hasVertexColors", 0))
        if sh.blockname == "BSLightingShaderProperty" and SF2 is not None and f2 & SF2.TREE_ANIM and not has_vc:
            out.append((WARN, f"{label}: wind (Tree_Anim) is on but the shape has no vertex colours, so "
                        "nothing tells the wind how far each vertex moves. 94% of vanilla wind shapes "
                        "(1,278 of 1,359) have vertex colours."))
        mat = (sh.name or "")
        if isinstance(mat, bytes):
            mat = mat.decode("utf-8", "replace")
        if not mat.lower().endswith(".bgsm"):
            continue
        rel = mat.replace("/", "\\")
        i = rel.lower().find("materials\\")
        rel = rel[i:] if i >= 0 else os.path.join("Materials", rel)
        mpath = next((os.path.join(r, rel) for r in roots if os.path.isfile(os.path.join(r, rel))), "")
        if not mpath or read_bgsm is None:
            continue
        try:
            with open(mpath, "rb") as fh:
                bd = read_bgsm(fh.read())
        except Exception:
            continue
        # With a BGSM the game uses the material's flags; the NIF's copy is a
        # leftover that vanilla often lets drift (NIF_VS_BGSM, raw scan), so a
        # difference is only reported where it shows a real conflict: the NIF
        # was set up for wind and the material has Tree off.
        if SF2 is not None and f2 & SF2.TREE_ANIM and not _on(bd, "tree"):
            st = _D.NIF_VS_BGSM.get("TREE_ANIM", {})
            out.append((WARN, f"{label}: the NIF has wind (Tree_Anim) on but {os.path.basename(mpath)} has Tree "
                        f"off. The game uses the material, so this shape does not sway. Vanilla has this in "
                        f"{st.get('nif_only', 2)} of {st.get('pairs', 1099964):,} shape/material pairs."))
        # Vertex alpha is wind sway only when the material's Tree flag is on;
        # otherwise the game multiplies it into the texture alpha, and with
        # alpha test on every pixel under the cut disappears. Seen in game
        # 2026-11-13: a fused one-piece tree with wind weights in vertex alpha
        # and a Tree-off material lost its whole trunk (6,260 of 15,500
        # vertices under the cut) while the canopy floated.
        # Only where the NIF says wind: vanilla also cuts shapes away with
        # vertex alpha on purpose (hairlines, Diamond City edge trims, cliff
        # edges), which must not be reported. The Blender-side check catches
        # a mesh with a Wind group and a Tree-off material before export.
        # A shape with the NIF's Vertex_Alpha flag (SLSF1 bit 3) asks for
        # vertex-alpha transparency on purpose: vanilla Thistle01.nif's
        # harvested state (Thistle02Picked) hides 427 of 437 vertices that way.
        if (has_vc and SF2 is not None and f2 & SF2.TREE_ANIM and not f1 & (1 << 3)
                and not _on(bd, "tree") and _on(bd, "alpha_test")):
            try:
                cols = s.colors or []
            except Exception:
                cols = []
            ref = int(getattr(bd, "alpha_test_ref", 128) or 0)
            n_cut = sum(1 for c in cols if len(c) > 3 and c[3] < ref / 255.0)
            if n_cut:
                out.append((ERROR,
                            f"{label}: {n_cut} of {len(cols)} vertices ({100.0 * n_cut / len(cols):.0f}%) have "
                            f"vertex alpha (the wind sway) under the alpha-test cut ({ref}) and "
                            f"{os.path.basename(mpath)} has Tree off, so the game reads that alpha as "
                            "transparency: those parts are INVISIBLE in game. Give the shape a material with "
                            "Tree on (its own _Wind.bgsm if the material is shared with other meshes)."))
        if mpath.lower() not in seen_mats:
            seen_mats.add(mpath.lower())
            mlabel = os.path.basename(mpath)
            out += check_bgsm(bd, mlabel)
            out += check_category(bd, mpath, mlabel)
            out += check_bgsm_textures(bd, roots, mlabel)
    return out


def summarize(findings, limit=4):
    """Short text for an export message: counts + the most serious findings."""
    if not findings:
        return ""
    order = {ERROR: 0, WARN: 1, INFO: 2}
    fs = sorted(findings, key=lambda f: order.get(f[0], 3))
    n = {k: sum(1 for f in fs if f[0] == k) for k in (ERROR, WARN, INFO)}
    head = ", ".join(f"{v} {k.lower()}{'s' if v != 1 else ''}" for k, v in n.items() if v)
    shown = " | ".join(f"{s}: {m}" for s, m in fs[:limit] if s != INFO) or " | ".join(f"{s}: {m}" for s, m in fs[:limit])
    return f"Game check ({head}): {shown}" + (" (more in the console)" if len(fs) > limit else "")
