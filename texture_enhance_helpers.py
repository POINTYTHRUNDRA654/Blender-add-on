"""
texture_enhance_helpers.py
==========================
AI texture enhancement for Fallout 4: more detail and a better finish from
the same texture.

Pipeline
--------
1. Source: a texture on the active object inside Blender (packed, baked or
   never saved) or a DDS/PNG file.
2. AI upscale 4x with Real-ESRGAN (the add-on's installed copy). If no AI
   upscaler works, a plain Lanczos resize is used and the result SAYS so.
3. From that high-resolution intermediate:
   * Normal map: height from the colour detail at two scales (fine + broad),
     correct DirectX signs (green down), renormalised after resizing, and
     added on top of the mesh's existing normal map (whiteout blend) so the
     baked shape is kept and the new surface detail is laid over it.
   * Specular map (_s), two real channels, as the game reads them (verified
     in the game's shaders): red = specular strength, green = gloss.
     Ranges come from the chosen surface type, measured from vanilla _s
     maps; gloss varies with surface roughness and crevices instead of being
     a flat grey copy of the colour (the "everything looks like plastic"
     look).
4. Resize every map to the output size with Lanczos; a light sharpen on the
   colour map keeps it crisp after downsizing.
5. Optional DDS: BC7 diffuse (sRGB-safe), BC5 normal, BC5 specular, full
   mipmaps with a cubic filter; alpha coverage kept for cutout textures.
"""

import os
import subprocess
import tempfile
from pathlib import Path

import bpy

# ---------------------------------------------------------------------------
# Tiers (source size -> AI intermediate)
# ---------------------------------------------------------------------------

ENHANCE_TIERS = {
    "1K->4K":  {"label": "1K -> 4K (Xbox One / Low-End PC)",  "src_size": 1024,  "scale": 4,  "note": "Best for Xbox One and older hardware"},
    "2K->8K":  {"label": "2K -> 8K (Mid-Range PC / PS5)",    "src_size": 2048,  "scale": 4,  "note": "Great balance of quality and VRAM"},
    "4K->16K": {"label": "4K -> 16K (High-End PC)",           "src_size": 4096,  "scale": 4,  "note": "Maximum detail -- requires 6GB+ VRAM"},
}

TIER_ITEMS = [
    ("1K_4K",  "1K -> 4K  (Xbox One / Low-End PC)",  "Uses 4K AI intermediate"),
    ("2K_8K",  "2K -> 8K  (Mid-Range PC / PS5)",     "Uses 8K AI intermediate"),
    ("4K_16K", "4K -> 16K (High-End PC Ultra)",      "Uses 16K AI intermediate -- needs 8GB+ RAM"),
]

# Specular (red) and gloss (green) as (10th percentile, median, 90th
# percentile), measured 2026-11-05 from 40 vanilla _s maps per category
# (texels of UV padding excluded).
SURFACES = {
    "ORGANIC":  ("Organic (plants, trees, fungus)", (0.04, 0.12, 0.40), (0.03, 0.13, 0.34)),
    "CREATURE": ("Creature (skin, flesh, fur)",     (0.04, 0.21, 0.41), (0.05, 0.22, 0.58)),
    "STONE":    ("Rock / ground",                   (0.04, 0.10, 0.29), (0.07, 0.21, 0.42)),
    "WOOD":     ("Wood",                            (0.11, 0.22, 0.52), (0.16, 0.31, 0.56)),
    "METAL":    ("Metal (weapons, machinery)",      (0.06, 0.26, 0.55), (0.08, 0.32, 0.60)),
    "CLOTH":    ("Cloth",                           (0.08, 0.25, 0.51), (0.06, 0.19, 0.57)),
}
SURFACE_ITEMS = [
    (k, v[0], f"Specular {v[1][0]:.2f}-{v[1][2]:.2f} (typical {v[1][1]:.2f}), gloss {v[2][0]:.2f}-{v[2][2]:.2f} "
              f"(typical {v[2][1]:.2f}), measured from vanilla _s maps")
    for k, v in SURFACES.items()
]

OUT_SIZE_ITEMS = [
    ("1", "Same size", "Keep the source resolution (all the detail goes into the same pixels)"),
    ("2", "2x", "Double the resolution"),
    ("4", "4x", "Four times the resolution (the full AI result)"),
]


def _prefs():
    try:
        from . import preferences as _prefs_mod
        return _prefs_mod.get_preferences()
    except Exception:
        return None


