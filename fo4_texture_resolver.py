"""
fo4_texture_resolver.py

Make importing a Fallout 4 asset show its real materials/textures — the way the
game (and MO2's virtual file system) does it — even though Blender isn't running
through MO2's VFS.

Fallout 4 meshes that use a BGSM material store *no* texture paths in the NIF;
the NIF just references a ``.bgsm`` material file, and the texture paths live
inside that BGSM.  On top of that, mod assets live in real mod folders
(e.g. ``E:\\Mod.Organizer-3 ...\\Plants\\Textures\\...``), not the game Data
folder.  This module:

  1. Scans the imported NIF for its ``.bgsm`` (and any inline ``.dds``) refs.
  2. Locates the BGSM under the asset's own mod folder (auto-detected from the
     mesh path: the folder that holds Meshes/Textures/Materials) plus any extra
     search roots the caller supplies.
  3. Parses it with bgsm_helpers.read_bgsm to get diffuse/normal/spec/glow.
  4. Resolves each texture the same way and wires it directly onto the material's
     Principled BSDF — by socket, NOT by node name (PyNifly doesn't name nodes
     'Normal', which is why the addon's old node-name lookup failed on normals).

Nothing here modifies geometry or scale.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

try:
    import bpy
except Exception:
    bpy = None

_IMG_EXTS = ['.dds', '.png', '.tga', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp']


# ---------------------------------------------------------------------------
# NIF reference scan (no full parser needed — texture/material paths are plain
# length-prefixed strings in the file)
# ---------------------------------------------------------------------------
def _scan_nif_refs(nif_path):
    try:
        raw = open(nif_path, 'rb').read()
    except Exception:
        return [], []
    bgsm, dds = [], []
    for m in re.findall(rb'[ -~]{4,}', raw):
        t = m.decode('latin1', 'replace').strip()
        low = t.lower()
        # Trim any leading bytes so the ref starts at a resource root token.
        for tok in ('textures\\', 'materials\\', 'textures/', 'materials/'):
            i = low.find(tok)
            if i > 0:
                t = t[i:]
                low = t.lower()
                break
        if low.endswith('.bgsm') or low.endswith('.bgem'):
            if t not in bgsm:
                bgsm.append(t)
        elif low.endswith('.dds'):
            if t not in dds:
                dds.append(t)
    return bgsm, dds


# ---------------------------------------------------------------------------
# Search roots + file lookup
# ---------------------------------------------------------------------------
def _candidate_roots(source_path, extra_roots=None):
    roots = []

    def add(d):
        try:
            d = Path(d)
            # Never search a bare drive/filesystem root (e.g. 'F:\\'): an rglob
            # over that can crawl the whole drive and hang the import for minutes.
            if len(d.parts) <= 1 or str(d) == d.anchor:
                return
            if d.exists() and d.is_dir() and d not in roots:
                roots.append(d)
        except Exception:
            pass

    p = Path(source_path)
    parts = list(p.parts)
    # Mod root = the folder that contains 'Meshes' (siblings: Textures, Materials)
    for i in range(len(parts) - 1, -1, -1):
        if parts[i].lower() == 'meshes':
            add(Path(*parts[:i]))
            break
    add(p.parent)
    for up in list(p.parents)[:6]:
        add(up)
    for r in (extra_roots or []):
        if not r:
            continue
        p_extra = Path(r)
        add(p_extra)
        # If this looks like an MO2 mods parent directory (its immediate children
        # have Textures/, Meshes/, or Materials/ inside them), expand one level so
        # each individual mod folder is added as its own search root.
        # This way setting fo4_mo2_mods_root to the parent 'mods\' folder works
        # without the user having to know which specific mod subfolder to pick.
        try:
            if p_extra.is_dir():
                _expanded = 0
                for sub in p_extra.iterdir():
                    if _expanded >= 50:          # safety cap — never add hundreds of roots
                        break
                    if not sub.is_dir():
                        continue
                    if any((sub / tok).is_dir()
                           for tok in ('Textures', 'Materials', 'Meshes',
                                       'textures', 'materials', 'meshes')):
                        add(sub)
                        _expanded += 1
        except Exception:
            pass
    return roots


def _find_file(ref, roots, deep_roots=None):
    """Resolve a game-relative path (e.g. 'Textures\\Landscape\\x_d.dds') or a
    bare filename to a real file under one of *roots*.  Exact relative match is
    tried first (fast); a bounded name search is the fallback."""
    if not ref:
        return None
    rel = ref.replace('\\', '/').lstrip('/')
    # If the ref carries extra leading segments, also try from the resource token.
    _low = rel.lower()
    for tok in ('textures/', 'materials/'):
        i = _low.find(tok)
        if i > 0:
            rel = rel[i:]
            break
    name = Path(rel).name
    stem = Path(name).stem.lower()
    ext = Path(name).suffix.lower()
    exts = [ext] + [e for e in _IMG_EXTS + ['.bgsm', '.bgem'] if e != ext]

    # NIF texture sets and BGSM files store paths WITHOUT the resource folder
    # ("SetDressing/ParkBench01_d.dds", "Landscape/Plants/Holly.bgsm"); the
    # file itself lives under Textures/ or Materials/ of a data root.
    rels = [rel]
    _low = rel.lower()
    if not (_low.startswith('textures/') or _low.startswith('materials/')):
        rels.append(('materials/' if ext in ('.bgsm', '.bgem') else 'textures/') + rel)

    # 1. exact relative path under each root (handles the normal MO2 mod layout)
    for root in roots:
        for r in rels:
            cand = root / r
            if cand.exists():
                return cand
    rel = rels[-1]
    # 2. same relative path but with an alternate image extension present
    for root in roots:
        relp = Path(rel)
        for e in exts:
            cand = root / relp.with_suffix(e)
            if cand.exists():
                return cand
    # 3. bounded recursive search by stem — ONLY within the Textures/ and
    #    Materials/ subfolders of each root (never the whole root/drive), and
    #    capped at a fixed number of entries so a large FO4 working folder can
    #    never stall the import for minutes.
    _MAX_SCAN = 20000
    _deep = deep_roots if deep_roots is not None else roots
    for root in _deep:
        for sub in ('Textures', 'Materials'):
            base = root / sub
            if not base.is_dir():
                continue
            scanned = 0
            try:
                for f in base.rglob('*'):
                    scanned += 1
                    if scanned > _MAX_SCAN:
                        break
                    if f.suffix.lower() in exts and f.stem.lower() == stem and f.is_file():
                        return f
            except Exception:
                pass
    return None


# ---------------------------------------------------------------------------
# Material wiring (by socket, not node name)
# ---------------------------------------------------------------------------
def _load_image(path):
    try:
        return bpy.data.images.load(str(path), check_existing=True)
    except Exception as exc:
        print(f"[FO4Tex] could not load '{path}': {exc}")
        return None


def _get_principled(mat):
    nt = mat.node_tree
    bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if bsdf is None:
        bsdf = nt.nodes.new('ShaderNodeBsdfPrincipled')
        out = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'), None)
        if out is None:
            out = nt.nodes.new('ShaderNodeOutputMaterial')
        nt.links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
    return bsdf


def _apply(obj, tex_map, alpha_cutout=False, mat=None):
    if mat is None:
        if not obj.data.materials:
            _newmat = bpy.data.materials.new(obj.name + "_FO4")
            _newmat.use_nodes = True
            obj.data.materials.append(_newmat)
        mat = obj.data.materials[0]
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = _get_principled(mat)
    applied = []
    y = 400

    def _mk(path, label, non_color=False):
        nonlocal y
        img = _load_image(path)
        if img is None:
            return None
        if non_color:
            try:
                img.colorspace_settings.name = 'Non-Color'
            except Exception:
                pass
        n = nt.nodes.new('ShaderNodeTexImage')
        n.image = img
        n.label = label
        n.location = (-700, y)
        y -= 300
        return n

    if tex_map.get('diffuse'):
        n = _mk(tex_map['diffuse'], 'FO4 Diffuse')
        if n:
            nt.links.new(n.outputs['Color'], bsdf.inputs['Base Color'])
            if alpha_cutout:
                a = bsdf.inputs.get('Alpha')
                if a:
                    nt.links.new(n.outputs['Alpha'], a)
                try:
                    mat.blend_method = 'CLIP'
                except Exception:
                    pass
                try:
                    mat.shadow_method = 'CLIP'  # removed in Blender 4.2+ (EEVEE Next)
                except Exception:
                    pass
                try:
                    mat.alpha_threshold = 0.5
                    mat.use_backface_culling = False
                except Exception:
                    pass
            applied.append('diffuse')

    if tex_map.get('normal'):
        n = _mk(tex_map['normal'], 'FO4 Normal', non_color=True)
        if n:
            nm = nt.nodes.new('ShaderNodeNormalMap')
            nm.location = (n.location[0] + 300, n.location[1])
            nt.links.new(n.outputs['Color'], nm.inputs['Color'])
            ninput = bsdf.inputs.get('Normal')
            if ninput:
                nt.links.new(nm.outputs['Normal'], ninput)
            applied.append('normal')

    if tex_map.get('specular'):
        n = _mk(tex_map['specular'], 'FO4 Specular', non_color=True)
        if n:
            si = bsdf.inputs.get('Specular IOR Level') or bsdf.inputs.get('Specular')
            if si:
                nt.links.new(n.outputs['Color'], si)
            applied.append('specular')

    if tex_map.get('glow'):
        n = _mk(tex_map['glow'], 'FO4 Glow', non_color=True)
        if n:
            ei = bsdf.inputs.get('Emission Color') or bsdf.inputs.get('Emission')
            if ei:
                nt.links.new(n.outputs['Color'], ei)
                es = bsdf.inputs.get('Emission Strength')
                if es:
                    es.default_value = 1.0
            applied.append('glow')

    return applied


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def _name_tokens(name):
    """Lowercase alphanumeric tokens (len>=3) of a name, dropping PyNifly's
    ':N' shape-index suffix — used to match a mesh to its BGSM.

    FO4 mesh/material names are almost always camelCase
    ("BlastedForestBurntTreeStump01") with no delimiters at all, while BGSM
    filenames are typically snake_case ("tree_atlas.bgsm"). Splitting on
    non-alphanumeric characters alone (the original approach) treats a whole
    camelCase name as one single token, so it can never overlap with a
    delimited filename's tokens — silently making every name-based match
    fail. Insert word breaks at lower→upper transitions BEFORE lowercasing
    so "TreeStump" tokenizes as {"tree", "stump"} instead of one blob.
    """
    import re as _re
    raw = (name or '').split(':')[0]
    raw = _re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', raw)
    base = raw.lower()
    return set(t for t in _re.split(r'[^a-z0-9]+', base) if len(t) >= 3)


def _obj_already_textured(obj):
    """True if a diffuse image is already wired to the object's material (e.g.
    PyNifly imported the per-shape material) — so we don't clobber it."""
    try:
        for slot in obj.material_slots:
            mat = slot.material
            if not mat or not mat.use_nodes or not mat.node_tree:
                continue
            for node in mat.node_tree.nodes:
                if node.type == 'BSDF_PRINCIPLED':
                    sock = node.inputs.get('Base Color')
                    if sock and sock.links:
                        fn = sock.links[0].from_node
                        if fn.type == 'TEX_IMAGE' and getattr(fn, 'image', None):
                            return True
    except Exception:
        pass
    return False


