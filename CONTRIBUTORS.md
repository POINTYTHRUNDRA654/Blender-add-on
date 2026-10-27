# Contributors and Credits

This file lists contributors, supporters, and every third-party project the
Mossy Fallout 4 Blender Add-on builds on, ships, installs, or works alongside.
The same list is shown in Blender: press N, open the **Fallout 4** tab, and click
**Credits** in the Tutorial System box.

Licences below are the ones each project's own repository states. "See
repository" means the project uses its own or a mixed licence; read it there.
If anything here is wrong or missing, please open an issue or PR.

## Patrons

- Tundra654 — patron support via https://buymeacoffee.com/tundra654

## Core Contributors

- (Add your name here) — role/contribution

## Built on

- **PyNifly** (io_scene_nifly) by **BadDog (BadDogSkyrim)** —
  https://github.com/BadDogSkyrim/PyNifly — GNU GPL v3.
  This add-on is built on PyNifly. You import and export through this add-on's
  own buttons; underneath, PyNifly reads and writes the NIF files, and our
  texture, material, scale and export tools are built around it. PyNifly is
  not included in our download; please get it from BadDog's releases page
  (this release was tested with V27.0.0). Full credit and thanks to BadDog.
- **Blender** by the Blender Foundation — https://www.blender.org — GNU GPL v2+.

## Shipped inside this add-on

