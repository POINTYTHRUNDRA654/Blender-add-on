// FO4LayoutMath.cs -- C# mirror of world_layout/layout_core.py.
// Pure .NET (no UnityEngine) so it can be unit-tested outside Unity against
// tests/vectors.json.  Keep in lock-step with layout_core.py.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;

namespace Mossy.FO4Layout
{
    public class LayoutEntry
    {
        public string Id, Label, Asset, RefEdid, Base = "", Nif = "", StatEdid = "";
        public double[] Pos = new double[3], Rot = new double[3];
        public double Scale = 1;
        public int[] Cell = new int[2];
        public double[] Bounds;   // null when unknown
        public List<string> Warnings = new List<string>();
    }

    public static class FO4LayoutMath
    {
        public const int FormatVersion = 1;
        public const double FO4MetresPerUnit = 0.0142875;
        public const double CellSize = 4096.0;
        public const double RefScaleMin = 0.01, RefScaleMax = 10.0;

        // Unity: +X right (east), +Z forward (north), +Y up.  fo4 = W * unity
        public static readonly double[,] UnityWorldAxes = { { 1, 0, 0 }, { 0, 0, 1 }, { 0, 1, 0 } };
        public const double UnityMetresPerUnit = 1.0;
        // UNVERIFIED until the calibration scene is checked -- see SPEC.md
        public const double UnityDefaultMeshYawDeg = 180.0;

        static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        // ---------------------------------------------------------- matrices
        public static double[,] Mul(double[,] a, double[,] b)
        {
            var r = new double[3, 3];
            for (int i = 0; i < 3; i++)
                for (int j = 0; j < 3; j++)
                    for (int k = 0; k < 3; k++) r[i, j] += a[i, k] * b[k, j];
            return r;
        }

        public static double[] Mul(double[,] m, double[] v)
        {
            var r = new double[3];
            for (int i = 0; i < 3; i++)
                for (int k = 0; k < 3; k++) r[i] += m[i, k] * v[k];
            return r;
        }

        public static double[,] T(double[,] m)
        {
            var r = new double[3, 3];
            for (int i = 0; i < 3; i++) for (int j = 0; j < 3; j++) r[i, j] = m[j, i];
            return r;
        }

        public static double[,] RotX(double a) { double c = Math.Cos(a), s = Math.Sin(a); return new double[,] { { 1, 0, 0 }, { 0, c, -s }, { 0, s, c } }; }
        public static double[,] RotY(double a) { double c = Math.Cos(a), s = Math.Sin(a); return new double[,] { { c, 0, s }, { 0, 1, 0 }, { -s, 0, c } }; }
        public static double[,] RotZ(double a) { double c = Math.Cos(a), s = Math.Sin(a); return new double[,] { { c, -s, 0 }, { s, c, 0 }, { 0, 0, 1 } }; }

        static double Rad(double d) => d * Math.PI / 180.0;
        static double Deg(double r) => r * 180.0 / Math.PI;

        public static double[,] FromAxes(double[] ax, double[] ay, double[] az) =>
            new double[,] { { ax[0], ay[0], az[0] }, { ax[1], ay[1], az[1] }, { ax[2], ay[2], az[2] } };

        static double[] Norm(double[] v)
        {
            double l = Math.Sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
            if (l == 0) l = 1;
            return new[] { v[0] / l, v[1] / l, v[2] / l };
        }

        static double Dot(double[] a, double[] b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

        public static double[,] Orthonormalize(double[] ax, double[] ay, double[] az)
        {
            var x = Norm(ax);
            double d = Dot(ay, x);
            var y = Norm(new[] { ay[0] - x[0] * d, ay[1] - x[1] * d, ay[2] - x[2] * d });
            var z = new[] { x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0] };
            if (Dot(z, az) < 0) z = new[] { -z[0], -z[1], -z[2] };
            return FromAxes(x, y, z);
        }

        // --------------------------------------------- FO4 Euler convention
        // M = Rz(-z) Ry(-y) Rx(-x), angles in degrees, Z = clockwise heading
        public static double[,] FO4MatrixFromEulerDeg(double x, double y, double z) =>
            Mul(RotZ(-Rad(z)), Mul(RotY(-Rad(y)), RotX(-Rad(x))));

