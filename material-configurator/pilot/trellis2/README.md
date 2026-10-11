# TRELLIS.2 pilot — photos → 3D models

Goal: find out, with a one-off test, whether [TRELLIS.2](https://github.com/microsoft/TRELLIS.2) builds
3D models from our product photos that are faithful enough for client renders. If it does, every
product photo can become a fully configurable 3D product in the 3D Studio.

`input/` holds the three high-resolution photos (tub chair, club chair, executive desk).

## 1. Rent a GPU (one-off, a few hours)

Any cloud with a Linux machine and an NVIDIA GPU with **at least 24 GB** (TRELLIS.2 is tested on
A100 and H100). Ubuntu image with CUDA 12.x drivers.

## 2. Install TRELLIS.2 (≈ 20–30 min, from its README)

```bash
git clone -b main https://github.com/microsoft/TRELLIS.2.git --recursive
cd TRELLIS.2
. ./setup.sh --new-env --basic --flash-attn --nvdiffrast --nvdiffrec --cumesh --o-voxel --flexgemm
conda activate trellis2
```

The model weights (`microsoft/TRELLIS.2-4B`) download from Hugging Face on first run.

## 3. Run the pilot

Copy this `pilot/trellis2` folder onto the machine, then from inside the `TRELLIS.2` folder:

```bash
python /path/to/pilot/trellis2/run_pilot.py --input /path/to/pilot/trellis2/input --output ~/pilot-out
```

Per photo it makes two models (two random seeds) plus a turntable video of each — 6 models,
roughly 2–4 minutes each at the highest quality (`--resolution 1536_cascade`, the default).
Download `~/pilot-out` when it finishes (GLB, MP4, summary.json).

## 4. Judge the results (go / no-go)

Watch the turntable videos against the photos and score each model:

| Check | Pass when |
|---|---|
| Proportions | width / height / depth match the photo; nothing stretched |
| Thin parts | legs, frame tubes and the desk's X-frame are straight, complete and the right thickness |
| Upholstery | cushion shape, seams and the shell's curve read like the product |
| Hidden side | the back (not in the photo) is plausible enough for a ¾ catalog view |
| Surfaces | no holes, lumps or melted areas where materials meet |

**Go** if most models pass at the front and ¾ views used in catalogs. **No-go** if thin parts or
proportions fail repeatedly — then the idea stays parked until a better model appears.

## 5. If it is a go

Open any GLB in the configurator with **Add product**. Generated models come as one fused piece,
so the next step on our side is:

- splitting the model into parts (seat, shell, frame…) from the part outlines we already have for each photo;
- mapping the Mobica swatches without visible seams (triplanar mapping), since the generated texture layout is fragmented.

## Licence

TRELLIS.2 itself is MIT. Its GLB export uses NVIDIA nvdiffrast / nvdiffrec, licensed for
**non-commercial research or evaluation only** — fine for this pilot. For production we either
export the shape another way (the finishes come from the Mobica library anyway) or have the
licence reviewed.