| Project | Author | Licence | Notes |
|---|---|---|---|
| [tqdm](https://github.com/tqdm/tqdm) | tqdm developers | MIT / MPL-2.0 | `lib/tqdm`, licence in `lib/tqdm/LICENCE` |
| [easydict](https://github.com/makinacorpus/easydict) | Makina Corpus | GNU LGPL v3 | `lib/easydict`, licence in `lib/easydict/LICENSE` |

## Installed on request or used alongside

Nothing below is bundled. The add-on either downloads it when you click an
install button, or works with it when you already have it.

### NIF, Havok and Bethesda tools

| Project | Author | Licence |
|---|---|---|
| [Blender Niftools Add-on](https://github.com/niftools/blender_niftools_addon) | NifTools team | See repository |
| [NifSkope](https://github.com/niftools/nifskope) | NifTools team | See repository |
| [ck-cmd](https://github.com/aerisarn/ck-cmd) | aerisarn | GNU GPL v3 |
| [havok2fbx](https://github.com/Highflex/havok2fbx) | Highflex | BSD-3-Clause |
| [FBXImporter](https://github.com/andrelo1/fbximporter) ([Nexus](https://www.nexusmods.com/fallout4/mods/59849)) | andrelo1 | No licence stated |
| [hkxcmd](https://github.com/figment/hkxcmd) | figment | See repository |
| [HKXPack](https://dexesttp.github.io/hkxpack/) | dexesttp | See project page |
| [xEdit / FO4Edit](https://github.com/TES5Edit/TES5Edit) | Elminster, Sharlikran, Zilav, Hlp and others | MPL-2.0 |
| [BodySlide and Outfit Studio](https://github.com/ousnius/BodySlide-and-Outfit-Studio) | ousnius, Caliente | GNU GPL v3 |
| [Collective Modding Toolkit](https://github.com/wxMichael/Collective-Modding-Toolkit) | wxMichael | GNU GPL v2 |

### Textures and images

| Project | Author | Licence |
|---|---|---|
| [DirectXTex / texconv](https://github.com/microsoft/DirectXTex) | Microsoft | MIT |
| [NVIDIA Texture Tools](https://github.com/castano/nvidia-texture-tools) | Ignacio Castaño, NVIDIA | See repository |
| [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) | Xintao Wang et al. | BSD-3-Clause |
| [StyleGAN2](https://github.com/NVlabs/stylegan2) | NVIDIA Research | See repository (non-commercial) |
| [TexTools for Blender](https://github.com/franMarz/TexTools-Blender) | franMarz, SavMartin | See repository |
| [FFmpeg](https://ffmpeg.org) | FFmpeg team | GNU LGPL v2.1+ / GPL v2+ |

### Mesh processing and 3D generation

| Project | Author | Licence |
|---|---|---|
| [MeshLab](https://github.com/cnr-isti-vclab/meshlab) / [PyMeshLab](https://github.com/cnr-isti-vclab/PyMeshLab) | Visual Computing Lab, ISTI-CNR | GNU GPL v3 |
| [trimesh](https://github.com/mikedh/trimesh) | Michael Dawson-Haggerty | MIT |
| [libigl](https://github.com/libigl/libigl) / [Python bindings](https://github.com/libigl/libigl-python-bindings) | libigl team | See repository |
| [BBW](https://github.com/azer89/BBW) | azer89 (also [shanmukhabharat](https://github.com/shanmukhabharat/BBW)) | No licence stated |
| [BbwPlugin](https://github.com/PhillipZeratul/BbwPlugin) | PhillipZeratul | No licence stated |
| [TripoSR](https://github.com/VAST-AI-Research/TripoSR) | VAST AI Research, Stability AI | MIT |
| [ComfyUI-Flowty-TripoSR](https://github.com/flowtyone/ComfyUI-Flowty-TripoSR) | flowtyone | GNU GPL v3 |
| [triposr-texture-gen](https://github.com/ejones/triposr-texture-gen) | ejones | MIT |
| [TripoSR-Bake](https://github.com/iffyloop/TripoSR-Bake) | iffyloop | Unlicense |
| [triposr_light](https://github.com/Dragoy/triposr_light) | Dragoy | No licence stated |
| [triposr-implementation](https://github.com/pythonicforge/triposr-implementation) | pythonicforge | No licence stated |
| [TRIPOSR mirror](https://github.com/StarxSky/TRIPOSR) | StarxSky | MIT |
| [Shap-E](https://github.com/openai/shap-e) / [Point-E](https://github.com/openai/point-e) | OpenAI | MIT |
| [Hunyuan3D-2](https://github.com/Tencent-Hunyuan/Hunyuan3D-2) | Tencent Hunyuan | See repository |
| [GET3D](https://github.com/nv-tlabs/GET3D) | NVIDIA Research | See repository |
| [Instant-NGP](https://github.com/NVlabs/instant-ngp) | NVIDIA Research | See repository |
| [DreamGaussian](https://github.com/dreamgaussian/dreamgaussian) | DreamGaussian authors | MIT |
| [ZoeDepth](https://github.com/isl-org/ZoeDepth) | Intel ISL | MIT |
| [PyTorch](https://pytorch.org) | PyTorch Foundation | BSD-3-Clause |

### Rigging and motion

| Project | Author | Licence |
|---|---|---|
| [RigNet](https://github.com/zhan-xu/RigNet) | Zhan Xu et al. | GNU GPL v3 |
| [brignet](https://github.com/pKrime/brignet) | pKrime | GNU GPL v3 |
| [Rignet_blender_addon](https://github.com/L-Medici/Rignet_blender_addon) | L-Medici | GNU GPL v3 |
| [rignet-gj](https://github.com/govindjoshi12/rignet-gj) | govindjoshi12 | No licence stated |
| [HY-Motion-1.0](https://github.com/Tencent-Hunyuan/HY-Motion-1.0) | Tencent Hunyuan | See repository |
| [MotionDiffuse](https://github.com/MotrixLab/MotionDiffuse) | MotrixLab (SMPL-X extension by [ellemcfarlane](https://github.com/ellemcfarlane/MotionDiffuse)) | See repository |
| [MediaPipe](https://github.com/google-ai-edge/mediapipe) | Google | Apache-2.0 |
| [BlendArMocap](https://github.com/cgtinker/BlendArMocap) | cgtinker | GNU GPL v3 |
| [ComfyUI-BlenderAI-node](https://github.com/AIGODLIKE/ComfyUI-BlenderAI-node) | AIGODLIKE | GNU GPL v3 |
| [ComfyUI-MotionDiff](https://github.com/Fannovel16/ComfyUI-MotionDiff) | Fannovel16 | MIT |
| [comfyui_controlnet_aux](https://github.com/Fannovel16/comfyui_controlnet_aux) | Fannovel16 | Apache-2.0 |

### Game engines and asset extraction

| Project | Author | Licence |
|---|---|---|
| [UModel / UE Viewer](https://github.com/gildor2/UEViewer) | Konstantin Nosov (Gildor) | MIT |
| [umodel_tools](https://github.com/skarndev/umodel_tools) | skarndev | GNU GPL v3 |
| [Blender-UE4-Importer](https://github.com/Waffle1434/Blender-UE4-Importer) | Waffle1434 | No licence stated |
| [AssetRipper](https://github.com/AssetRipper/AssetRipper) | AssetRipper team | GNU GPL v3 |
| [AssetStudio](https://github.com/Perfare/AssetStudio) | Perfare | MIT |
| [UnityFBX-To-Blender-Importer](https://github.com/Varneon/UnityFBX-To-Blender-Importer) | Varneon | MIT |
| [pypdf](https://github.com/py-pdf/pypdf) | py-pdf team | See repository (BSD-3-Clause) |

### Fallout 4 mods and guides referenced in the workflows

| Mod | Author |
|---|---|
| [Blender Power Armor Animation Rig](https://www.nexusmods.com/fallout4/mods/81279) | Shiagur |
| [Blender Animation Rig (1st & 3rd Person)](https://www.nexusmods.com/fallout4/mods/82537) | Shiagur |
| [Creating Armor & Clothing with Blender](https://www.nexusmods.com/fallout4/mods/17785) | Vugluscris |
| [FOMOD Creation Tool](https://www.nexusmods.com/fallout4/mods/6821) | Wenderer |
| [Cathedral Assets Optimizer](https://www.nexusmods.com/skyrimspecialedition/mods/23316) | G_k and contributors |
| [CBBE](https://www.nexusmods.com/fallout4/mods/15) | Caliente |
| [Story Action Poses](https://www.nexusmods.com/fallout4/mods/58448) | EngineGaming |
| [AAF – Advanced Animation Framework](https://www.nexusmods.com/fallout4/mods/31304) | dagobaking |
| [Poser Hotkeys](https://www.nexusmods.com/fallout4/mods/45967) | opparco |

And **Bethesda Game Studios** for Fallout 4 and the Creation Kit.

## How to contribute

- To add yourself: open a PR and add your name and role under Core Contributors.
- For third-party credits or corrections, open an issue or PR.

Thank you to everyone who contributes, reports bugs, tests releases, writes
docs, and supports this project.