        static double Wrap(double d)
        {
            d %= 360.0;
            if (d < 0) d += 360.0;
            if (d >= 359.99995) d = 0.0;
            return d;
        }

        public static double[] FO4EulerDegFromMatrix(double[,] m)
        {
            double sb = Math.Max(-1.0, Math.Min(1.0, -m[2, 0]));
            double b = Math.Asin(sb), a, c;
            if (Math.Abs(sb) < 0.9999995) { a = Math.Atan2(m[2, 1], m[2, 2]); c = Math.Atan2(m[1, 0], m[0, 0]); }
            else { a = 0; c = Math.Atan2(-m[0, 1], m[1, 1]); }
            return new[] { Wrap(-Deg(a)), Wrap(-Deg(b)), Wrap(-Deg(c)) };
        }

        // ------------------------------------------------------- conversion
        public static double UnitScale => UnityMetresPerUnit / FO4MetresPerUnit;

        public static double[,] MeshBasis(double yawDeg) => Mul(RotZ(Rad(yawDeg)), UnityWorldAxes);

        /// axes = transform.right, .up, .forward (local X, Y, Z in world space)
        public static void ConvertTransform(LayoutEntry e, double[] position, double[] ax, double[] ay, double[] az,
                                            double[] scale3d, double yawDeg, double[] worldOffset)
        {
            var W = UnityWorldAxes;
            var B = MeshBasis(yawDeg);
            var rSrc = Orthonormalize(ax, ay, az);
            var rFo4 = Mul(Mul(W, rSrc), T(B));

            if (scale3d[0] * scale3d[1] * scale3d[2] < 0)
                e.Warnings.Add("mirrored (negative scale) -- FO4 refs cannot be mirrored; bake the mirror into the mesh instead");
            double sx = Math.Abs(scale3d[0]), sy = Math.Abs(scale3d[1]), sz = Math.Abs(scale3d[2]);
            double s = (sx + sy + sz) / 3.0;
            if (Math.Max(sx, Math.Max(sy, sz)) - Math.Min(sx, Math.Min(sy, sz)) > 1e-3 * Math.Max(s, 1e-6))
                e.Warnings.Add(string.Format(Inv, "non-uniform scale ({0:0.####}, {1:0.####}, {2:0.####}) -- FO4 refs are uniform only, using average {3:0.0000}",
                    scale3d[0], scale3d[1], scale3d[2], s));
            if (s < RefScaleMin || s > RefScaleMax)
                e.Warnings.Add(string.Format(Inv, "scale {0:0.0000} outside FO4 range {1}-{2}", s, RefScaleMin, RefScaleMax));

            var p = Mul(W, position);
            for (int i = 0; i < 3; i++) e.Pos[i] = p[i] * UnitScale + worldOffset[i];
            e.Rot = FO4EulerDegFromMatrix(rFo4);
            e.Scale = s;
            e.Cell = CellOf(e.Pos[0], e.Pos[1]);
        }

        public static double[] ConvertLocalBounds(double[] bmin, double[] bmax, double yawDeg)
        {
            var B = MeshBasis(yawDeg);
            double k = UnitScale;
            var lo = new[] { double.MaxValue, double.MaxValue, double.MaxValue };
            var hi = new[] { double.MinValue, double.MinValue, double.MinValue };
            foreach (var cx in new[] { bmin[0], bmax[0] })
                foreach (var cy in new[] { bmin[1], bmax[1] })
                    foreach (var cz in new[] { bmin[2], bmax[2] })
                    {
                        var v = Mul(B, new[] { cx, cy, cz });
                        for (int i = 0; i < 3; i++) { lo[i] = Math.Min(lo[i], v[i] * k); hi[i] = Math.Max(hi[i], v[i] * k); }
                    }
            return new[] { lo[0], lo[1], lo[2], hi[0], hi[1], hi[2] };
        }

        public static int[] CellOf(double x, double y) =>
            new[] { (int)Math.Floor(x / CellSize), (int)Math.Floor(y / CellSize) };

