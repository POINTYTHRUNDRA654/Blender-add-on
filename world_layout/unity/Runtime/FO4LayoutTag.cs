// FO4LayoutTag.cs -- optional per-object overrides for the FO4 layout exporter.
// Lives OUTSIDE an Editor folder so it can be added to scene objects.
// It affects nothing at runtime.
using UnityEngine;

namespace Mossy.FO4Layout
{
    [DisallowMultipleComponent]
    public class FO4LayoutTag : MonoBehaviour
    {
        [Tooltip("Leave this object (and its children) out of the export")]
        public bool skip;

        [Tooltip("NIF to use, relative to Meshes\\ (e.g. ORS\\Props\\Crate01.nif). Blank = default naming")]
        public string nifPath;

        [Tooltip("Place an existing base object instead: 'Fallout4.esm|0001A2B3' or an EditorID")]
        public string baseObject;
    }
}
