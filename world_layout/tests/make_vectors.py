"""Generate tests/vectors.json (+ expected TSV) from layout_core for the C# mirror test."""
import json
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import layout_core as lc  # noqa: E402


def main():
    rng = random.Random(42)
    cases, entries = [], []
    for i in range(60):
        m = lc.mat_mul(lc.rot_z(rng.uniform(-3, 3)),
                       lc.mat_mul(lc.rot_y(rng.uniform(-1.5, 1.5)), lc.rot_x(rng.uniform(-3, 3))))
        if i == 0:
            m = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
        axes = [[m[0][j], m[1][j], m[2][j]] for j in range(3)]
        s = rng.uniform(0.3, 2.5)
        scale = [s, s, s] if i % 7 else [s, s * 1.5, -s]
        pos = [rng.uniform(-400, 400) for _ in range(3)]
        yaw = 180.0 if i % 5 else rng.choice([0.0, 90.0, 270.0])
        off = [rng.uniform(-5000, 5000), rng.uniform(-5000, 5000), 0.0]
        bmin = [rng.uniform(-2, 0) for _ in range(3)]
        bmax = [rng.uniform(0, 2) for _ in range(3)]
        base = "Fallout4.esm|0001A2B3" if i % 11 == 3 else None
        nif = "Data/Meshes/ORS/Custom/Thing.nif" if i % 13 == 4 else None
        inp = dict(actor_id=f"GlobalObjectID_V1-2-abc{i}-12345-0", actor_label=f"Prop {i}/x",
                   asset_name=f"Mesh {i % 4}", asset_path=f"Assets/Props/m{i % 4}.fbx:Mesh {i % 4}",
                   position=pos, axes=axes, scale3d=scale, local_bounds=[bmin, bmax],
                   nif=nif, base=base, nif_root="ORS/Unity", prefix="ORSL", world_offset=off)
        p = lc.profile_for("unity", yaw)
        e = lc.make_entry(p, **inp)
        entries.append(e)
        cases.append({"input": inp, "yaw": yaw, "expect": e})
    d = tempfile.mkdtemp()
    _, tsv = lc.write_layout(d, {"source": "unity", "worldspace": "OperationResoluteSentinel",
                                 "prefix": "ORSL", "plugin": "ORS_Layout.esp"}, entries)
    out = {"cases": cases, "tsv": open(tsv, encoding="utf-8").read()}
    with open(os.path.join(HERE, "vectors.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print(f"wrote {len(cases)} cases")


if __name__ == "__main__":
    main()