        // ------------------------------------------------------ names, paths
        static readonly Regex EdidBad = new Regex("[^A-Za-z0-9_]");
        static readonly Regex Underscores = new Regex("_+");

        public static string SanitizeEdid(string text, int maxLen = 80)
        {
            var s = Underscores.Replace(EdidBad.Replace(text ?? "", "_").Trim('_'), "_");
            if (s.Length > maxLen) s = s.Substring(0, maxLen);
            return s.Length == 0 ? "Unnamed" : s;
        }

        public static string RefEdid(string prefix, string label, string actorId)
        {
            using (var md5 = MD5.Create())
            {
                var h = md5.ComputeHash(Encoding.UTF8.GetBytes(actorId));
                var hex = BitConverter.ToString(h).Replace("-", "").Substring(0, 8).ToUpperInvariant();
                return SanitizeEdid(prefix, 16) + "_" + SanitizeEdid(label, 40) + "_" + hex;
            }
        }

        public static string NormalizeNifPath(string path)
        {
            if (string.IsNullOrEmpty(path)) return "";
            var p = path.Replace('/', '\\').Trim().TrimStart('\\');
            var low = p.ToLowerInvariant();
            foreach (var prefix in new[] { "data\\meshes\\", "meshes\\" })
                if (low.StartsWith(prefix)) { p = p.Substring(prefix.Length); break; }
            if (!p.ToLowerInvariant().EndsWith(".nif")) p += ".nif";
            return p;
        }

        public static string DefaultNifForAsset(string assetName, string nifRoot)
        {
            var name = assetName.Replace(' ', '_');
            var root = (nifRoot ?? "").Replace('/', '\\').Trim('\\');
            return NormalizeNifPath(root.Length > 0 ? root + "\\" + name + ".nif" : name + ".nif");
        }

        public static LayoutEntry MakeEntry(string actorId, string label, string assetName, string assetPath,
            double[] position, double[] ax, double[] ay, double[] az, double[] scale3d,
            double[] boundsMin, double[] boundsMax, string nif, string baseSpec,
            string nifRoot, string prefix, double yawDeg, double[] worldOffset)
        {
            var e = new LayoutEntry { Id = actorId, Label = label, Asset = assetPath };
            ConvertTransform(e, position, ax, ay, az, scale3d, yawDeg, worldOffset);
            e.RefEdid = RefEdid(prefix, label, actorId);
            e.Base = baseSpec ?? "";
            e.Nif = !string.IsNullOrEmpty(nif) ? NormalizeNifPath(nif)
                  : (e.Base.Length > 0 ? "" : DefaultNifForAsset(assetName, nifRoot));
            if (e.Base.Length == 0)
            {
                var stem = e.Nif.ToLowerInvariant().EndsWith(".nif") ? e.Nif.Substring(0, e.Nif.Length - 4) : e.Nif;
                e.StatEdid = SanitizeEdid(prefix + "_" + stem);
            }
            e.Bounds = boundsMin != null ? ConvertLocalBounds(boundsMin, boundsMax, yawDeg) : null;
            return e;
        }

        public static int CheckNifs(List<LayoutEntry> entries, string modFolder)
        {
            if (string.IsNullOrEmpty(modFolder)) return 0;
            int missing = 0;
            foreach (var e in entries)
            {
                if (e.Nif.Length == 0) continue;
                var p = Path.Combine(modFolder, "Meshes", e.Nif.Replace('\\', Path.DirectorySeparatorChar));
                if (!File.Exists(p)) { e.Warnings.Add("NIF not found in mod folder: Meshes\\" + e.Nif); missing++; }
            }
            return missing;
        }

        // ---------------------------------------------------------- writers
        public static readonly string[] TsvColumns = {
            "id", "ref_edid", "base", "nif", "stat_edid", "px", "py", "pz", "rx", "ry", "rz", "scale",
            "cell_x", "cell_y", "bminx", "bminy", "bminz", "bmaxx", "bmaxy", "bmaxz" };

        static string F4(double v) => v.ToString("0.0000", Inv);
        static string J(double v) => v.ToString("R", Inv);

