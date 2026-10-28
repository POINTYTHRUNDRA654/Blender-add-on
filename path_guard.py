"""Refuse to save to a path that is not a full path.

Through the file browser a path is always absolute. When an operator is run
without it (by Mossy, a script or a re-used button), an empty or bare file name
would be written into whatever folder Blender was started from.
"""
import os

import bpy


def output_path_ok(op, path, what="where to save"):
    """True if *path* resolves to an absolute path; otherwise report and return False."""
    p = bpy.path.abspath(path) if path else ""
    if p and os.path.isabs(p) and os.path.basename(p.rstrip("\\/")) not in ("", ".nif", ".fbx"):
        return True
    op.report({'ERROR'}, f"Choose {what} first (a full path, not just a file name)")
    return False