def _pick_bgsm_for_obj(obj, bgsm_refs, roots, deep_roots):
    """Choose the resolvable BGSM whose filename best matches THIS mesh (so a
    multi-material NIF gives each mesh its own material).  Falls back to the
    first resolvable BGSM when nothing matches by name."""
    obj_tokens = _name_tokens(getattr(obj, 'name', ''))
    try:
        for slot in getattr(obj, 'material_slots', []):
            if slot.material:
                obj_tokens |= _name_tokens(slot.material.name)
    except Exception:
        pass
    best = None          # (score, ref, file)
    first = None         # (ref, file) — fallback
    for ref in bgsm_refs:
        f = _find_file(ref, roots, deep_roots)
        if not f:
            continue
        if first is None:
            first = (ref, f)
        score = len(obj_tokens & _name_tokens(Path(ref).stem))
        if best is None or score > best[0]:
            best = (score, ref, f)
    if best and best[0] > 0:
        return best[1], best[2]
    return first if first else (None, None)


def _mat_already_textured(mat):
    """True if a diffuse image is already wired to *mat*'s Base Color."""
    try:
        if not mat or not mat.use_nodes or not mat.node_tree:
            return False
        for node in mat.node_tree.nodes:
            if node.type == 'BSDF_PRINCIPLED':
                sock = node.inputs.get('Base Color')
                if sock and sock.links:
                    fn = sock.links[0].from_node
                    if fn.type == 'TEX_IMAGE' and getattr(fn, 'image', None):
                        return True
    except Exception:
        pass
    return False


