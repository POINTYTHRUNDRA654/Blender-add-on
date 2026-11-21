"""
python_path_guard.py
====================
Added 2026-11-20 (Blender 5.3 test with other extensions).

A package folder (site-packages / pip --target) built for a different Python version
must never be put on Blender's sys.path: its compiled modules (.pyd) cannot load, and
its pure-Python parts shadow Blender's own packages.  Found live: the saved PyTorch path
pointed at a Python 3.14 site-packages while Blender 5.3 runs Python 3.13; torch could
never load there (WinError 126), but NumPy from that folder took over and made the
UVgami extension's unwrapper crash ("No module named 'numpy._core._multiarray_umath'").

The check reads the compiled-module tags actually present in the folder (cpXY in
"*.cpXY-win_amd64.pyd"), so it does not depend on folder names.
"""
import os
import sys

_TAG_DIRS = ("torch", "numpy", os.path.join("numpy", "_core"), os.path.join("numpy", "core"), "PIL", "scipy")


def folder_python_tag(path: str) -> "str | None":
    """'cp313' etc. for the compiled modules found in the folder, or None when none are found."""
    tags = {}
    try:
        for sub in _TAG_DIRS:
            d = os.path.join(path, sub)
            if not os.path.isdir(d):
                continue
            for name in os.listdir(d):
                if name.endswith(".pyd") or name.endswith(".so"):
                    parts = name.split(".")
                    for p in parts:
                        if p.startswith("cp3") and p[3:].split("-")[0].isdigit():
                            tag = p.split("-")[0]
                            tags[tag] = tags.get(tag, 0) + 1
            if tags:
                break
    except OSError:
        return None
    return max(tags, key=tags.get) if tags else None


def blender_python_tag() -> str:
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def path_is_compatible(path: str) -> "tuple[bool, str]":
    """(True, '') if the folder may go on sys.path; (False, reason) if it was built for another Python."""
    tag = folder_python_tag(path)
    if tag is None or tag == blender_python_tag():
        return True, ""
    found = f"{tag[2]}.{tag[3:]}"
    have = f"{sys.version_info.major}.{sys.version_info.minor}"
    return False, (f"{path} holds packages built for Python {found}, but this Blender runs Python {have}; "
                   f"not added (they cannot load here and would break other add-ons). "
                   f"Install PyTorch for Python {have} or clear the PyTorch path in the add-on preferences.")
