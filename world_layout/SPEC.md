# FO4 world-layout pipeline

Lay out towns in **Unreal** or **Unity**, place them in a **Fallout 4**
worldspace via xEdit.

```
Unreal / Unity scene ──exporter──► layout.json + layout.tsv ──xEdit script──► ORS_Layout.esp
        ▲                                                                     (STATs + REFRs)
  assets converted to NIF by the Blender add-on (fo4_ue5_converter / fo4_unity_converter)
```

| Piece | File | Runs in |
|---|---|---|
| Shared math + writers | `layout_core.py` | any Python (Unreal Editor uses it) |
| Unreal exporter | `unreal/fo4_layout_export.py` | Unreal Editor Python (UE 5.x) |
| Unity exporter | `unity/Editor/*.cs`, `unity/Runtime/FO4LayoutTag.cs` | Unity Editor |
| Importer | `xedit/FO4 - Import Layout.pas` | FO4Edit / xEdit |
| Tests | `tests/` | `python tests/test_layout_core.py`, `python tests/test_unreal_exporter_stub.py`, `cd tests/csharp && dotnet run` |

`FO4LayoutMath.cs` mirrors `layout_core.py` exactly; `tests/vectors.json`
(regenerate with `tests/make_vectors.py`) proves they agree, down to a
byte-identical TSV.

## Coordinate conversion

FO4 world: X = east, Y = north, Z = up, right-handed, 1 unit = 0.0142875 m
(the same Havok scale the converters and `fo4_ck_cell.py` use).

| Engine | Units | FO4 position |
|---|---|---|
| Unreal (X fwd, Y right, Z up, left-handed) | cm → ×0.69991 | (UE.Y, UE.X, UE.Z) — Unreal top view keeps its look: +X up the screen = north |
| Unity (X right, Y up, Z fwd, left-handed) | m → ×69.991 | (U.X, U.Z, U.Y) |

`world_offset` (FO4 units) is added afterwards to move the whole layout.

**Rotation.** Exporters read each object's local X/Y/Z axes in world space
(never Euler angles, so engine rotation order can't matter), and compute

    R_fo4 = W · R_engine · B⁻¹

where `W` is the axis map above and `B` is the **mesh basis**: how the
Blender converter maps engine-local mesh coordinates to NIF-local ones
(`B = Rz(mesh_yaw_offset) · W`). The result is written as FO4 Euler degrees
using `M = Rz(-z) · Ry(-y) · Rx(-x)` (Z = compass heading, clockwise).

**Scale.** FO4 refs only have a uniform scale (0.01 – 10). Non-uniform
scales are averaged with a warning; mirrored (negative) scale is warned
about — bake those into the mesh.

### What is verified, and what isn't yet

* ✅ Unit tests: a mesh vertex placed by the engine transform lands on the
  same world point as the NIF vertex placed by the generated FO4 ref (300
  random transforms per engine), Euler round-trips, gimbal lock, cells.
* ⚠️ **Mesh yaw offset (default 180° for both engines)** — derived from the
  converters' FBX import axes (`-X`/`Z` for Unreal, `-Z`/`Y` for Unity) and
  each engine's FBX export handedness flip. Not yet checked in the CK.
* ⚠️ **FO4 X/Y rotation order** — heading (Z) direction is certain; the
  pitch/roll composition order is the standard Gamebryo one but not yet
  checked in the CK.

Both are settled by the **calibration scene**: six copies of one clearly
asymmetric converted mesh (identity, yaw 90, pitch 30, roll 45, a mixed
rotation, and one moved 4 m north). Export → import → open the cell in the
CK and compare with the engine viewport:

* everything turned by the same yaw (e.g. all facing backwards) → change
  `mesh_yaw_offset_deg` (Unreal config / Unity window) by that amount;
* only pitch/roll/mixed wrong → the Euler order needs changing in
  `fo4_euler_deg_from_matrix` (+ the C# mirror) — report it, don't hand-tune.

## layout.tsv (what xEdit reads)

UTF-8, tab-separated. Header lines start with `#`:

```
#format     fo4-layout   1
#source     unreal | unity
#worldspace OperationResoluteSentinel
#prefix     ORSL
#plugin     ORS_Layout.esp
```

then a column row and one row per placement:

| column | meaning |
|---|---|
| `id` | engine's stable object id (Unreal actor path [+ component, `#instance`]; Unity GlobalObjectId) |
| `ref_edid` | REFR EditorID: `prefix_label_<MD5(id)[:8]>` — how re-imports find their refs |
| `base` | existing base object instead of a new STAT: `Plugin.esm|00ABCDEF` or an EditorID; blank for new meshes |
| `nif` | path relative to `Meshes\` |
| `stat_edid` | EditorID of the STAT to create/reuse for `nif` |
| `px py pz` | position, FO4 units |
| `rx ry rz` | rotation, degrees (xEdit edit values) |
| `scale` | uniform scale |
| `cell_x cell_y` | exterior cell grid (`floor(pos / 4096)`) |
| `bmin* bmax*` | mesh bounds in NIF space → the STAT's OBND |

`layout.json` carries the same data plus labels, source asset paths and
per-entry warnings, for tools (Mossy) to read.

## Mapping engine assets to NIFs

Default: `<nif_root>\<mesh name with spaces → _>.nif`, which is what the
Blender converters produce when their **Mesh Sub-path** is set to the same
`nif_root`. Overrides, highest priority first:

* Unreal: actor/component tags `fo4:nif=...`, `fo4:base=...`, `fo4:skip`;
  or `nif_map` / `base_map` in `FO4Layout/fo4_layout_config.json`.
* Unity: `FO4LayoutTag` component (skip / nifPath / baseObject).

Exporters warn about any NIF not present in the configured MO2 mod folder.

## Importer behaviour

* Needs the worldspace's plugin loaded; target plugin is created if missing
  (masters: Fallout4.esm + the worldspace's plugin).
* One STAT per `stat_edid`, reused on re-import.
* REFRs go into the matching exterior cell (cell override copied into the
  target plugin). Cells must already exist in the worldspace.
* **Re-import** updates refs with matching `ref_edid` in place; refs that
  changed cell are re-created; refs no longer in the layout are reported
  as orphans and left alone. Set `DRY_RUN = True` to preview.

## Still on the list

* Terrain: FO4 landscape → Unreal/Unity heightmap (so towns are built on
  the real ORS ground), and later the reverse.
* Lights, markers, and non-STAT bases created from engine actors.
* After import, as always: navmesh, precombines/previs, LOD in the CK.