def _pick_bgsm_by_name(name, bgsm_refs, roots, deep_roots):
    """Choose the resolvable BGSM whose filename best matches *name* (a material
    or mesh name).  Falls back to the first resolvable BGSM.

    Returns (ref, file, confident) — ``confident`` is True only when a BGSM
    actually scored a name-token match; a "first resolvable" fallback is
    marked unconfident so callers can avoid overwriting an already-correct
    material with a low-confidence guess (see resolve_and_apply — this
    matters in multi-mesh NIFs, where blindly falling back would otherwise
    force-apply the same wrong mesh's BGSM onto every material that fails
    to score against its own name).
    """
    tokens = _name_tokens(name)
    best = None
    first = None
    for ref in bgsm_refs:
        f = _find_file(ref, roots, deep_roots)
        if not f:
            continue
        if first is None:
            first = (ref, f)
        score = len(tokens & _name_tokens(Path(ref).stem))
        if best is None or score > best[0]:
            best = (score, ref, f)
    if best and best[0] > 0:
        return best[1], best[2], True
    if first:
        return first[0], first[1], False
    return None, None, False


def preferred_roots(context=None):
    """Extra data roots from the add-on preferences (falling back to the scene
    properties): the MO2 mods folder and the extracted game Data folder."""
    roots = []
    mo2 = gdata = ""
    try:
        from . import preferences as _prefs_mod
        _p = _prefs_mod.get_preferences()
        if _p:
            mo2 = getattr(_p, "fo4_mo2_mods_root", "") or ""
            gdata = getattr(_p, "fo4_game_data_path", "") or ""
    except Exception:
        pass
    scene = getattr(context, "scene", None) if context is not None else None
    if scene is None and bpy is not None:
        scene = getattr(bpy.context, "scene", None)
    if scene is not None:
        mo2 = mo2 or (getattr(scene, "fo4_mo2_mods_root", "") or "")
        gdata = gdata or (getattr(scene, "fo4_game_data_path", "") or "")
    for r in (mo2, gdata):
        if r:
            roots.append(bpy.path.abspath(r))
    return roots