def _texconv():
    p = _prefs()
    t = getattr(p, 'texconv_path', '').strip() if p else ''
    return t if t and os.path.isfile(t) else ''


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _convert_dds_to_png(dds_path: str, out_dir: str) -> tuple:
    """Convert DDS -> PNG. Returns (success, png_path_or_error)."""
    texconv = _texconv()
    out_png = os.path.join(out_dir, Path(dds_path).stem + ".png")
    if texconv:
        result = subprocess.run([texconv, "-nologo", "-y", "-ft", "png", "-o", out_dir, dds_path],
                                capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and os.path.isfile(out_png):
            return True, out_png
    try:
        img = bpy.data.images.load(dds_path)
        try:
            from .texture_inblender import image_to_png
            return True, image_to_png(img, out_dir, Path(dds_path).stem)
        finally:
            bpy.data.images.remove(img)
    except Exception as exc:
        return False, f"Could not convert DDS to PNG: {exc}"


def _ai_upscale(png_path: str, scale: int = 4, out_dir: str = None, tile_size: int = 512) -> tuple:
    """AI upscale. Returns (ok, path_or_error, method, message); method is
    'ai' (Real-ESRGAN, size checked) or 'lanczos' (no AI was available)."""
    from .texture_inblender import ai_upscale_png
    out_dir = out_dir or os.path.dirname(png_path)
    out_path = os.path.join(out_dir, f"{Path(png_path).stem}_x{scale}.png")
    ok, method, msg = ai_upscale_png(png_path, out_path, scale)
    return ok, (out_path if ok else msg), method, msg


# ---------------------------------------------------------------------------
# NumPy-only filters (SciPy's ndimage fails to load in some Blender installs:
# "DLL load failed while importing _ufuncs", seen in Blender 5.1, 2026-11-05)
# ---------------------------------------------------------------------------

def _box1d(a, r, axis):
    import numpy as np
    if r < 1:
        return a
    pad = [(0, 0)] * a.ndim
    pad[axis] = (r + 1, r)
    c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis, dtype=np.float64)
    n = a.shape[axis]
    hi = np.take(c, np.arange(2 * r + 1, 2 * r + 1 + n), axis=axis)
    lo = np.take(c, np.arange(0, n), axis=axis)
    return ((hi - lo) / (2 * r + 1)).astype(np.float32)


def _gauss1d(a, sigma, axis):
    import numpy as np
    r = max(int(np.ceil(3 * sigma)), 1)
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    pad = [(0, 0)] * a.ndim
    pad[axis] = (r, r)
    p = np.pad(a, pad, mode="edge")
    n = a.shape[axis]
    out = np.zeros_like(a, dtype=np.float32)
    for i, w in enumerate(k):
        out += w * np.take(p, np.arange(i, i + n), axis=axis)
    return out


