"""
Texture helper functions for Fallout 4 mod creation
"""

import bpy
import os

class TextureHelpers:
    """Helper functions for texture setup and validation"""

    # ---------------------------------------------------------------------------
    # Fallout 4 texture naming conventions
    # FO4 .dds textures follow strict suffixes recognised by the game engine:
    #   _d  – diffuse/albedo colour map        (BC1 or BC3 with alpha)
    #   _n  – tangent-space normal map          (BC5 / ATI2N)
    #   _s  – specular / gloss map              (BC5, as vanilla)
    #   _g  – glow / emissive mask              (BC1)
    #   _e  – environment / cube-map mask       (BC1)
    # The helpers use these conventions for auto-detection and compression.
    # ---------------------------------------------------------------------------
    _FO4_SUFFIX_MAP = {
        '_d': 'DIFFUSE',
        '_n': 'NORMAL',
        '_s': 'SPECULAR',
        '_g': 'GLOW',
        '_e': 'ENVIRONMENT',  # environment / cube-map mask (not emissive)
    }

    @staticmethod
    def detect_fo4_texture_type(filepath):
        """Return the FO4 texture type for *filepath* based on its filename suffix.

        Fallout 4 uses a strict `_<letter>` suffix convention immediately before
        the ``.dds`` extension.  For example ``rust_metal_d.dds`` is a diffuse
        map and ``rust_metal_n.dds`` is a normal map.

        Returns one of: ``'DIFFUSE'``, ``'NORMAL'``, ``'SPECULAR'``, ``'GLOW'``,
        ``'EMISSIVE'``, or ``'DIFFUSE'`` as the safe default when no recognised
        suffix is found.
        """
        if not filepath:
            return 'DIFFUSE'
        stem = os.path.splitext(os.path.basename(filepath))[0].lower()
        for suffix, tex_type in TextureHelpers._FO4_SUFFIX_MAP.items():
            if stem.endswith(suffix):
                return tex_type
        return 'DIFFUSE'

    @staticmethod
    def setup_fo4_material(obj, force=False):
        """Setup a Fallout 4 compatible material for the object.

        A material the object already has is never replaced: it is returned
        as-is, with its textures, its .bgsm link and its node graph untouched.
        A fresh FO4 node graph is only built for a mesh with no material.

        The old check only recognised a Principled BSDF fed by a loaded image,
        so a mesh loaded from a NIF (PyNifly's own "Fallout 4 MTS" shader, no
        Principled BSDF) or one whose texture files weren't found got its
        material swapped for an empty one by every caller of this function.

        *force* is kept for compatibility and no longer replaces anything.
        """
        if obj.type != 'MESH':
            return None

        existing = obj.active_material or next(
            (m for m in obj.data.materials if m is not None), None)
        if existing is not None:
            return existing

        # Create new material
        mat_name = f"{obj.name}_FO4_Material"
        mat = bpy.data.materials.new(name=mat_name)
        mat.use_nodes = True
        
        # Clear default nodes
        nodes = mat.node_tree.nodes
        nodes.clear()
        
        # Create shader nodes for Fallout 4 material
        # Output node
        output_node = nodes.new(type='ShaderNodeOutputMaterial')
        output_node.location = (500, 0)
        
        # Principled BSDF (main shader)
        bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
        bsdf.location = (100, 0)
        
        # Diffuse texture (_d)
        diffuse_tex = nodes.new(type='ShaderNodeTexImage')
        diffuse_tex.name = "Diffuse"
        diffuse_tex.label = "Diffuse (_d)"
        diffuse_tex.location = (-400, 300)
        
        # Normal map texture (_n)
        normal_tex = nodes.new(type='ShaderNodeTexImage')
        normal_tex.name = "Normal"
        normal_tex.label = "Normal Map (_n)"
        normal_tex.location = (-400, 0)
        # Colorspace will be set when image is loaded in install_texture
        
        # Normal map node
        normal_map = nodes.new(type='ShaderNodeNormalMap')
        normal_map.location = (-100, 0)
        
        # Specular texture (_s)
        specular_tex = nodes.new(type='ShaderNodeTexImage')
        specular_tex.name = "Specular"
        specular_tex.label = "Specular (_s)"
        specular_tex.location = (-400, -300)
        # Colorspace will be set when image is loaded in install_texture
        
        # Glow / emissive texture (_g)
        glow_tex = nodes.new(type='ShaderNodeTexImage')
        glow_tex.name = "Glow"
        glow_tex.label = "Glow/Emissive (_g)"
        glow_tex.location = (-400, -600)

        # Environment / cube-map mask (_e)
        env_tex = nodes.new(type='ShaderNodeTexImage')
        env_tex.name = "Environment"
        env_tex.label = "Environment Mask (_e)"
        env_tex.location = (-400, -900)
        # Colorspace will be set when image is loaded in install_texture
        
        # Connect nodes
        links = mat.node_tree.links
        links.new(bsdf.outputs['BSDF'], output_node.inputs['Surface'])
        links.new(diffuse_tex.outputs['Color'], bsdf.inputs['Base Color'])

        # Do NOT wire diffuse alpha -> BSDF Alpha here. This "Diffuse" node
        # is freshly created with no image loaded yet (this branch only
        # runs for untextured meshes -- see the docstring), and an Image
        # Texture node with no image outputs Alpha=0. That's harmless while
        # blend_method stays 'OPAQUE' (the default, which ignores the Alpha
        # input entirely) but as soon as ANYTHING switches this material to
        # 'CLIP'/'BLEND'/'HASHED' (e.g. Setup Vegetation Material, which the
        # Retopologize & Bake pipeline's own docstring recommends running
        # right after this) with no diffuse texture ever installed (e.g.
        # bake_diffuse=False), the whole mesh silently renders fully
        # invisible -- verified by reproducing it directly. install_texture
        # (via _wire_texture_slot_node) wires this link correctly once a
        # real diffuse image actually exists; until then, leave BSDF Alpha
        # at its own default (1.0, opaque).

        links.new(normal_tex.outputs['Color'], normal_map.inputs['Color'])
        links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])
        # 'Specular' was renamed to 'Specular IOR Level' in Blender 4.0
        specular_input = bsdf.inputs.get('Specular IOR Level') or bsdf.inputs.get('Specular')
        if specular_input:
            links.new(specular_tex.outputs['Color'], specular_input)
        # Glow/emissive → Emission input
        emission_input = bsdf.inputs.get('Emission Color') or bsdf.inputs.get('Emission')
        if emission_input:
            links.new(glow_tex.outputs['Color'], emission_input)
        # Set emission strength so the glow texture is visible
        emission_strength = bsdf.inputs.get('Emission Strength')
        if emission_strength:
            emission_strength.default_value = 1.0
        # Environment mask → Metallic (environment cube-map areas ≈ reflective areas)
        metallic_input = bsdf.inputs.get('Metallic')
        if metallic_input:
            links.new(env_tex.outputs['Color'], metallic_input)
        
        # Assign material to object (respect the active slot rather than
        # always overwriting slot 0, so multi-material objects don't lose
        # an unrelated material in another slot)
        if obj.data.materials:
            idx = obj.active_material_index
            if not (0 <= idx < len(obj.data.materials)):
                idx = 0
            obj.data.materials[idx] = mat
            obj.active_material_index = idx
        else:
            obj.data.materials.append(mat)

        return mat
    
    @staticmethod
    def setup_vegetation_material(obj):
        """Set up FO4 vegetation material settings on *obj*.

        Preserves existing materials: if the object already has a material with
        a diffuse texture connected (PyNifly import format or named-node format)
        only the render settings and custom properties are patched — the node
        tree and texture links are left untouched.

        A fresh material is created only when there is no usable material.
        """
        def _has_diffuse(m):
            """Return True if *m* already has a diffuse texture connected."""
            if not (m and m.use_nodes):
                return False
            for node in m.node_tree.nodes:
                # PyNifly format: Image Texture → Principled BSDF Base Color
                if node.type == 'BSDF_PRINCIPLED':
                    sock = node.inputs.get('Base Color')
                    if sock and sock.links and sock.links[0].from_node.type == 'TEX_IMAGE':
                        return True
                # Named-node format: node.name/label == "Diffuse" or "Base"
                if node.type == 'TEX_IMAGE' and (
                    node.name  in {'Diffuse', 'Base'} or
                    node.label in {'Diffuse', 'Base'}
                ):
                    return True
            return False

        existing = obj.active_material
        if _has_diffuse(existing):
            mat = existing
        else:
            mat = TextureHelpers.setup_fo4_material(obj)
            if mat is None:
                return None

        # Alpha clip — standard FO4 foliage
        mat.blend_method     = 'CLIP'
        mat.alpha_threshold  = 0.5
        mat.use_backface_culling = False

        # fo4_shader_type/fo4_core_profile are read by blender_mat_to_bgsm()
        # (bgsm_helpers.py) as shader-flag hints. fo4_translucency (the bare
        # bool) is also read there and sets SF1_BACK_LIGHTING.
        #
        # fo4_translucency_subsurface_r/g/b, _scale, and _mix_albedo below
        # are NOT read anywhere in the codebase (verified: grepped for every
        # one of these keys project-wide, only this write site exists).
        # That's not an oversight to "fix" by wiring them up -- real FO4
        # BGSM files (format version 2, confirmed across this addon's whole
        # sampled reference library) have no translucency/subsurface fields
        # at all to write these into; see blender_mat_to_bgsm's own comment
        # on this. They're kept here purely as informational metadata on
        # the material (e.g. visible via Blender's custom-properties panel
        # for a modder cross-referencing values against a real vanilla
        # asset) -- do not expect changing them to affect the export.
        mat["fo4_shader_type"]               = "vegetation"
        mat["fo4_core_profile"]              = "foliage"
        mat["fo4_translucency"]              = True
        mat["fo4_translucency_subsurface_r"] = 0.35
        mat["fo4_translucency_subsurface_g"] = 0.60
        mat["fo4_translucency_subsurface_b"] = 0.15
        mat["fo4_translucency_scale"]        = 0.55
        mat["fo4_translucency_mix_albedo"]   = True

        bsdf = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bsdf is not None:
            # Leave a glow that is already wired in alone.
            strength = bsdf.inputs.get('Emission Strength')
            emit_col = bsdf.inputs.get('Emission Color') or bsdf.inputs.get('Emission')
            if strength and not strength.is_linked and not (emit_col and emit_col.is_linked):
                strength.default_value = 0.0
            subsurface = bsdf.inputs.get('Subsurface Weight') or bsdf.inputs.get('Subsurface')
            if subsurface:
                subsurface.default_value = 0.25
            subsurface_color = (bsdf.inputs.get('Subsurface Color') or
                                bsdf.inputs.get('Subsurface Radius'))
            if subsurface_color and hasattr(subsurface_color, 'default_value'):
                try:
                    subsurface_color.default_value = (0.35, 0.60, 0.15, 1.0)
                except Exception:
                    pass

        return mat

    @staticmethod
    def _sanitize_folder_name(name):
        """Strip characters that aren't safe in a Windows folder name."""
        keep = "".join(c if (c.isalnum() or c in "_- ") else "_" for c in name)
        return keep.strip("_ ") or "UnknownAsset"

    # Maps the object's fo4_mesh_type EnumProperty (operators.py, per-object
    # registration) to the real vanilla FO4 Textures\ subfolder its category
    # of asset actually lives in. Previously every install unconditionally
    # went to Textures\Armor\<name> regardless of what kind of asset it was
    # -- a weapon, a piece of furniture, a wall section, a plant, etc. would
    # all get filed under Armor, which is wrong and makes texture installs
    # hard to find/manage in the actual Data folder.
    #
    # Verified against the real vanilla Textures\ folder layout (checked
    # 2026-08-25 against an unpacked vanilla Data folder) rather than guessed -- ARMOR/WEAPON/FURNITURE/ARCHITECTURE/
    # VEGETATION/FLORA all confirmed as real top-level folders. Two entries
    # were corrected after that check found they didn't match anything real:
    # there is no top-level "Clutter" folder (small junk/debris items like
    # AbraxoClean/AlienToy/Antifreeze actually live under "Props"), and LOD
    # variants get their own top-level "LOD" folder (see
    # reference_fo4_working_folder.md), not "Landscape" (which is terrain
    # textures -- an unrelated category).
    _MESH_TYPE_TEXTURE_FOLDER = {
        'ARMOR': "Armor",
        'SKINNED': "Armor",
        'WEAPON': "Weapons",
        'FURNITURE': "Furniture",
        'ARCHITECTURE': "Architecture",
        'VEGETATION': "Plants",
        'FLORA': "Plants",
        'DEBRIS': "Props",
        'ANIMATED': "Props",
        'LOD': "LOD",
        # AUTO / STATIC / unset -- no reliable category signal, keep the
        # previous default so existing installs don't change location.
        'STATIC': "Armor",
        'AUTO': "Armor",
    }

    @staticmethod
    def _texture_category_folder(obj):
        """Return the FO4 ``Textures\\<Category>`` subfolder name for *obj*,
        derived from its ``fo4_mesh_type`` classification. Must be read via
        attribute access -- fo4_mesh_type is a real RNA EnumProperty, not a
        custom ID property, so ``obj["fo4_mesh_type"]``/``.get()`` would
        silently miss it.
        """
        mesh_type = getattr(obj, "fo4_mesh_type", "AUTO") or "AUTO"
        return TextureHelpers._MESH_TYPE_TEXTURE_FOLDER.get(mesh_type, "Armor")

    @staticmethod
    def _ensure_texture_in_data_folder(obj, texture_path):
        """Copy *texture_path* into ``Data\\Textures\\<Category>\\<asset name>\\``
        under the user's configured FO4 Data folder (the category subfolder
        is derived from the object's ``fo4_mesh_type``), returning the
        copied file's absolute path -- or the original path unchanged if no
        Data folder is configured, the source is already inside one, or the
        copy fails for any reason (never blocks the install over this).
        """
        try:
            from . import preferences as _prefs_mod
        except ImportError:
            from .. import preferences as _prefs_mod

        norm_src = os.path.normpath(texture_path)
        if "\\data\\textures\\" in norm_src.lower() or "/data/textures/" in norm_src.lower():
            return texture_path  # already living in a Data\Textures tree

        data_root = None
        try:
            p = _prefs_mod.get_preferences()
            data_root = (getattr(p, "fo4_game_data_path", "") or "") if p else ""
        except Exception:
            pass
        if not data_root:
            try:
                data_root = _prefs_mod.get_fo4_assets_path() or ""
            except Exception:
                data_root = ""
        if not data_root:
            return texture_path  # nothing configured -- best effort, unchanged

        asset_name = TextureHelpers._sanitize_folder_name(obj.name)
        category_folder = TextureHelpers._texture_category_folder(obj)
        dest_dir = os.path.join(bpy.path.abspath(data_root), "Textures", category_folder, asset_name)
        dest_path = os.path.join(dest_dir, os.path.basename(texture_path))

        if os.path.normcase(os.path.normpath(dest_path)) == os.path.normcase(norm_src):
            return texture_path  # already the destination

        # Refuse to blindly clobber an existing, higher-resolution texture at
        # the destination with whatever this call is installing -- confirmed
        # real incident: a correct 2K texture already sitting in a mod
        # folder got silently overwritten by a 512 one, because this copy
        # only ever checked whether source == destination, never what was
        # already there.
        # A file already at the destination is never written over (the old
        # check only refused a lower-resolution replacement).  The same file
        # is simply reused; a different one stays, and the texture is used
        # from where the user picked it.
        if os.path.exists(dest_path):
            import filecmp
            try:
                if filecmp.cmp(dest_path, texture_path, shallow=False):
                    return dest_path
            except OSError:
                pass
            print(
                f"[FO4 Add-on] Not copying '{texture_path}' over the existing "
                f"'{dest_path}' -- existing files are never replaced."
            )
            return texture_path

        try:
            os.makedirs(dest_dir, exist_ok=True)
            import shutil
            shutil.copy2(texture_path, dest_path)
            return dest_path
        except Exception:
            return texture_path  # copy failed -- fall back to the original path

    # PyNifly (io_scene_nifly) builds a NIF material around its own FO4 shader
    # group: image nodes named <Slot>_Texture feeding these group inputs
    # (names checked against PyNifly's shaders.blend, "Fallout 4 MTS").
    _PYN_SLOT = {
        'Diffuse':  ('Diffuse_Texture',  'Diffuse'),
        'Normal':   ('Normal_Texture',   'Normal'),
        'Specular': ('Specular_Texture', 'Smooth Spec'),
        'Glow':     ('Glow_Map_Texture', 'Glow Map'),
    }

    @staticmethod
    def _pynifly_shader(mat):
        """PyNifly's FO4 shader group driving *mat*'s output, or None."""
        for n in mat.node_tree.nodes:
            if n.type != 'OUTPUT_MATERIAL':
                continue
            surf = n.inputs.get('Surface')
            if surf is not None and surf.is_linked:
                src = surf.links[0].from_node
                if src.type == 'GROUP' and 'Diffuse' in src.inputs \
                        and 'Alpha Property' in src.inputs:
                    return src
        return None

    @staticmethod
    def _bsdf_slot_socket(bsdf, node_name):
        """The Principled BSDF input a FO4 texture slot feeds."""
        if node_name == 'Diffuse':
            return bsdf.inputs.get('Base Color')
        if node_name == 'Normal':
            return bsdf.inputs.get('Normal')
        if node_name == 'Specular':
            return bsdf.inputs.get('Specular IOR Level') or bsdf.inputs.get('Specular')
        if node_name == 'Glow':
            return bsdf.inputs.get('Emission Color') or bsdf.inputs.get('Emission')
        if node_name == 'Environment':
            return bsdf.inputs.get('Metallic')
        return None

    @staticmethod
    def _find_slot_node(mat, node_name):
        """The image node already serving texture slot *node_name*, or None:
        the FO4-named node, PyNifly's node, or whichever image node already
        feeds that slot's shader input."""
        nodes = mat.node_tree.nodes
        n = nodes.get(node_name)
        if n is not None and n.type == 'TEX_IMAGE':
            return n
        pyn = TextureHelpers._PYN_SLOT.get(node_name)
        if pyn:
            n = nodes.get(pyn[0])
            if n is not None and n.type == 'TEX_IMAGE':
                return n
        group = TextureHelpers._pynifly_shader(mat)
        sock = None
        if group is not None:
            sock = group.inputs.get(pyn[1]) if pyn else None
        else:
            bsdf = next((x for x in nodes if x.type == 'BSDF_PRINCIPLED'), None)
            if bsdf is not None:
                sock = TextureHelpers._bsdf_slot_socket(bsdf, node_name)
        if sock is not None and sock.is_linked:
            src = sock.links[0].from_node
            if src.type == 'NORMAL_MAP' and src.inputs['Color'].is_linked:
                src = src.inputs['Color'].links[0].from_node
            if src.type == 'TEX_IMAGE':
                return src
        return None

    @staticmethod
    def _image_works(img):
        """True when *img* has pixels to show: packed, or its file exists."""
        if img is None:
            return False
        try:
            if img.packed_file is not None:
                return True
            fp = bpy.path.abspath(img.filepath) if img.filepath else ""
            return bool(fp) and os.path.isfile(fp)
        except Exception:
            return False

    @staticmethod
    def install_texture(obj, texture_path, texture_type='DIFFUSE', replace=True):
        """Install a texture into the object's material.

        *texture_type* accepts both the FO4 node-name convention (``'Diffuse'``,
        ``'Normal'``, ``'Specular'``, ``'Glow'``, ``'Emissive'``) and the
        upper-case enum used elsewhere in the add-on (``'DIFFUSE'``, ``'NORMAL'``,
        ``'SPECULAR'``, ``'GLOW'``, ``'EMISSIVE'``).  If *texture_path* is given
        and *texture_type* is omitted, :meth:`detect_fo4_texture_type` will
        auto-detect the type from the FO4 filename suffix.

        The texture goes into the node that already serves that slot (an FO4
        "Diffuse" node, PyNifly's "Diffuse_Texture", or whatever image already
        feeds the shader input); a node is only added when the slot has none,
        and it is only plugged into a shader input nothing else uses.  The
        material, its other textures and its shader wiring are never replaced.

        *replace=False* (used by automatic steps such as import) keeps a slot
        that already shows a working texture.  The Install Texture button
        passes True: putting the chosen file into that slot is what it is for.
        """
        if not os.path.exists(texture_path):
            return False, f"Texture file not found: {texture_path}"

        mat = obj.active_material or next(
            (m for m in obj.data.materials if m is not None), None)
        if mat is None:
            mat = bpy.data.materials.new(name=f"{obj.name}_FO4_Material")
            if obj.data.materials:
                # Fill the empty slot rather than adding another one.
                idx = obj.active_material_index
                if not (0 <= idx < len(obj.data.materials)):
                    idx = 0
                obj.data.materials[idx] = mat
            else:
                obj.data.materials.append(mat)
        mat.use_nodes = True

        # Auto-detect type from filename suffix if caller left it at the default
        # and the path has a recognisable FO4 suffix.
        detected = TextureHelpers.detect_fo4_texture_type(texture_path)
        if texture_type == 'DIFFUSE' and detected != 'DIFFUSE':
            texture_type = detected

        # Normalise to the internal node-name key (upper-case enum).
        texture_type = texture_type.upper()

        node_name_map = {
            'DIFFUSE':     'Diffuse',
            'NORMAL':      'Normal',
            'SPECULAR':    'Specular',
            'GLOW':        'Glow',
            'EMISSIVE':    'Glow',       # EMISSIVE and GLOW share the same node slot
            'ENVIRONMENT': 'Environment',
            'ENV':         'Environment',
        }

        node_name = node_name_map.get(texture_type)
        if not node_name:
            return False, f"Unknown texture type: {texture_type}"

        tex_node = TextureHelpers._find_slot_node(mat, node_name)
        if tex_node is not None and not replace and TextureHelpers._image_works(tex_node.image):
            return True, (f"Kept the existing {node_name} texture "
                          f"({os.path.basename(tex_node.image.filepath or tex_node.image.name)})")

        # Copy the texture into the FO4 Data folder if it isn't already there.
        # A texture loaded straight from wherever the user picked it (their
        # Downloads folder, an artist's source-art path on a completely
        # different machine, etc.) gets that exact absolute path baked into
        # the NIF's shader block on export -- Fallout 4 only resolves
        # Data\Textures\-relative paths, so an un-copied texture silently
        # fails to load in-game (and often in NifSkope's Textured view too).
        texture_path = TextureHelpers._ensure_texture_in_data_folder(obj, texture_path)

        if tex_node is None:
            tex_node = TextureHelpers._ensure_texture_slot_node(mat, node_name)
        else:
            # Plug an existing but unplugged slot node back in -- only into an
            # input nothing else is using.
            TextureHelpers._wire_texture_slot_node(mat, node_name, tex_node)

        try:
            img = bpy.data.images.load(texture_path)
            tex_node.image = img

            # Non-colour textures must use 'Non-Color' colorspace so Blender
            # does not apply gamma correction before the NIF exporter reads them.
            if texture_type in ('NORMAL', 'SPECULAR', 'GLOW', 'EMISSIVE', 'ENVIRONMENT', 'ENV'):
                img.colorspace_settings.name = 'Non-Color'

            return True, f"Texture installed successfully: {texture_type}"
        except Exception as e:
            return False, f"Failed to load texture: {str(e)}"

    @staticmethod
    def _ensure_texture_slot_node(mat, node_name):
        """Add a texture node for slot *node_name* to *mat* and plug it into
        that slot's shader input if the input is free.  Used when the material
        has no node for the slot yet.  On a PyNifly material the node follows
        PyNifly's naming and feeds its FO4 shader group; a Principled BSDF is
        only added (and connected to the output) when the material has no
        shader at all."""
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links

        group = TextureHelpers._pynifly_shader(mat)
        if group is not None:
            pyn = TextureHelpers._PYN_SLOT.get(node_name)
            tex_node = nodes.new('ShaderNodeTexImage')
            tex_node.name = tex_node.label = pyn[0] if pyn else node_name
            tex_node.location = (group.location.x - 500, group.location.y - 300)
            sock = group.inputs.get(pyn[1]) if pyn else None
            if sock is not None and not sock.is_linked:
                links.new(tex_node.outputs['Color'], sock)
            uv = nodes.get('UV_Converter')
            if uv is not None and uv.outputs.get('Vector'):
                links.new(uv.outputs['Vector'], tex_node.inputs['Vector'])
            return tex_node

        bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bsdf is None:
            bsdf = nodes.new('ShaderNodeBsdfPrincipled')
            out = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None)
            if out is None:
                out = nodes.new('ShaderNodeOutputMaterial')
            if not out.inputs['Surface'].is_linked:
                links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])

        tex_node = nodes.new('ShaderNodeTexImage')
        tex_node.name = node_name
        tex_node.label = node_name
        tex_node.location = (bsdf.location.x - 400, bsdf.location.y)

        TextureHelpers._wire_texture_slot_node(mat, node_name, tex_node)
        return tex_node

    @staticmethod
    def _wire_texture_slot_node(mat, node_name, tex_node):
        """Plug *tex_node* into the Principled BSDF input for slot *node_name*
        -- only when that input is free (or already fed by this node).  An
        input another texture already drives is left as it is."""
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bsdf is None:
            return

        def _free(sock):
            return sock is not None and not sock.is_linked

        if node_name == 'Diffuse':
            base = bsdf.inputs.get('Base Color')
            if _free(base):
                links.new(tex_node.outputs['Color'], base)
                alpha_input = bsdf.inputs.get('Alpha')
                if _free(alpha_input):
                    links.new(tex_node.outputs['Alpha'], alpha_input)
        elif node_name == 'Normal':
            normal_input = bsdf.inputs.get('Normal')
            nmap = next(
                (n for n in nodes if n.type == 'NORMAL_MAP'
                 and n.inputs['Color'].is_linked
                 and n.inputs['Color'].links[0].from_node is tex_node),
                None,
            )
            if nmap is None and _free(normal_input):
                nmap = nodes.new('ShaderNodeNormalMap')
                nmap.location = (tex_node.location.x + 200, tex_node.location.y)
                links.new(tex_node.outputs['Color'], nmap.inputs['Color'])
            if nmap is not None and _free(normal_input):
                links.new(nmap.outputs['Normal'], normal_input)
        elif node_name == 'Specular':
            spec_input = bsdf.inputs.get('Specular IOR Level') or bsdf.inputs.get('Specular')
            if _free(spec_input):
                links.new(tex_node.outputs['Color'], spec_input)
        elif node_name == 'Glow':
            emission_input = bsdf.inputs.get('Emission Color') or bsdf.inputs.get('Emission')
            if _free(emission_input):
                links.new(tex_node.outputs['Color'], emission_input)
                strength = bsdf.inputs.get('Emission Strength')
                if strength and not strength.is_linked and strength.default_value == 0.0:
                    strength.default_value = 1.0
        elif node_name == 'Environment':
            metallic_input = bsdf.inputs.get('Metallic')
            if _free(metallic_input):
                links.new(tex_node.outputs['Color'], metallic_input)

    @staticmethod
    def validate_textures(obj):
        """Validate textures for Fallout 4 compatibility"""
        issues = []

        if not obj.data.materials or obj.active_material is None:
            issues.append("Object has no materials")
            return False, issues

        mat = obj.active_material

        if not mat.use_nodes:
            issues.append("Material does not use nodes")
            return False, issues

        nodes = mat.node_tree.nodes

        # PyNifly-imported materials don't use node names 'Diffuse'/'Normal' — they
        # name nodes after the texture filename.  Detect this case up-front: if the
        # material has *any* TEX_IMAGE nodes with images loaded, treat it as textured.
        has_any_tex_image = any(
            n.type == 'TEX_IMAGE' and n.image
            for n in nodes
        )

        # Check for required texture nodes (Diffuse and Normal are mandatory for FO4)
        required_textures = ['Diffuse', 'Normal']
        # Non-colour texture nodes that must use Non-Color colorspace
        non_color_nodes = {'Normal', 'Specular', 'Glow', 'Environment'}

        for tex_name in required_textures:
            tex_node = nodes.get(tex_name)
            if not tex_node:
                if has_any_tex_image:
                    # PyNifly material — image texture nodes exist but with different names
                    continue
                issues.append(f"Missing {tex_name} texture node")
            elif not tex_node.image:
                issues.append(f"{tex_name} texture is not loaded")
            else:
                img = tex_node.image

                # FO4 requires DDS format - warn if the texture is not DDS
                filepath = getattr(img, 'filepath', '') or ''
                if filepath and not filepath.lower().endswith('.dds'):
                    issues.append(
                        f"{tex_name} texture is not DDS format "
                        f"('{os.path.basename(filepath)}') – FO4 requires DDS. "
                        "Use 'Texture Conversion (NVTT)' to convert."
                    )

                # Blender lazy-loads DDS headers; reload if size is still 0x0.
                if img.size[0] == 0 or img.size[1] == 0:
                    try:
                        img.reload()
                    except Exception:
                        pass
                if img.size[0] == 0 or img.size[1] == 0:
                    issues.append(f"{tex_name} texture has invalid size")

                # FO4 requires power-of-2 dimensions for DDS textures.
                # 512, 1024, 2048, 4096 and 8192 are all valid for 4K / 8K assets.
                width, height = img.size[0], img.size[1]
                if width and not (width & (width - 1) == 0):
                    issues.append(
                        f"{tex_name} width ({width}) is not power of 2 "
                        "(use 512, 1024, 2048, 4096, or 8192)"
                    )
                if height and not (height & (height - 1) == 0):
                    issues.append(
                        f"{tex_name} height ({height}) is not power of 2 "
                        "(use 512, 1024, 2048, 4096, or 8192)"
                    )
                # Warn if the texture is larger than 4096 – FO4 streaming can
                # struggle with 8K+ textures on lower-memory GPUs.
                if width > 4096 or height > 4096:
                    issues.append(
                        f"{tex_name} texture is {width}×{height} – "
                        "textures larger than 4096×4096 may cause streaming "
                        "issues or increased VRAM usage on lower-end GPUs. "
                        "Use 4096×4096 (4K) as the practical maximum."
                    )

        # Check colorspace for all non-colour texture nodes that are loaded
        for node_name in non_color_nodes:
            tex_node = nodes.get(node_name)
            if not tex_node or not tex_node.image:
                continue
            cs = getattr(getattr(tex_node.image, 'colorspace_settings', None), 'name', None)
            if cs and cs not in ('Non-Color', 'Raw'):
                issues.append(
                    f"{node_name} texture colorspace is '{cs}' – must be 'Non-Color' "
                    "to prevent gamma correction in the NIF exporter "
                    "(washed-out normals / wrong specular in Fallout 4)"
                )
        
        if not issues:
            return True, ["Textures are valid for Fallout 4"]
        
        return False, issues

def register():
    """Register texture helper functions and conversion operators"""
    from . import conversion_operators
    conversion_operators.register()

def unregister():
    """Unregister texture helper functions and conversion operators"""
    from . import conversion_operators
    conversion_operators.unregister()