# PyNifly builds every imported FO4 shape's material around its own
# "Fallout 4 MTS" node group, with image nodes named after the NIF texture
# slots and wired into the group's inputs.
_PYN_NODE = {'diffuse': 'Diffuse_Texture', 'normal': 'Normal_Texture',
             'specular': 'Specular_Texture', 'glow': 'Glow_Texture'}
_PYN_INPUT = {'diffuse': 'Diffuse', 'normal': 'Normal',
              'specular': 'Smooth Spec', 'glow': 'Glow'}
_NIF_PROP = {'diffuse': 'BSShaderTextureSet_Diffuse', 'normal': 'BSShaderTextureSet_Normal',
             'specular': 'BSShaderTextureSet_Specular', 'glow': 'BSShaderTextureSet_Glow'}
_BGSM_ATTR = {'diffuse': 'diffuse_texture', 'normal': 'normal_texture',
              'specular': 'smooth_spec_texture', 'glow': 'glow_texture'}


def _image_works(img):
    """True when an image actually has pixels to show: packed, or its file
    exists on disk."""
    if img is None:
        return False
    try:
        if img.packed_file is not None:
            return True
        fp = bpy.path.abspath(img.filepath) if img.filepath else ""
        return bool(fp) and os.path.isfile(fp)
    except Exception:
        return False


def _surface_node(mat):
    for n in mat.node_tree.nodes:
        if n.type == 'OUTPUT_MATERIAL' and n.inputs.get('Surface') and n.inputs['Surface'].links:
            return n.inputs['Surface'].links[0].from_node
    return None


def _is_nif_material(mat):
    """A material PyNifly built from a NIF shader (its own FO4 node group)."""
    if mat is None or not mat.use_nodes or not mat.node_tree:
        return False
    if mat.get('BS_Shader_Block_Name') or mat.get('BSLSP_Shader_Name'):
        return True
    s = _surface_node(mat)
    return bool(s and s.type == 'GROUP' and s.node_tree and 'fallout 4' in s.node_tree.name.lower())


