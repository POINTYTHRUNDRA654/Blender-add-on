using System; using System.IO; using System.Linq; using System.Text.Json; using System.Collections.Generic;
using Mossy.FO4Layout;
class P {
  static double[] A(JsonElement e) => e.EnumerateArray().Select(x => x.GetDouble()).ToArray();
  static string S(JsonElement e) => e.ValueKind == JsonValueKind.Null ? null : e.GetString();
  static int Main() {
    var doc = JsonDocument.Parse(File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", "vectors.json"))).RootElement;
    int fails = 0; var entries = new List<LayoutEntry>();
    foreach (var c in doc.GetProperty("cases").EnumerateArray()) {
      var i = c.GetProperty("input"); var ex = c.GetProperty("expect"); var ax = i.GetProperty("axes").EnumerateArray().Select(A).ToArray();
      var lb = i.GetProperty("local_bounds").EnumerateArray().Select(A).ToArray();
      var e = FO4LayoutMath.MakeEntry(S(i.GetProperty("actor_id")), S(i.GetProperty("actor_label")), S(i.GetProperty("asset_name")),
        S(i.GetProperty("asset_path")), A(i.GetProperty("position")), ax[0], ax[1], ax[2], A(i.GetProperty("scale3d")), lb[0], lb[1],
        S(i.GetProperty("nif")), S(i.GetProperty("base")), S(i.GetProperty("nif_root")), S(i.GetProperty("prefix")),
        c.GetProperty("yaw").GetDouble(), A(i.GetProperty("world_offset")));
      entries.Add(e);
      bool ok = true;
      void Near(string n, double[] a, double[] b, double eps){ for(int k=0;k<a.Length;k++){ double d=Math.Abs(a[k]-b[k]); if(n=="rot") d=Math.Min(d,360-d); if(d>eps){ok=false;Console.WriteLine($"{e.Label} {n}[{k}] {a[k]} != {b[k]}");}}}
      Near("pos", e.Pos, A(ex.GetProperty("pos")), 1e-6); Near("rot", e.Rot, A(ex.GetProperty("rot")), 1e-6);
      Near("bounds", e.Bounds, A(ex.GetProperty("bounds")), 1e-6);
      if (Math.Abs(e.Scale - ex.GetProperty("scale").GetDouble())>1e-9) { ok=false; Console.WriteLine("scale"); }
      foreach (var (n,v) in new[]{("ref_edid",e.RefEdid),("stat_edid",e.StatEdid),("nif",e.Nif),("base",e.Base)})
        if (v != ex.GetProperty(n).GetString()) { ok=false; Console.WriteLine($"{n}: '{v}' != '{ex.GetProperty(n).GetString()}'"); }
      if (e.Cell[0]!=ex.GetProperty("cell")[0].GetInt32()||e.Cell[1]!=ex.GetProperty("cell")[1].GetInt32()) {ok=false;Console.WriteLine("cell");}
      if (e.Warnings.Count != ex.GetProperty("warnings").GetArrayLength()) { ok=false; Console.WriteLine($"warnings {e.Warnings.Count}: {string.Join("|",e.Warnings)}"); }
      if (!ok) fails++;
    }
    var dir = Path.Combine(Path.GetTempPath(), "cslayout");
    FO4LayoutMath.WriteLayout(dir, new Dictionary<string,string>{{"source","unity"},{"worldspace","OperationResoluteSentinel"},{"prefix","ORSL"},{"plugin","ORS_Layout.esp"}}, 180, new double[3], entries);
    var tsv = File.ReadAllText(Path.Combine(dir,"layout.tsv")); var pyTsv = doc.GetProperty("tsv").GetString();
    var a = tsv.Split('\n'); var b = pyTsv.Split('\n'); int tsvDiff = 0;
    for (int k=0;k<Math.Max(a.Length,b.Length);k++){ var x=k<a.Length?a[k]:"<none>"; var y=k<b.Length?b[k]:"<none>"; if(x!=y){ if(tsvDiff++<5){Console.WriteLine("C#: "+x);Console.WriteLine("Py: "+y);} } }
    JsonDocument.Parse(File.ReadAllText(Path.Combine(dir,"layout.json")));  // must be valid JSON
    Console.WriteLine($"cases failed: {fails}/{entries.Count}, tsv lines differing: {tsvDiff}");
    return fails + tsvDiff == 0 ? 0 : 1;
  }
}