def _blur(a, sigma):
    """Gaussian blur without SciPy. Up to sigma 4: an exact kernel. Larger:
    shrink, blur exactly, scale back (a wide blur keeps only smooth shapes,
    so nothing is lost and it stays fast on 8K images)."""
    import numpy as np
    a = a.astype(np.float32)
    if sigma <= 0.3:
        return a
    if sigma <= 4.0:
        return _gauss1d(_gauss1d(a, sigma, 0), sigma, 1)
    from PIL import Image
    f = max(int(sigma // 2), 2)
    h, w = a.shape
    sw, sh = max(w // f, 8), max(h // f, 8)
    small = np.asarray(Image.fromarray(a, "F").resize((sw, sh), Image.BOX), dtype=np.float32)
    s = sigma * sw / float(w)
    small = _gauss1d(_gauss1d(small, s, 0), s, 1)
    return np.asarray(Image.fromarray(small, "F").resize((w, h), Image.BICUBIC), dtype=np.float32)


def _sobel(a, axis):
    """Sobel derivative along *axis* (1 = x to the right, 0 = rows down)."""
    import numpy as np
    p = np.pad(a, 1, mode="edge")
    if axis == 1:
        d = p[:, 2:] - p[:, :-2]
        return (d[:-2] + 2 * d[1:-1] + d[2:]).astype(np.float32)
    d = p[2:, :] - p[:-2, :]
    return (d[:, :-2] + 2 * d[:, 1:-1] + d[:, 2:]).astype(np.float32)


def _luminance(path):
    import numpy as np
    from PIL import Image
    with Image.open(path) as im:
        a = np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0
    return a[..., 0] * 0.2126 + a[..., 1] * 0.7152 + a[..., 2] * 0.0722


def _encode_normals(n):
    import numpy as np
    n = n / np.maximum(np.linalg.norm(n, axis=-1, keepdims=True), 1e-6)
    return ((n * 0.5 + 0.5) * 255.0 + 0.5).clip(0, 255).astype(np.uint8)


def _decode_normals(path, size=None):
    import numpy as np
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        if size and im.size != tuple(size):
            im = im.resize(tuple(size), Image.LANCZOS)
        n = np.asarray(im, dtype=np.float32) / 127.5 - 1.0
    return n / np.maximum(np.linalg.norm(n, axis=-1, keepdims=True), 1e-6)


def _generate_normal_map(diffuse_path: str, strength: float = 2.5, out_path: str = None,
                         px_scale: float = 1.0, fine_sigma: float = 1.0,
                         broad_sigma: float = 4.0, broad_weight: float = 0.6) -> tuple:
    """DirectX tangent-space detail normal from a colour texture.

    Height = luminance, read at a fine and a broad scale so both small grain
    and larger forms show. Signs: a bright (raised) spot tilts its right side
    to +X and its lower side to +Y-down (DirectX, green down). *px_scale* is
    intermediate pixels per output pixel, so the slope is measured per output
    pixel and the strength does not change with the AI upscale factor.
    Returns (success, path_or_error)."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError as e:
        return False, f"NumPy / Pillow not available: {e}"
    try:
        h = _luminance(diffuse_path)
        hf = _blur(h, fine_sigma * px_scale)
        hb = _blur(h, broad_sigma * px_scale)
        k = strength * px_scale / 8.0          # sobel = 8 x slope
        gx = (_sobel(hf, 1) + broad_weight * _sobel(hb, 1)) * k
        gy = (_sobel(hf, 0) + broad_weight * _sobel(hb, 0)) * k   # rows grow downwards
        n = np.stack([-gx, -gy, np.ones_like(gx)], -1)                    # DirectX: green down
        out_path = out_path or os.path.splitext(diffuse_path)[0] + "_n.png"
        Image.fromarray(_encode_normals(n), "RGB").save(out_path)
        return True, out_path
    except Exception as exc:
        return False, f"Normal map generation failed: {exc}"


def _combine_normals(base_path, detail_path, out_path, size):
    """Lay *detail* over *base* (whiteout blend), both DirectX, at *size*."""
    import numpy as np
    from PIL import Image
    b = _decode_normals(base_path, size)
    d = _decode_normals(detail_path, size)
    n = np.stack([b[..., 0] + d[..., 0], b[..., 1] + d[..., 1], b[..., 2] * d[..., 2]], -1)
    Image.fromarray(_encode_normals(n), "RGB").save(out_path)
    return out_path


def _resize_normals(src, dst, size):
    from PIL import Image
    Image.fromarray(_encode_normals(_decode_normals(src, size)), "RGB").save(dst)
    return dst


def _remap(t, rng):
    """Map 0..1 values onto (lo, typical, hi) so the median lands on typical."""
    import numpy as np
    lo, mid, hi = rng
    t = np.clip(t, 0.0, 1.0)
    med = float(np.median(t))
    want = (mid - lo) / max(hi - lo, 1e-6)
    g = 1.0
    if 0.02 < med < 0.98 and 0.02 < want < 0.98:
        g = float(np.clip(np.log(want) / np.log(med), 0.3, 3.0))
    return lo + (hi - lo) * t ** g


def _generate_specular_map(diffuse_path: str, out_path: str = None, surface: str = "ORGANIC",
                           px_scale: float = 1.0) -> tuple:
    """Two-channel FO4 _s map: red = specular strength, green = gloss.

    * Specular follows brightness, lowered in crevices.
    * Gloss is highest on smooth, raised areas and lowest where the surface
      is rough (lots of fine detail) or in crevices, so it varies across the
      surface instead of copying the colour.
    Both are mapped into the vanilla range for *surface*. Blue is 0 (the
    game does not read it). Returns (success, path_or_error, stats)."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError as e:
        return False, f"NumPy / Pillow not available: {e}", {}
    try:
        L = _luminance(diffuse_path)
        lo, hi = np.percentile(L, (2, 98))
        Ln = np.clip((L - lo) / max(hi - lo, 1e-6), 0, 1)
        s = 2.0 * px_scale
        mu = _blur(L, s)
        rough = np.sqrt(np.maximum(_blur(L * L, s) - mu * mu, 0))
        rough = np.clip(rough / max(float(np.percentile(rough, 95)), 1e-6), 0, 1)
        cav = np.maximum(_blur(L, 8.0 * px_scale) - L, 0)
        cav = np.clip(cav / max(float(np.percentile(cav, 95)), 1e-6), 0, 1)
        spec_t = _blur(0.6 * Ln + 0.4 * (1 - cav), 0.7 * px_scale)
        gloss_t = _blur(0.5 * (1 - rough) + 0.3 * (1 - cav) + 0.2 * Ln, 0.7 * px_scale)
        _label, srng, grng = SURFACES.get(surface, SURFACES["ORGANIC"])
        R = _remap(spec_t, srng)
        G = _remap(gloss_t, grng)
        rgb = np.stack([R, G, np.zeros_like(R)], -1)
        out_path = out_path or os.path.splitext(diffuse_path)[0] + "_s.png"
        Image.fromarray((rgb * 255 + 0.5).clip(0, 255).astype(np.uint8), "RGB").save(out_path)
        stats = {"spec_median": float(np.median(R)), "gloss_median": float(np.median(G)),
                 "gloss_p10": float(np.percentile(G, 10)), "gloss_p90": float(np.percentile(G, 90))}
        return True, out_path, stats
    except Exception as exc:
        return False, f"Specular map generation failed: {exc}", {}


def _downscale_perceptual(image_path: str, target_size: tuple, out_path: str = None,
                          sharpen: bool = False) -> tuple:
    """Lanczos resize to target_size (+ light unsharp mask when *sharpen*)."""
    try:
        from PIL import Image, ImageFilter
        with Image.open(image_path) as img:
            out = img.resize(tuple(target_size), Image.LANCZOS) if img.size != tuple(target_size) else img.copy()
        if sharpen:
            if out.mode == "RGBA":
                rgb, a = out.convert("RGB"), out.getchannel("A")
                rgb = rgb.filter(ImageFilter.UnsharpMask(radius=0.8, percent=60, threshold=2))
                out = Image.merge("RGBA", (*rgb.split(), a))
            else:
                out = out.filter(ImageFilter.UnsharpMask(radius=0.8, percent=60, threshold=2))
        if out_path is None:
            out_path = os.path.join(os.path.dirname(image_path),
                                    f"{Path(image_path).stem}_{target_size[0]}x{target_size[1]}.png")
        out.save(out_path, "PNG")
        return True, out_path
    except Exception as exc:
        return False, f"Downscale failed: {exc}"


def _alpha_cutout(png_path):
    """True when the image has real alpha (cutout leaves etc.)."""
    try:
        from PIL import Image
        with Image.open(png_path) as im:
            if im.mode != "RGBA":
                return False
            lo, _hi = im.getchannel("A").getextrema()
            return lo < 250
    except Exception:
        return False


def _convert_png_to_dds(png_path: str, out_dir: str, fmt: str = "BC7_UNORM", color: bool = True,
                        keep_coverage: bool = False) -> tuple:
    """PNG -> DDS via texconv with full mipmaps (cubic filter).

    Colour maps get -srgb (without it texconv darkens PNG colour, measured).
    Normal/specular data get --ignore-srgb so values and mip filtering stay
    on the raw numbers. Cutout textures keep their alpha coverage in mips
    (alpha test 0.5, as vanilla's 127 threshold)."""
    texconv = _texconv()
    if not texconv:
        return False, "texconv not configured in add-on preferences"
    dds_out = os.path.join(out_dir, Path(png_path).stem + ".dds")
    cmd = [texconv, "-nologo", "-y", "-f", fmt, "-m", "0", "-if", "CUBIC"]
    cmd += ["-srgb"] if color else ["--ignore-srgb"]
    if fmt.startswith("BC7"):
        cmd += ["-bc", "x"]
    if keep_coverage:
        cmd += ["--keep-coverage", "0.5"]
    cmd += ["-o", out_dir, png_path]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode == 0 and os.path.isfile(dds_out):
        return True, dds_out
    return False, f"texconv failed: {(result.stderr or result.stdout).strip()[:300]}"


# ---------------------------------------------------------------------------
# Main enhancement pipeline
# ---------------------------------------------------------------------------

def enhance_texture(texture_path: str,
                    tier: str = "1K_4K",
                    output_dir: str = None,
                    normal_strength: float = 2.5,
                    generate_normal: bool = True,
                    generate_specular: bool = True,
                    to_dds: bool = True,
                    tile_size: int = 512,
                    out_scale: int = 1,
                    surface: str = "ORGANIC",
                    base_normal_path: str = None,
                    sharpen: bool = True,
                    name_stem: str = None) -> dict:
    """Run the enhancement pipeline on *texture_path* (DDS or PNG).

    Returns dict: success, message, steps, upscale_method ('ai'/'lanczos'),
    diffuse_png/normal_png/specular_png (always PNG), diffuse_path/
    normal_path/specular_path (DDS when to_dds worked, else the PNGs),
    size (output width, height)."""
    steps = []
    result = {"success": False, "message": "", "steps": steps, "tier": tier, "upscale_method": None,
              "diffuse_path": None, "normal_path": None, "specular_path": None,
              "diffuse_png": None, "normal_png": None, "specular_png": None, "size": None}

    if not os.path.isfile(texture_path):
        result["message"] = f"File not found: {texture_path}"
        return result
    work_dir = output_dir or tempfile.mkdtemp(prefix="fo4_tex_enhance_")
    os.makedirs(work_dir, exist_ok=True)
    stem = name_stem or Path(texture_path).stem
    base_stem = stem[:-2] if stem.lower().endswith("_d") else stem

    if Path(texture_path).suffix.lower() == ".dds":
        ok, png_path = _convert_dds_to_png(texture_path, work_dir)
        if not ok:
            result["message"] = png_path
            return result
        steps.append(f"DDS -> PNG: {os.path.basename(png_path)}")
    else:
        png_path = texture_path

    from .texture_inblender import png_size
    src_w, src_h = png_size(png_path)
    out_w, out_h = src_w * int(out_scale), src_h * int(out_scale)
    result["size"] = (out_w, out_h)

    ok, up_path, method, msg = _ai_upscale(png_path, 4, work_dir, tile_size)
    if not ok:
        result["message"] = f"Upscale failed: {up_path}"
        return result
    result["upscale_method"] = method
    steps.append(("AI upscale: " if method == "ai" else "WARNING, not AI: ") + msg)
    inter_w, _inter_h = png_size(up_path)
    px_scale = inter_w / float(out_w)

    # colour
    diff_png = os.path.join(work_dir, f"{stem}_enhanced.png")
    ok, diff_png = _downscale_perceptual(up_path, (out_w, out_h), diff_png, sharpen=sharpen)
    if not ok:
        result["message"] = diff_png
        return result
    result["diffuse_png"] = diff_png
    steps.append(f"Colour: {inter_w}px -> {out_w}x{out_h} (Lanczos{' + light sharpen' if sharpen else ''})")

    # normal
    if generate_normal:
        ok, det = _generate_normal_map(up_path, normal_strength, os.path.join(work_dir, f"{base_stem}_detail_n_full.png"),
                                       px_scale=px_scale)
        if ok:
            nrm_png = os.path.join(work_dir, f"{base_stem}_n_enhanced.png")
            if base_normal_path and os.path.isfile(base_normal_path):
                det_small = _resize_normals(det, os.path.join(work_dir, f"{base_stem}_detail_n.png"), (out_w, out_h))
                _combine_normals(base_normal_path, det_small, nrm_png, (out_w, out_h))
                steps.append("Normal: new surface detail laid over the existing normal map (whiteout blend), DirectX")
            else:
                _resize_normals(det, nrm_png, (out_w, out_h))
                steps.append("Normal: detail normal from the AI intermediate, DirectX (no existing normal map to keep)")
            result["normal_png"] = nrm_png
        else:
            steps.append(f"WARNING: normal map skipped: {det}")

    # specular / gloss
    if generate_specular:
        ok, spec_full, stats = _generate_specular_map(up_path, os.path.join(work_dir, f"{base_stem}_s_full.png"),
                                                      surface=surface, px_scale=px_scale)
        if ok:
            spec_png = os.path.join(work_dir, f"{base_stem}_s_enhanced.png")
            _downscale_perceptual(spec_full, (out_w, out_h), spec_png)
            result["specular_png"] = spec_png
            steps.append(f"Specular: red = specular (median {stats['spec_median']:.2f}), green = gloss "
                         f"({stats['gloss_p10']:.2f}-{stats['gloss_p90']:.2f}), {SURFACES[surface][0]} range")
        else:
            steps.append(f"WARNING: specular map skipped: {spec_full}")

    result["diffuse_path"], result["normal_path"], result["specular_path"] = (
        result["diffuse_png"], result["normal_png"], result["specular_png"])

    if to_dds:
        ok, dds = _convert_png_to_dds(diff_png, work_dir, "BC7_UNORM", color=True,
                                      keep_coverage=_alpha_cutout(diff_png))
        if ok:
            result["diffuse_path"] = dds
            steps.append("Colour -> BC7 DDS, full mipmaps")
        else:
            steps.append(f"WARNING: colour DDS skipped: {dds}")
        for key, label in (("normal", "Normal"), ("specular", "Specular")):
            png = result[f"{key}_png"]
            if png:
                ok, dds = _convert_png_to_dds(png, work_dir, "BC5_UNORM", color=False)
                if ok:
                    result[f"{key}_path"] = dds
                    steps.append(f"{label} -> BC5 DDS, full mipmaps")
                else:
                    steps.append(f"WARNING: {label.lower()} DDS skipped: {dds}")

    maps = sum(1 for k in ("diffuse_png", "normal_png", "specular_png") if result[k])
    result["success"] = True
    result["message"] = (f"Enhanced: {maps} maps at {out_w}x{out_h} from a {inter_w}px "
                         + ("AI intermediate" if method == "ai" else "plain resize (NOT AI: no AI upscaler worked)"))
    return result


# Backward-compat alias for old calls
def enhance_texture_1k(texture_path, output_dir=None, normal_strength=2.5,
                       generate_normal=True, to_dds=True):
    return enhance_texture(texture_path, tier="1K_4K", output_dir=output_dir,
                           normal_strength=normal_strength,
                           generate_normal=generate_normal, to_dds=to_dds)


# ---------------------------------------------------------------------------
# In-Blender application
# ---------------------------------------------------------------------------

def wire_specular_rg(mat, spec_node):
    """Preview the _s map in Blender like the game uses it: red -> Specular
    IOR Level, green (gloss) -> Roughness (inverted). Only free inputs."""
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if bsdf is None or spec_node is None:
        return
    spec_in = bsdf.inputs.get('Specular IOR Level') or bsdf.inputs.get('Specular')
    rough_in = bsdf.inputs.get('Roughness')
    for l in list(links):
        if l.from_node is spec_node and l.to_socket in (spec_in,):
            links.remove(l)
    sep = nodes.get("FO4 _s channels")
    if sep is None:
        sep = nodes.new('ShaderNodeSeparateColor')
        sep.name = sep.label = "FO4 _s channels"
        sep.location = (spec_node.location.x + 260, spec_node.location.y)
    if not sep.inputs[0].is_linked:
        links.new(spec_node.outputs['Color'], sep.inputs[0])
    if spec_in is not None and not spec_in.is_linked:
        links.new(sep.outputs[0], spec_in)
    if rough_in is not None and not rough_in.is_linked:
        inv = nodes.new('ShaderNodeMath')
        inv.operation = 'SUBTRACT'
        inv.name = inv.label = "Gloss to Roughness"
        inv.inputs[0].default_value = 1.0
        inv.location = (sep.location.x + 180, sep.location.y - 120)
        links.new(sep.outputs[1], inv.inputs[1])
        links.new(inv.outputs[0], rough_in)


def enhance_object_image(obj, image_name, out_scale=1, surface="ORGANIC", normal_strength=2.5,
                         generate_normal=True, generate_specular=True, combine_normal=True,
                         apply=True, save_dir="", to_dds=False):
    """Enhance an image on *obj* inside Blender. Returns (ok, message, level, result)."""
    from . import texture_inblender as inb
    hit = inb.find_image_on_object(obj, image_name)
    if not hit:
        return False, f"Texture '{image_name}' is not on the active object", 'ERROR', None
    slot, mat, node, img = hit
    if slot in ("Normal", "Specular"):
        return False, f"'{img.name}' is a {slot.lower()} map. Pick the colour (diffuse) texture to enhance.", 'ERROR', None
    work = tempfile.mkdtemp(prefix="fo4_enhance_", dir=inb.work_dir())
    stem = inb._safe(img.name)
    src = inb.image_to_png(img, work, stem)
    base_nrm, nrm_img = None, None
    if generate_normal and combine_normal:
        hitn = next(((s, m, n, i) for s, m, n, i in inb.object_images(obj) if s == "Normal" and m is mat), None)
        if hitn:
            nrm_img = hitn[3]
            base_nrm = inb.image_to_png(nrm_img, work, inb._safe(nrm_img.name) + "_base")
    res = enhance_texture(src, output_dir=work, normal_strength=normal_strength, generate_normal=generate_normal,
                          generate_specular=generate_specular, to_dds=False, out_scale=int(out_scale),
                          surface=surface, base_normal_path=base_nrm, name_stem=stem)
    if not res["success"]:
        return False, res["message"], 'ERROR', res
    w, h = res["size"]
    made = []
    d_img = inb.load_png(res["diffuse_png"], f"{img.name}_enhanced", img.colorspace_settings.name or 'sRGB')
    made.append(d_img)
    if apply:
        inb.assign(obj, mat, node, d_img)
    if res["normal_png"]:
        n_name = (nrm_img.name if nrm_img else f"{obj.name}_Normal") + "_enhanced"
        n_img = inb.load_png(res["normal_png"], n_name, 'Non-Color')
        made.append(n_img)
        if apply:
            nnode = inb.assign(obj, mat, None, n_img, slot="Normal")
            if nnode is not None:
                try:
                    from .texture_helpers import TextureHelpers
                    TextureHelpers._wire_texture_slot_node(mat, "Normal", nnode)
                except Exception:
                    pass
    if res["specular_png"]:
        s_img = inb.load_png(res["specular_png"], f"{obj.name}_Specular_enhanced", 'Non-Color')
        made.append(s_img)
        if apply:
            from .texture_helpers import TextureHelpers
            snode = TextureHelpers._find_slot_node(mat, "Specular")
            if snode is None:
                snode = mat.node_tree.nodes.new('ShaderNodeTexImage')
                snode.name = snode.label = "Specular"
                bsdf = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
                if bsdf is not None:
                    snode.location = (bsdf.location.x - 700, bsdf.location.y - 420)
            snode.image = s_img
            wire_specular_rg(mat, snode)
    saved = []
    if save_dir:
        folder = bpy.path.abspath(save_dir)
        os.makedirs(folder, exist_ok=True)
        for im in made:
            saved.append(inb.save_copy(im, folder))
        if to_dds:
            for p in list(saved):
                is_color = p.endswith(f"{inb._safe(d_img.name)}.png")
                fmt = "BC7_UNORM" if is_color else "BC5_UNORM"
                ok, dds = _convert_png_to_dds(p, folder, fmt, color=is_color,
                                              keep_coverage=is_color and _alpha_cutout(p))
                if ok:
                    saved.append(dds)
    level = 'INFO' if res["upscale_method"] == "ai" else 'WARNING'
    msg = res["message"] + f" | new images in the .blend: {', '.join(i.name for i in made)}"
    if apply:
        msg += " (plugged into the material; the old images are kept)"
    if saved:
        msg += f" | saved: {folder}"
    return True, msg, level, res


# ---------------------------------------------------------------------------
# Blender Operators
# ---------------------------------------------------------------------------

def _image_items(self, context):
    from . import texture_inblender as inb
    return inb.image_enum_items(self, context)


class FO4_OT_EnhanceTexture(bpy.types.Operator):
    """Add detail and a better finish to a texture: AI upscale, a detail normal map laid over
    the existing one, and a real specular/gloss map. Works on the active object's texture inside
    Blender (no saved file needed) or on a file"""
    bl_idname  = "fo4.enhance_texture_1k"   # keep old idname for compat
    bl_label   = "Enhance Texture (AI)"
    bl_options = {'REGISTER', 'UNDO'}

    source: bpy.props.EnumProperty(
        name="Source", default='OBJECT', options={'SKIP_SAVE'},
        items=[('OBJECT', "Texture on Active Object", "A texture inside Blender on the active object"),
               ('FILE', "File on Disk", "A DDS or PNG file")])
    image_name: bpy.props.EnumProperty(name="Texture", items=_image_items,
                                       description="Colour (diffuse) texture on the active object")
    filepath: bpy.props.StringProperty(name="Texture", subtype='FILE_PATH', options={'SKIP_SAVE'},
                                       description="Source DDS or PNG texture")
    tier: bpy.props.EnumProperty(name="Enhancement Tier", items=TIER_ITEMS, default="1K_4K",
                                 description="Kept for old presets; the AI step is always 4x the source")
    out_scale: bpy.props.EnumProperty(name="Output Size", items=OUT_SIZE_ITEMS, default="2")
    surface: bpy.props.EnumProperty(name="Surface", items=SURFACE_ITEMS, default="ORGANIC",
                                    description="Sets the specular and gloss ranges (measured from vanilla _s maps)")
    output_dir: bpy.props.StringProperty(
        name="Output Folder", subtype='DIR_PATH', default="",
        description="File mode: where to save (default: next to the source). Object mode: optional, "
                    "also save copies here; leave empty to keep the results only in the .blend")
    normal_strength: bpy.props.FloatProperty(name="Normal Strength", default=2.5, min=0.5, max=8.0,
                                             description="How strongly the surface detail shows in the normal map")
    generate_normal: bpy.props.BoolProperty(name="Normal Map", default=True)
    combine_normal: bpy.props.BoolProperty(
        name="Keep Existing Normal Map", default=True,
        description="Lay the new detail over the mesh's current normal map instead of replacing it "
                    "(keeps the shape baked from a high-detail source)")
    generate_specular: bpy.props.BoolProperty(name="Specular Map", default=True,
                                              description="Red = specular strength, green = gloss")
    apply_to_object: bpy.props.BoolProperty(name="Use on Object", default=True,
                                            description="Plug the new maps into the material (old images stay in the .blend)")
    to_dds: bpy.props.BoolProperty(
        name="Also Write DDS", default=True,
        description="BC7 colour, BC5 normal, BC5 specular, full mipmaps (only when a folder is set in object mode)")

    def _object_mode(self):
        return self.source == 'OBJECT' and not self.filepath

    def invoke(self, context, event):
        if self._object_mode():
            from . import texture_inblender as inb
            obj = context.active_object
            if not inb.object_images(obj):
                self.report({'ERROR'}, "The active object has no textures. Select a textured mesh, or use 'From File'.")
                return {'CANCELLED'}
            name = inb.default_image_name(obj)
            if name:
                self.image_name = name
            return context.window_manager.invoke_props_dialog(self, width=480)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        col = self.layout.column()
        if self._object_mode():
            col.prop(self, "image_name")
        col.prop(self, "out_scale")
        col.prop(self, "surface")
        row = col.row(align=True)
        row.prop(self, "generate_normal")
        row.prop(self, "generate_specular")
        sub = col.column()
        sub.enabled = self.generate_normal
        sub.prop(self, "normal_strength")
        if self._object_mode():
            sub.prop(self, "combine_normal")
            col.prop(self, "apply_to_object")
            col.prop(self, "output_dir", text="Also Save To")
        else:
            col.prop(self, "output_dir")
        col.prop(self, "to_dds")

    def execute(self, context):
        if self._object_mode():
            ok, msg, level, res = enhance_object_image(
                context.active_object, self.image_name, int(self.out_scale), self.surface,
                self.normal_strength, self.generate_normal, self.generate_specular, self.combine_normal,
                self.apply_to_object, self.output_dir, self.to_dds and bool(self.output_dir))
            for step in (res or {}).get("steps", []):
                print(f"[Texture Enhance] {step}")
            self.report({level}, msg)
            return {'FINISHED'} if ok else {'CANCELLED'}

        path = bpy.path.abspath(self.filepath)
        if not os.path.isfile(path):
            self.report({'ERROR'}, f"File not found: {path}")
            return {'CANCELLED'}
        out_dir = bpy.path.abspath(self.output_dir) if self.output_dir else os.path.dirname(path)
        os.makedirs(out_dir, exist_ok=True)
        result = enhance_texture(texture_path=path, tier=self.tier, output_dir=out_dir,
                                 normal_strength=self.normal_strength, generate_normal=self.generate_normal,
                                 generate_specular=self.generate_specular, to_dds=self.to_dds,
                                 out_scale=int(self.out_scale), surface=self.surface)
        for step in result.get("steps", []):
            print(f"[Texture Enhance] {step}")
        if result["success"]:
            self.report({'INFO'} if result["upscale_method"] == "ai" else {'WARNING'}, result["message"])
        else:
            self.report({'ERROR'}, result["message"])
            return {'CANCELLED'}
        return {'FINISHED'}


class FO4_OT_BatchEnhanceTextures(bpy.types.Operator):
    """Batch-enhance all textures in a folder."""
    bl_idname  = "fo4.batch_enhance_1k_textures"   # keep old idname for compat
    bl_label   = "Batch Enhance Textures (AI)"
    bl_options = {'REGISTER'}

    directory: bpy.props.StringProperty(name="Texture Folder", subtype='DIR_PATH',
                                        description="Folder of DDS textures to enhance")
    tier: bpy.props.EnumProperty(name="Enhancement Tier", items=TIER_ITEMS, default="1K_4K")
    out_scale: bpy.props.EnumProperty(name="Output Size", items=OUT_SIZE_ITEMS, default="1")
    surface: bpy.props.EnumProperty(name="Surface", items=SURFACE_ITEMS, default="ORGANIC")
    normal_strength: bpy.props.FloatProperty(name="Normal Strength", default=2.5, min=0.5, max=8.0)
    diffuse_only: bpy.props.BoolProperty(name="Diffuse Only (_d.dds)",
                                         description="Only enhance diffuse textures, skip normal/specular",
                                         default=True)

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        folder = bpy.path.abspath(self.directory)
        if not os.path.isdir(folder):
            self.report({'ERROR'}, f"Folder not found: {folder}")
            return {'CANCELLED'}
        out_dir = os.path.join(folder, "enhanced")
        os.makedirs(out_dir, exist_ok=True)
        import glob
        files = (glob.glob(os.path.join(folder, "*_d.dds")) if self.diffuse_only
                 else glob.glob(os.path.join(folder, "*.dds")) + glob.glob(os.path.join(folder, "*.png")))
        if not files:
            self.report({'WARNING'}, "No textures found")
            return {'CANCELLED'}
        ok_count = fail_count = not_ai = 0
        for tex in files:
            res = enhance_texture(tex, tier=self.tier, output_dir=out_dir, normal_strength=self.normal_strength,
                                  generate_normal=True, to_dds=True, out_scale=int(self.out_scale),
                                  surface=self.surface)
            if res["success"]:
                ok_count += 1
                not_ai += res["upscale_method"] != "ai"
                print(f"[Batch] OK {os.path.basename(tex)}")
            else:
                fail_count += 1
                print(f"[Batch] FAILED {os.path.basename(tex)}: {res['message']}")
        msg = f"Batch: {ok_count} enhanced, {fail_count} failed -> {out_dir}"
        if not_ai:
            msg += f" | {not_ai} used a plain resize, NOT AI (no AI upscaler worked)"
        self.report({'WARNING'} if (fail_count or not_ai) else {'INFO'}, msg)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_CLASSES = [
    FO4_OT_EnhanceTexture,
    FO4_OT_BatchEnhanceTextures,
]


def register():
    for cls in _CLASSES:
        try:
            bpy.utils.register_class(cls)
        except Exception:
            pass


def unregister():
    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