def _exact_texture_set(mat, roots, deep_roots):
    """The exact textures this one shape uses, straight from its own data:
    the shape's BGSM (named in the material) when it has one -- that is what
    the game reads -- else the NIF's own texture set. No name guessing.
    Returns (slot -> resolved file path, bgsm file or None, slot -> wanted ref)."""
    wanted = {}
    bgsm_file = None
    shader_name = str(mat.get('BSLSP_Shader_Name', '') or '').strip()
    if shader_name.lower().endswith(('.bgsm', '.bgem')):
        bgsm_file = _find_file(shader_name, roots, deep_roots)
        if bgsm_file:
            try:
                from . import bgsm_helpers
                raw = open(bgsm_file, 'rb').read()
                try:
                    data = bgsm_helpers.read_bgsm(raw)
                except Exception:
                    data = bgsm_helpers._bgsm_scrape_textures(raw)
                if data is not None:
                    for slot, attr in _BGSM_ATTR.items():
                        rel = (getattr(data, attr, '') or '').strip()
                        if rel:
                            wanted[slot] = rel
            except Exception as exc:
                print(f"[FO4Tex] BGSM read failed ({exc})")
    for slot, prop in _NIF_PROP.items():
        if slot not in wanted:
            rel = str(mat.get(prop, '') or '').strip()
            if rel:
                wanted[slot] = rel
    resolved = {}
    for slot, rel in wanted.items():
        hit = _find_file(rel, roots, deep_roots)
        if hit:
            resolved[slot] = str(hit)
    return resolved, bgsm_file, wanted


def _repair_nif_material(mat, resolved, force=False):
    """Fill in PyNifly's own texture nodes. A node whose image already works is
    left alone (unless *force*); a node whose file is missing gets the exact
    file; a slot PyNifly left out gets a node wired into the FO4 group.
    The material's output is never rewired."""
    nt = mat.node_tree
    group = _surface_node(mat)
    uv = nt.nodes.get('UV_Converter')
    repaired = []
    for slot, path in resolved.items():
        node = nt.nodes.get(_PYN_NODE[slot])
        if node is not None and node.type == 'TEX_IMAGE':
            if _image_works(node.image) and not force:
                continue
            if force and node.image and _image_works(node.image) and \
                    os.path.normcase(bpy.path.abspath(node.image.filepath)) == os.path.normcase(path):
                continue
            img = _load_image(path)
            if img is None:
                continue
            if slot != 'diffuse':
                try:
                    img.colorspace_settings.name = 'Non-Color'
                except Exception:
                    pass
            node.image = img
            repaired.append(slot)
            continue
        # PyNifly made no node for this slot (e.g. the NIF texture set was
        # empty and only the BGSM names it): add one into the FO4 group.
        if group is None or group.type != 'GROUP':
            continue
        sock = group.inputs.get(_PYN_INPUT[slot])
        if sock is None or sock.links:
            continue
        img = _load_image(path)
        if img is None:
            continue
        if slot != 'diffuse':
            try:
                img.colorspace_settings.name = 'Non-Color'
            except Exception:
                pass
        node = nt.nodes.new('ShaderNodeTexImage')
        node.name = node.label = _PYN_NODE[slot]
        node.image = img
        node.location = (group.location[0] - 400, group.location[1] - 260 * len(repaired))
        nt.links.new(node.outputs['Color'], sock)
        if uv is not None and uv.outputs.get('Vector'):
            nt.links.new(uv.outputs['Vector'], node.inputs['Vector'])
        repaired.append(slot)
    return repaired


def _repair_broken_paths(mat, roots, deep_roots):
    """For any material: re-point image nodes whose file is missing to the same
    file name found under the asset's folders / data roots. Nothing else."""
    fixed = []
    for n in mat.node_tree.nodes:
        if n.type != 'TEX_IMAGE' or n.image is None or _image_works(n.image):
            continue
        ref = n.image.filepath or n.image.name
        if not ref:
            continue
        hit = _find_file(ref, roots, deep_roots) or _find_file(Path(ref.replace('\\', '/')).name, roots, deep_roots)
        if hit:
            try:
                n.image.filepath = str(hit)
                n.image.reload()
                fixed.append(Path(str(hit)).name)
            except Exception:
                pass
    return fixed


def _has_working_texture(mat):
    try:
        return any(n.type == 'TEX_IMAGE' and _image_works(n.image)
                   and any(o.links for o in n.outputs)
                   for n in mat.node_tree.nodes)
    except Exception:
        return False


