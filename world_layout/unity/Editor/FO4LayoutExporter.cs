// FO4LayoutExporter.cs -- Unity Editor window: export scene layout for Fallout 4.
// Copy the whole world_layout/unity/Editor folder into your project's Assets/
// (any folder named "Editor").  Open with  Tools > FO4 Layout Exporter.
// Also copy world_layout/unity/Runtime/FO4LayoutTag.cs into Assets/ (NOT
// inside an Editor folder) -- add that component to an object to skip it,
// point it at a specific NIF, or place an existing FO4 base object instead.
#if UNITY_EDITOR
using System.Collections.Generic;
using System.IO;
using UnityEditor;
using UnityEngine;

namespace Mossy.FO4Layout
{
    public class FO4LayoutExporter : EditorWindow
    {
        const string Pref = "Mossy.FO4Layout.";

        string worldspace, plugin, prefix, nifRoot, modFolder, outDir;
        Vector3 worldOffset;
        float meshYaw;
        bool selectedOnly = true;
        Vector2 scroll;
        string lastReport = "";

        [MenuItem("Tools/FO4 Layout Exporter")]
        static void Open() => GetWindow<FO4LayoutExporter>("FO4 Layout");

        void OnEnable()
        {
            worldspace = EditorPrefs.GetString(Pref + "worldspace", "OperationResoluteSentinel");
            plugin = EditorPrefs.GetString(Pref + "plugin", "ORS_Layout.esp");
            prefix = EditorPrefs.GetString(Pref + "prefix", "ORSL");
            nifRoot = EditorPrefs.GetString(Pref + "nifRoot", "ORS\\Unity");
            modFolder = EditorPrefs.GetString(Pref + "modFolder", "");
            outDir = EditorPrefs.GetString(Pref + "outDir",
                Path.GetFullPath(Path.Combine(Application.dataPath, "..", "FO4Layout")));
            worldOffset = new Vector3(EditorPrefs.GetFloat(Pref + "offX", 0),
                                      EditorPrefs.GetFloat(Pref + "offY", 0),
                                      EditorPrefs.GetFloat(Pref + "offZ", 0));
            meshYaw = EditorPrefs.GetFloat(Pref + "meshYaw", (float)FO4LayoutMath.UnityDefaultMeshYawDeg);
        }

        void Save()
        {
            EditorPrefs.SetString(Pref + "worldspace", worldspace);
            EditorPrefs.SetString(Pref + "plugin", plugin);
            EditorPrefs.SetString(Pref + "prefix", prefix);
            EditorPrefs.SetString(Pref + "nifRoot", nifRoot);
            EditorPrefs.SetString(Pref + "modFolder", modFolder);
            EditorPrefs.SetString(Pref + "outDir", outDir);
            EditorPrefs.SetFloat(Pref + "offX", worldOffset.x);
            EditorPrefs.SetFloat(Pref + "offY", worldOffset.y);
            EditorPrefs.SetFloat(Pref + "offZ", worldOffset.z);
            EditorPrefs.SetFloat(Pref + "meshYaw", meshYaw);
        }

        void OnGUI()
        {
            EditorGUI.BeginChangeCheck();
            worldspace = EditorGUILayout.TextField("Worldspace EditorID", worldspace);
            plugin = EditorGUILayout.TextField("Layout plugin", plugin);
            prefix = EditorGUILayout.TextField("EditorID prefix", prefix);
            nifRoot = EditorGUILayout.TextField(new GUIContent("NIF sub-path",
                "The 'Mesh Sub-path' you used in the Blender Unity converter"), nifRoot);
            using (new EditorGUILayout.HorizontalScope())
            {
                modFolder = EditorGUILayout.TextField(new GUIContent("MO2 mod folder",
                    "Folder holding Meshes\\ -- used to warn about NIFs not converted yet"), modFolder);
                if (GUILayout.Button("...", GUILayout.Width(28)))
                {
                    var p = EditorUtility.OpenFolderPanel("MO2 mod folder", modFolder, "");
                    if (!string.IsNullOrEmpty(p)) modFolder = p;
                }
            }
            using (new EditorGUILayout.HorizontalScope())
            {
                outDir = EditorGUILayout.TextField("Output folder", outDir);
                if (GUILayout.Button("...", GUILayout.Width(28)))
                {
                    var p = EditorUtility.OpenFolderPanel("Output folder", outDir, "");
                    if (!string.IsNullOrEmpty(p)) outDir = p;
                }
            }
            worldOffset = EditorGUILayout.Vector3Field(new GUIContent("World offset (FO4 units)",
                "Added to every position after conversion; FO4 axes: X east, Y north, Z up"), worldOffset);
            meshYaw = EditorGUILayout.FloatField(new GUIContent("Mesh yaw offset (deg)",
                "Extra yaw the Blender converter bakes into NIFs. Default 180 is UNVERIFIED -- " +
                "check with the calibration scene"), meshYaw);
            selectedOnly = EditorGUILayout.Toggle("Selected objects only", selectedOnly);
            if (EditorGUI.EndChangeCheck()) Save();

            EditorGUILayout.Space();
            if (GUILayout.Button("Export layout", GUILayout.Height(28))) Export();
            if (GUILayout.Button("Spawn calibration scene (uses selected object's mesh)")) SpawnCalibration();

            if (lastReport.Length > 0)
            {
                scroll = EditorGUILayout.BeginScrollView(scroll);
                EditorGUILayout.HelpBox(lastReport, MessageType.None);
                EditorGUILayout.EndScrollView();
            }
        }

        static double[] D(Vector3 v) => new double[] { v.x, v.y, v.z };

