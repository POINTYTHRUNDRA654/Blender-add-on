# Lessons learned the hard way

Rules below exist because a specific real incident happened. Read them
before writing anything that calls a `bpy.ops` operator or writes a file
to a user's Data/mod folder.

## Never trust a `bpy.ops` return value alone for anything that writes files

A Blender operator can return `{'CANCELLED'}` (or even an empty `set()`)
without raising an exception -- a failed `poll()`, no valid selection, a
context quirk (this addon has its own documented one: PyNifly's `execute()`
raising when called from inside a `FILE_BROWSER` modal context, which
Blender swallows and reports back as an empty `set()`). None of that is an
error in the Python sense, so code that only wraps the call in
`try/except` and treats "didn't raise" as "it worked" will confidently
report success while nothing actually happened.

`export_helpers.py`'s `_call_nif_export` already learned this the hard
way and does the right thing: it doesn't trust the operator's return value
at all, it verifies the output file actually landed on disk
(`_export_output_written`, timestamped so an earlier export in the same
folder can't be mistaken for this one's). That lesson had not propagated
anywhere else that imports or exports a NIF -- three separate call sites
(`mesh_helpers.import_game_nif`, `asset_library.py`'s Import Asset NIF
branch, `fo4_skeleton_helpers.import_fo4_skeleton`) each reinvented NIF
import with no equivalent check, trusting `context.selected_objects`
as a stand-in for "what the import just added." All three were fixed the
same session this file was added: diff the scene before/after instead of
trusting selection state, and require real evidence (`'FINISHED'` in the
result, a real new object, a real file on disk) before reporting success.

**The rule going forward:** any function that calls `bpy.ops` and then
reports success/failure to a caller (a UI report, a return tuple, a TCP
response back to Mossy) must verify the actual real-world effect --
a new object in the scene, a file on disk with a fresh timestamp, a
counted change -- not just "the call didn't throw."

## Never silently overwrite an existing texture/asset with a smaller one

Same shape, different layer. A real incident: a correct 2K game texture
already sitting in a user's mod folder got silently overwritten by a 512
texture during a normal load → add collision → export pass. The
responsible code (`texture_helpers.install_texture` /
`_ensure_texture_in_data_folder`, and `bgsm_helpers.export_textures_for_object`)
only ever checked whether the destination path was literally the same
file as the source -- never whether something already there was *better*
than what was about to replace it. `shutil.copy2` doesn't ask; it just
overwrites.

Fixed via `nvtt_helpers.read_dds_dimensions` / `is_texture_size_downgrade`
(reads the real DDS header, no external tool needed) -- both copy paths
now refuse to replace an existing destination file with a lower-resolution
one, and say so instead of failing silently.

**The rule going forward:** before any code path overwrites a file that
might already hold real user content (a texture, a NIF, a BGSM someone
hand-edited), check what's already there. Existence + "is the replacement
actually better/different" beats a bare `shutil.copy2`.

## A generic simplify/decimate pass doesn't know what matters in your mesh

Third instance of the same underlying shape: a step that's technically
correct in isolation (decimate this mesh down to fit a hard limit) but
blind to *what* it's simplifying away. Real incident:
`mesh_helpers._enforce_vert_limit` (used by Custom Collision, the
exact-mesh collision path meant specifically to preserve real openings —
doorways, windows, any cutout a convex hull would wrongly seal shut) ran a
plain ratio-based Decimate with zero awareness that the thin frame
geometry around an opening is exactly the kind of detail edge-collapse
decimation strips first. Confirmed on a real cut-doorway mesh: an
unprotected pass took a doorway's boundary loop from 56 verts down to 18
under a moderate 0.15 ratio.

Fixed by building a vertex group from open-boundary vertices (`link_faces
<= 1`) each decimate iteration and setting `vertex_group_factor=1000`,
`invert_vertex_group=False`, weight `0.0` on the protected verts — verified
empirically (Blender doesn't obviously document which direction protects)
before shipping. Only the last of the bounded decimate attempts drops
protection, since the hard 255-vert/255-tri export limit still has to be
met even if that means an imperfectly-preserved opening.

**The rule going forward:** if a mesh operation exists specifically
*because* it needs to preserve some feature (an opening, a UV seam, a
weight-painted boundary), any later step in that same pipeline that
simplifies/decimates/reduces the mesh needs to know about that feature
too, explicitly — not just run generically and hope. Test against a real
mesh with the feature present, not a synthetic blob; a watertightness or
vert-count check won't catch this class of bug, since the output can be
perfectly well-formed and still be topologically wrong for the actual
gameplay purpose.

## An operation that can silently destroy good existing data needs a confirmation, not just Undo

Fourth instance of the same underlying shape as the three rules above,
found testing the Armor & Clothing panel against a real, already-correctly
-skinned armor import (Daz/G3 source): `fo4.auto_weight_armor` calls
Blender's own `bpy.ops.object.parent_set(type='ARMATURE_AUTO')`, which only
touches vertex groups whose *name* matches a real bone in the target
armature -- so on a mesh that's already properly skinned to that exact
skeleton (the normal case for anything imported from Daz/Marvelous
Designer/G3), every one of its real bone-weight groups gets silently
recomputed by Blender's heat-map algorithm, which is not necessarily as
good as what the mesh already had. Confirmed on a real asset: 46 of 3638
verts lost their weighting entirely from one click.

Undo (Ctrl+Z) is not a real safety net here -- it protects a user who is
watching it happen, not one who runs a batch step and walks away, by which
point the undo history may be gone. Fixed with a real confirmation prompt:
`invoke()` checks whether any selected mesh already has real weight data
before falling through to `execute()`, and if so shows
`window_manager.invoke_confirm(...)` naming which mesh(es) are at risk,
only proceeding if the user confirms. The exact parameter names for
`invoke_confirm` were verified via `bl_rna.functions['invoke_confirm'].parameters`
before use, not guessed (see the Mirror Weights lesson two entries up in
this file's own history) -- `title`, `message`, `confirm_text`, `icon`,
not whatever seemed plausible.

**The rule going forward:** before shipping an operator that can silently
overwrite or degrade existing user data when run on data that's already
good (not just "empty/default" data), ask whether the user could plausibly
run it expecting a no-op and get a quiet loss instead. If yes, gate it
behind a real confirmation naming what's about to be replaced -- Undo
existing is not sufficient justification to skip one. This is the same
shape as the texture-downgrade rule above and the NIF-import fabrication
fixes from this same session: an operation that *looks* successful while
quietly destroying something real.
