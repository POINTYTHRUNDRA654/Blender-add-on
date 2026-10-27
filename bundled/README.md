# Bundled Dependencies

## PyNifly (io_scene_nifly) — download separately (tested with V27.0.0)

**Author:** BadDog (BadDogSkyrim)  
**Repository:** https://github.com/BadDogSkyrim/PyNifly  
**License:** GNU GPL v3  

This add-on is built on PyNifly: you import and export through this add-on's
own buttons, and PyNifly reads and writes the NIF files underneath.
Full credit goes to BadDog for this excellent tool.

PyNifly is NOT included in the release zips; users download it from BadDog's
releases page, which keeps them on his official builds.  If a PyNifly zip
(`io_scene_nifly*.zip`) is placed in this folder for a local build, the
add-on's "Auto-Install PyNifly" button installs it from here first instead of
downloading.

See also: `CONTRIBUTORS.md` in the addon root for full attribution.

---

## PyMeshLab — on-demand install

**Authors:** Alessandro Muntoni and the VCLab team at ISTI-CNR  
**Repository:** https://github.com/cnr-isti-vclab/PyMeshLab  
**License:** GPL-3.0  
**PyPI:** https://pypi.org/project/pymeshlab/

Built on MeshLab:
> P. Cignoni, M. Callieri, M. Corsini, M. Dellepiane, F. Ganovelli, G. Ranzuglia  
> "MeshLab: an Open-Source Mesh Processing Tool"  
> Sixth Eurographics Italian Chapter Conference, pp. 129–136, 2008

PyMeshLab is **not bundled** (compiled C-extension wheels are ~80 MB and
platform-specific).  It is installed on demand via `pip` into
`<addon>/lib/ml/` the first time the user clicks **Install PyMeshLab** in the
FO4 Tools → Mesh Tools panel.  No modification is made to the PyMeshLab source.