        static string Esc(string s)
        {
            var sb = new StringBuilder("\"");
            foreach (var ch in s ?? "")
            {
                switch (ch)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (ch < 0x20) sb.Append("\\u").Append(((int)ch).ToString("x4"));
                        else sb.Append(ch);
                        break;
                }
            }
            return sb.Append('"').ToString();
        }

        static string Arr(double[] a)
        {
            if (a == null) return "null";
            var parts = new string[a.Length];
            for (int i = 0; i < a.Length; i++) parts[i] = J(a[i]);
            return "[" + string.Join(", ", parts) + "]";
        }

        /// header keys: source, worldspace, prefix, plugin (+ anything else, written as strings)
        public static void WriteLayout(string outDir, IDictionary<string, string> header, double yawDeg,
                                       double[] worldOffset, List<LayoutEntry> entries, string basename = "layout")
        {
            Directory.CreateDirectory(outDir);
            var js = new StringBuilder("{\n");
            foreach (var kv in header) js.Append(' ').Append(Esc(kv.Key)).Append(": ").Append(Esc(kv.Value)).Append(",\n");
            js.Append(" \"mesh_yaw_offset_deg\": ").Append(J(yawDeg)).Append(",\n");
            js.Append(" \"world_offset\": ").Append(Arr(worldOffset)).Append(",\n");
            js.Append(" \"format\": \"fo4-layout\",\n \"format_version\": ").Append(FormatVersion).Append(",\n \"entries\": [");
            for (int i = 0; i < entries.Count; i++)
            {
                var e = entries[i];
                var w = new List<string>();
                foreach (var x in e.Warnings) w.Add(Esc(x));
                js.Append(i == 0 ? "\n" : ",\n");
                js.Append("  {\"id\": ").Append(Esc(e.Id)).Append(", \"label\": ").Append(Esc(e.Label))
                  .Append(", \"asset\": ").Append(Esc(e.Asset)).Append(", \"ref_edid\": ").Append(Esc(e.RefEdid))
                  .Append(", \"base\": ").Append(Esc(e.Base)).Append(", \"nif\": ").Append(Esc(e.Nif))
                  .Append(", \"stat_edid\": ").Append(Esc(e.StatEdid)).Append(", \"pos\": ").Append(Arr(e.Pos))
                  .Append(", \"rot\": ").Append(Arr(e.Rot)).Append(", \"scale\": ").Append(J(e.Scale))
                  .Append(", \"cell\": [").Append(e.Cell[0]).Append(", ").Append(e.Cell[1]).Append("]")
                  .Append(", \"bounds\": ").Append(Arr(e.Bounds))
                  .Append(", \"warnings\": [").Append(string.Join(", ", w)).Append("]}");
            }
            js.Append("\n ]\n}\n");
            File.WriteAllText(Path.Combine(outDir, basename + ".json"), js.ToString(), new UTF8Encoding(false));

            var t = new StringBuilder();
            t.Append("#format\tfo4-layout\t").Append(FormatVersion).Append('\n');
            foreach (var key in new[] { "source", "worldspace", "prefix", "plugin" })
                t.Append('#').Append(key).Append('\t').Append(header.TryGetValue(key, out var v) ? v : "").Append('\n');
            t.Append(string.Join("\t", TsvColumns)).Append('\n');
            foreach (var e in entries)
            {
                var b = e.Bounds ?? new double[6];
                var row = new List<string> { e.Id, e.RefEdid, e.Base, e.Nif, e.StatEdid,
                    F4(e.Pos[0]), F4(e.Pos[1]), F4(e.Pos[2]), F4(e.Rot[0]), F4(e.Rot[1]), F4(e.Rot[2]), F4(e.Scale),
                    e.Cell[0].ToString(Inv), e.Cell[1].ToString(Inv) };
                foreach (var x in b) row.Add(((long)Math.Round(x, MidpointRounding.ToEven)).ToString(Inv));
                for (int i = 0; i < row.Count; i++) row[i] = (row[i] ?? "").Replace('\t', ' ');
                t.Append(string.Join("\t", row)).Append('\n');
            }
            File.WriteAllText(Path.Combine(outDir, basename + ".tsv"), t.ToString(), new UTF8Encoding(false));
        }
    }
}