def resolve_and_apply(obj, source_path, extra_roots=None, force=False):
    """Make an imported asset's materials show their real textures.

    Rules (each confirmed against real PyNifly imports of vanilla NIFs):
      * A material PyNifly built (its "Fallout 4 MTS" group) is kept as is.
        Only image nodes whose file is missing are filled in, from this
        shape's own BGSM / texture set -- the old version replaced every such
        material with a new Principled BSDF and guessed textures by name,
        which unplugged correct materials and put e.g. the sky cubemap on a
        pistol and the hedgerow texture on a holly bush.
      * Any other material (FBX/OBJ/GLB, hand-made) only gets missing image
        files re-pointed to the same file name under the asset's folders.
      * A NIF material with no texture nodes at all falls back to the exact
        per-shape texture set (PyNifly's parse, then a name-matched BGSM only
        when the match is confident).

    ``force`` (manual "Resolve FO4 Textures" button) also re-points working
    NIF texture nodes to the exact BGSM textures when they differ.

    Returns (success: bool, message: str).
    """
    if bpy is None or obj is None or getattr(obj, 'type', None) != 'MESH':
        return False, "no mesh object"

    roots = _candidate_roots(source_path, extra_roots)
    if not roots:
        return False, "no search roots found near the asset"

    # Deep (recursive) search only in the asset's own folder; the global data
    # / MO2 roots get fast exact-path lookups only.
    _extra_set = set()
    for _r in (extra_roots or []):
        try:
            _extra_set.add(Path(_r).resolve())
        except Exception:
            pass
    deep_roots = [r for r in roots if r.resolve() not in _extra_set]

    src = Path(source_path)
    is_nif = src.suffix.lower() == '.nif'
    bgsm_refs, dds_refs = (_scan_nif_refs(src) if is_nif else ([], []))

    done, missing, kept = [], [], 0
    for slot in list(getattr(obj, 'material_slots', [])):
        mat = slot.material
        if mat is None:
            continue
        if not mat.use_nodes:
            mat.use_nodes = True

        if _is_nif_material(mat):
            resolved, bgsm_file, wanted = _exact_texture_set(mat, roots, deep_roots)
            if bgsm_file:
                try:
                    mat["fo4_bgsm_path"] = str(bgsm_file)
                except Exception:
                    pass
            got = _repair_nif_material(mat, resolved, force=force)
            done += got
            for s, rel in wanted.items():
                node = mat.node_tree.nodes.get(_PYN_NODE[s])
                if s not in resolved and not (node is not None and _image_works(node.image)):
                    missing.append(f"{s} ({rel})")
            if not got:
                kept += 1
            continue

        fixed = _repair_broken_paths(mat, roots, deep_roots)
        done += fixed
        if _has_working_texture(mat):
            if not fixed:
                kept += 1
            continue
        if not is_nif:
            continue

        # A NIF material with nothing usable at all (e.g. imported by another
        # NIF tool): use the exact per-shape data, or a confident BGSM match.
        tex_map = {}
        alpha_cutout = False
        bgsm_file = None
        shape_name = obj.get('fo4_original_name') or obj.name
        try:
            from io_scene_nifly.pyn import pynifly as _pyn
            _nf = _pyn.NifFile(str(src))
            _shape = next((s for s in _nf.shapes if s.name == shape_name), None)
            _tex = (getattr(_shape.shader, 'textures', None) or {}) if (_shape and _shape.shader) else {}
            for key, sl in (('Diffuse', 'diffuse'), ('Normal', 'normal'),
                            ('Specular', 'specular'), ('Glow', 'glow')):
                rel = (_tex.get(key) or '').strip()
                hit = _find_file(rel, roots, deep_roots) if rel else None
                if hit:
                    tex_map[sl] = str(hit)
        except Exception:
            pass
        if not tex_map and bgsm_refs:
            _ref, bgsm_file, confident = _pick_bgsm_by_name(mat.name, bgsm_refs, roots, deep_roots)
            if bgsm_file and confident:
                try:
                    from . import bgsm_helpers
                    _raw = open(bgsm_file, 'rb').read()
                    try:
                        data = bgsm_helpers.read_bgsm(_raw)
                    except Exception:
                        data = bgsm_helpers._bgsm_scrape_textures(_raw)
                    for key, attr in _BGSM_ATTR.items():
                        rel = (getattr(data, attr, '') or '').strip()
                        hit = _find_file(rel, roots, deep_roots) if rel else None
                        if hit:
                            tex_map[key] = str(hit)
                    alpha_cutout = bool(getattr(data, 'tree', False))
                except Exception as exc:
                    print(f"[FO4Tex] BGSM read failed ({exc})")
        if tex_map:
            got = _apply(obj, tex_map, alpha_cutout=alpha_cutout, mat=mat)
            done += got
            if bgsm_file:
                try:
                    mat["fo4_bgsm_path"] = str(bgsm_file)
                except Exception:
                    pass
        else:
            missing.append(f"'{mat.name}' (no texture data found)")

    parts = []
    if done:
        parts.append("filled in: " + ", ".join(sorted(set(done))))
    if kept:
        parts.append(f"{kept} material(s) already complete, left as imported")
    if missing:
        parts.append("NOT found: " + "; ".join(missing[:4]) +
                     f" -- searched {roots[0]}" + (f" and {len(roots) - 1} more folder(s)" if len(roots) > 1 else ""))
    if not parts:
        return True, "no materials to texture"
    return (not missing or bool(done) or bool(kept)), "  |  ".join(parts)


