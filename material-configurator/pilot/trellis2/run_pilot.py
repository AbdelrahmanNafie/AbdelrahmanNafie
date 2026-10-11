"""
TRELLIS.2 pilot: turn product photos into 3D models (GLB) for the 3D Studio.

Run inside a TRELLIS.2 checkout on a Linux machine with an NVIDIA GPU (>= 24 GB):

    python run_pilot.py --input /path/to/pilot/trellis2/input --output /path/to/out

For every photo it writes, per seed:
    <name>-s<seed>.glb   textured 3D model (open it in the configurator via "Add product")
    <name>-s<seed>.mp4   turntable preview, to judge the model without any 3D software
and a summary.json with timings. Generation is random: two seeds per photo give you a choice.

Licence note: the GLB export below uses nvdiffrast / nvdiffrec (NVIDIA, non-commercial,
research/evaluation only). That is fine for this evaluation; production use needs a review
or a different export path (we only need the shape, the finishes come from the Mobica library).
"""
import argparse
import json
import os
import time

os.environ["OPENCV_IO_ENABLE_OPENEXR"] = "1"
os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

import cv2
import imageio
import torch
from PIL import Image

import o_voxel
from trellis2.pipelines import Trellis2ImageTo3DPipeline
from trellis2.renderers import EnvMap
from trellis2.utils import render_utils


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="folder with product photos (white background)")
    ap.add_argument("--output", required=True)
    ap.add_argument("--seeds", default="42,7", help="comma-separated seeds; one model per seed")
    ap.add_argument("--resolution", default="1536_cascade", choices=["512", "1024", "1024_cascade", "1536_cascade"])
    ap.add_argument("--texture-size", type=int, default=4096)
    ap.add_argument("--no-video", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.output, exist_ok=True)

    pipeline = Trellis2ImageTo3DPipeline.from_pretrained("microsoft/TRELLIS.2-4B")
    pipeline.cuda()
    envmap = None
    if not a.no_video:
        hdr = cv2.imread("assets/hdri/forest.exr", cv2.IMREAD_UNCHANGED)  # ships with the TRELLIS.2 repo
        envmap = EnvMap(torch.tensor(cv2.cvtColor(hdr, cv2.COLOR_BGR2RGB), dtype=torch.float32, device="cuda"))

    summary = []
    photos = sorted(f for f in os.listdir(a.input) if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")))
    for fn in photos:
        name = os.path.splitext(fn)[0]
        image = Image.open(os.path.join(a.input, fn)).convert("RGB")
        for seed in [int(s) for s in a.seeds.split(",")]:
            t0 = time.time()
            # preprocess_image (default) removes the white background and centres the product
            mesh = pipeline.run(image, seed=seed, pipeline_type=a.resolution)[0]
            mesh.simplify(16777216)  # nvdiffrast limit
            t_gen = time.time() - t0
            stem = os.path.join(a.output, f"{name}-s{seed}")
            if envmap is not None:
                frames = render_utils.make_pbr_vis_frames(render_utils.render_video(mesh, envmap=envmap))
                imageio.mimsave(stem + ".mp4", frames, fps=15)
            glb = o_voxel.postprocess.to_glb(
                vertices=mesh.vertices,
                faces=mesh.faces,
                attr_volume=mesh.attrs,
                coords=mesh.coords,
                attr_layout=mesh.layout,
                voxel_size=mesh.voxel_size,
                aabb=[[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]],
                decimation_target=300000,  # plenty for furniture, keeps the file browser-friendly
                texture_size=a.texture_size,
                remesh=True,
                remesh_band=1,
                remesh_project=0,
                verbose=False,
            )
            glb.export(stem + ".glb", extension_webp=True)
            row = {"photo": fn, "seed": seed, "generate_s": round(t_gen, 1), "total_s": round(time.time() - t0, 1), "glb": os.path.basename(stem) + ".glb"}
            print(row, flush=True)
            summary.append(row)
            torch.cuda.empty_cache()

    with open(os.path.join(a.output, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1)


if __name__ == "__main__":
    main()