        static string ObjectId(GameObject go)
        {
            // GlobalObjectId survives renames, re-parenting and reordering, so a
            // re-import updates the same FO4 ref.  Unsaved scenes have no GUID yet,
            // so fall back to the hierarchy path (save the scene to get stable ids).
            var gid = GlobalObjectId.GetGlobalObjectIdSlow(go);
            if (gid.identifierType != 0 && !string.IsNullOrEmpty(go.scene.path))
                return gid.ToString();
            var t = go.transform;
            var path = t.name + "[" + t.GetSiblingIndex() + "]";
            while (t.parent != null) { t = t.parent; path = t.name + "[" + t.GetSiblingIndex() + "]/" + path; }
            return go.scene.path + ":" + path;
        }

        void Export()
        {
            IEnumerable<MeshFilter> filters;
            if (selectedOnly)
            {
                var list = new List<MeshFilter>();
                foreach (var go in Selection.gameObjects)
                    list.AddRange(go.GetComponentsInChildren<MeshFilter>(false));
                filters = list;
            }
            else filters = Object.FindObjectsByType<MeshFilter>(FindObjectsSortMode.None);

            var entries = new List<LayoutEntry>();
            var seen = new HashSet<MeshFilter>();
            int skipped = 0;
            var off = D(worldOffset);
            foreach (var mf in filters)
            {
                if (mf == null || mf.sharedMesh == null || !seen.Add(mf)) continue;
                var tag = mf.GetComponentInParent<FO4LayoutTag>();
                if (tag != null && tag.skip) { skipped++; continue; }
                var t = mf.transform;
                var mesh = mf.sharedMesh;
                var assetPath = AssetDatabase.GetAssetPath(mesh);
                var b = mesh.bounds;
                var e = FO4LayoutMath.MakeEntry(
                    ObjectId(mf.gameObject), mf.gameObject.name, mesh.name,
                    string.IsNullOrEmpty(assetPath) ? mesh.name : assetPath + ":" + mesh.name,
                    D(t.position), D(t.right), D(t.up), D(t.forward), D(t.lossyScale),
                    D(b.min), D(b.max),
                    tag != null ? tag.nifPath : null, tag != null ? tag.baseObject : null,
                    nifRoot, prefix, meshYaw, off);
                entries.Add(e);
            }
            if (entries.Count == 0)
            {
                lastReport = selectedOnly ? "Nothing with a MeshFilter is selected." : "No meshes in the open scenes.";
                return;
            }

            int missing = FO4LayoutMath.CheckNifs(entries, modFolder);
            var header = new Dictionary<string, string> {
                { "source", "unity" }, { "source_project", Path.GetFullPath(Path.Combine(Application.dataPath, "..")) },
                { "level", UnityEngine.SceneManagement.SceneManager.GetActiveScene().path },
                { "worldspace", worldspace }, { "plugin", plugin }, { "prefix", prefix } };
            FO4LayoutMath.WriteLayout(outDir, header, meshYaw, off, entries);

            var cells = new HashSet<string>();
            var sb = new System.Text.StringBuilder();
            foreach (var e in entries)
            {
                cells.Add(e.Cell[0] + "," + e.Cell[1]);
                foreach (var w in e.Warnings) { sb.AppendLine(e.Label + ": " + w); Debug.LogWarning("[FO4 Layout] " + e.Label + ": " + w); }
            }
            lastReport = $"Exported {entries.Count} placement(s) across {cells.Count} cell(s), {skipped} skipped.\n" +
                         $"-> {Path.Combine(outDir, "layout.tsv")}\n" +
                         (missing > 0 ? $"{missing} NIF(s) not in the mod folder yet -- convert them in Blender first.\n" : "") +
                         sb.ToString();
            Debug.Log("[FO4 Layout] " + lastReport);
        }

        void SpawnCalibration()
        {
            var src = Selection.activeGameObject;
            var mf = src != null ? src.GetComponentInChildren<MeshFilter>() : null;
            if (mf == null || mf.sharedMesh == null)
            {
                lastReport = "Select an object whose mesh is clearly asymmetric (a chair, a sign) and has been converted to NIF.";
                return;
            }
            var specs = new (string, Vector3, Vector3)[] {
                ("Cal_Identity", new Vector3(0, 0, 0), new Vector3(0, 0, 0)),
                ("Cal_Yaw90", new Vector3(4, 0, 0), new Vector3(0, 90, 0)),
                ("Cal_Pitch30", new Vector3(8, 0, 0), new Vector3(30, 0, 0)),
                ("Cal_Roll45", new Vector3(12, 0, 0), new Vector3(0, 0, 45)),
                ("Cal_Mixed", new Vector3(16, 0, 0), new Vector3(20, 135, -35)),
                ("Cal_North4m", new Vector3(0, 0, 4), new Vector3(0, 0, 0)) };
            var made = new List<Object>();
            foreach (var (name, pos, euler) in specs)
            {
                var go = new GameObject(name);
                go.AddComponent<MeshFilter>().sharedMesh = mf.sharedMesh;
                var mr = go.AddComponent<MeshRenderer>();
                var srcMr = mf.GetComponent<MeshRenderer>();
                if (srcMr != null) mr.sharedMaterials = srcMr.sharedMaterials;
                go.transform.SetPositionAndRotation(pos, Quaternion.Euler(euler));
                Undo.RegisterCreatedObjectUndo(go, "FO4 calibration");
                made.Add(go);
            }
            Selection.objects = made.ToArray();
            lastReport = $"Spawned {made.Count} calibration objects (selected). Click Export layout next.";
        }
    }
}
#endif