# ---------------------------------------------------------------------------
# Manual operator (works on any selected imported mesh, any import path)
# ---------------------------------------------------------------------------
if bpy is not None:

    class FO4_OT_ResolveTextures(bpy.types.Operator):
        """Follow the selected mesh's BGSM material and load its FO4 textures
        from the asset's mod folder onto the material."""
        bl_idname = "fo4.resolve_textures"
        bl_label = "Resolve FO4 Textures (BGSM)"
        bl_options = {'REGISTER', 'UNDO'}

        source_path: bpy.props.StringProperty(
            name="Source NIF",
            description="Path to the imported .nif (leave blank to use the object's stored path)",
            subtype='FILE_PATH',
            default="",
        )

        def execute(self, context):
            obj = context.active_object
            if not obj or obj.type != 'MESH':
                self.report({'WARNING'}, "Select the imported mesh first")
                return {'CANCELLED'}
            path = self.source_path or obj.get("fo4_source_nif", "")
            if not path:
                self.report({'WARNING'},
                            "No source NIF path — set it in the operator field")
                return {'CANCELLED'}
            extra = []
            # Read from addon preferences (persistent across sessions) first,
            # fall back to scene properties for backwards compat.
            try:
                from . import preferences as _prefs_mod
                _p = _prefs_mod.get_preferences()
                root = (getattr(_p, "fo4_mo2_mods_root", "") or "") if _p else ""
                gdata = (getattr(_p, "fo4_game_data_path", "") or "") if _p else ""
            except Exception:
                root = ""
                gdata = ""
            if not root:
                root = getattr(context.scene, "fo4_mo2_mods_root", "") or ""
            if not gdata:
                gdata = getattr(context.scene, "fo4_game_data_path", "") or ""
            if root:
                extra.append(bpy.path.abspath(root))
            if gdata:
                extra.append(bpy.path.abspath(gdata))
            ok, msg = resolve_and_apply(obj, bpy.path.abspath(path), extra_roots=extra, force=True)
            self.report({'INFO'} if ok else {'WARNING'}, msg)
            return {'FINISHED'} if ok else {'CANCELLED'}

    _classes = (FO4_OT_ResolveTextures,)
else:
    _classes = ()


def register():
    if bpy is None:
        return
    try:
        bpy.types.Scene.fo4_mo2_mods_root = bpy.props.StringProperty(
            name="MO2 Mods Root",
            description="Optional extra root to search for textures/materials "
                        "(e.g. your MO2 mods or Overwrite folder). The asset's own "
                        "mod folder is detected automatically.",
            subtype='DIR_PATH',
            default="",
        )
        bpy.types.Scene.fo4_game_data_path = bpy.props.StringProperty(
            name="FO4 Game Data Path",
            description="Path to your Fallout 4 Data folder "
                        "(e.g. E:\\Steam\\steamapps\\common\\Fallout 4\\Data). "
                        "Used to find loose textures extracted from BA2 archives.",
            subtype='DIR_PATH',
            default="",
        )
    except Exception as exc:
        print(f"[FO4Tex] property register failed: {exc}")
    for cls in _classes:
        try:
            bpy.utils.register_class(cls)
        except Exception as exc:
            print(f"[FO4Tex] register {cls.__name__} failed: {exc}")
    print("[FO4Tex] BGSM/MO2 texture resolver registered")


def unregister():
    if bpy is None:
        return
    for cls in reversed(_classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
    try:
        del bpy.types.Scene.fo4_mo2_mods_root
    except Exception:
        pass
    try:
        del bpy.types.Scene.fo4_game_data_path
    except Exception:
        pass
