#!/usr/bin/env python3
"""
Build the material library used by the configurator.

Input : the Mobica finishes PDF + tools/mobica_catalog.csv (page -> code/name/category)
Output: public/materials/<id>.jpg      512x512 seamless (tileable) albedo texture
        public/materials/thumbs/<id>.jpg  160x160 swatch thumbnail (untouched crop)
        public/materials/materials.json   the library manifest

Why a CSV for codes: the PDF was exported from Illustrator with all text converted
to outlines, so codes cannot be read from the text layer. The CSV is the single
source of truth for codes/names; to add a material, append a row (and the page
or an image path) and re-run this script.

Requirements: python3, numpy, pillow, poppler-utils (pdfimages).
Usage: python3 tools/build_materials.py path/to/Mobica_Finishes.pdf
"""
import csv
import json
import math
import os
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "public", "materials")
TEX = 512

# Swatch area inside each 591x591 page image (the top ~120px is the logo/code header).
CROP = (60, 120, 531, 591)

# Physical size (cm) that one texture tile represents. Calibrated by eye against the
# swatch scans (thread pitch for fabric, pore/grain size for leather, figure for wood).
TILE_CM = {"fabric": 10.0, "leather-natural": 22.0, "leather-artificial": 22.0, "wood": 55.0, "metal": 14.0}

CATEGORY_LABEL = {
    "fabric": "Fabric",
    "leather-natural": "Natural Leather",
    "leather-artificial": "Artificial Leather",
    "wood": "Wood",
    "metal": "Metal",
    "concept": "Concept (not in catalog)",
}


def shader_for(row):
    """Physical rendering parameters per material. See src/render/shader.ts."""
    cat, code = row["category"], row["code"]
    if cat == "fabric":
        return dict(kind="fabric", spec=0.04, gloss=0.15, sheen=0.25, relief=0.55)
    if cat.startswith("leather"):
        natural = cat == "leather-natural"
        return dict(kind="leather", spec=0.30 if natural else 0.42, gloss=0.45 if natural else 0.6, sheen=0.0, relief=0.35)
    if cat == "wood":
        plain = code == "101"
        return dict(kind="wood", spec=0.22, gloss=0.55, sheen=0.0, relief=0.08 if plain else 0.15)
    if cat == "metal":
        if code == "9006":
            return dict(kind="metal", spec=0.85, gloss=0.6, sheen=0.0, relief=0.15, metallic=0.75)
        if code == "8519-1":
            return dict(kind="metal", spec=0.7, gloss=0.4, sheen=0.0, relief=0.45, metallic=0.6)
        return dict(kind="metal", spec=0.45, gloss=0.5, sheen=0.0, relief=0.1, metallic=0.0)
    raise ValueError(cat)


# ---------------------------------------------------------------- image helpers
def flatten_illumination(a, sigma):
    """Remove slow brightness gradients from the scan so tiles do not checkerboard."""
    img = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    low = np.asarray(img.filter(ImageFilter.GaussianBlur(sigma))).astype(np.float64)
    mean = a.reshape(-1, 3).mean(0)
    return a / np.maximum(low, 1) * mean


def min_cut_columns(err):
    """Dynamic-programming vertical min-cut through an (H, B) error band. Returns cut x per row."""
    h, b = err.shape
    cost = err.copy()
    back = np.zeros((h, b), dtype=np.int64)
    for y in range(1, h):
        prev = cost[y - 1]
        left = np.r_[np.inf, prev[:-1]]
        right = np.r_[prev[1:], np.inf]
        stack = np.stack([left, prev, right])
        idx = stack.argmin(0)
        cost[y] += stack[idx, np.arange(b)]
        back[y] = np.arange(b) + idx - 1
    cut = np.zeros(h, dtype=np.int64)
    cut[-1] = int(cost[-1].argmin())
    for y in range(h - 1, 0, -1):
        cut[y - 1] = back[y, cut[y]]
    return cut


def make_tileable_x(a, band):
    """Quilting-style horizontal wrap: overlap the right overflow onto the left band along a min-error seam."""
    h, w, _ = a.shape
    W = w - band
    right = a[:, W:W + band]
    left = a[:, :band]
    err = ((right - left) ** 2).sum(-1)
    cut = min_cut_columns(err)
    out = a[:, :W].copy()
    xs = np.arange(band)[None, :]
    # soft 4px feather around the seam to hide pixel steps
    t = np.clip((xs - cut[:, None] + 2) / 4.0, 0, 1)[..., None]
    out[:, :band] = right * (1 - t) + left * t
    return out


def make_tileable(a):
    band = max(24, a.shape[1] // 7)
    a = make_tileable_x(a, band)
    a = make_tileable_x(a.transpose(1, 0, 2), band).transpose(1, 0, 2)
    return a


def equalize_bands(a, sigma=4.0):
    """Remove darker/lighter horizontal or vertical bands (scan artefacts, weft bars) wider than
    a few pixels; they would otherwise line up into stripes when the tile repeats."""
    from numpy.fft import fft, ifft
    out = a.copy()
    for axis in (0, 1):
        prof = out.mean(axis=(1 - axis, 2))  # mean per row (axis 0) or column (axis 1)
        n = prof.size
        k = np.fft.fftfreq(n)
        g = np.exp(-0.5 * (k * 2 * np.pi * sigma) ** 2)
        smooth = np.real(ifft(fft(prof) * g))  # periodic smoothing
        gain = prof.mean() / np.maximum(smooth, 1)
        out = out * (gain[:, None, None] if axis == 0 else gain[None, :, None])
    return out


def flatten_periodic(a, sigma):
    """Like flatten_illumination but with wrap-around blur, so every tile has the same
    low-frequency brightness and a repeated pattern never shows a grid."""
    h, w, _ = a.shape
    big = np.tile(a, (3, 3, 1))
    img = Image.fromarray(np.clip(big, 0, 255).astype(np.uint8))
    low = np.asarray(img.filter(ImageFilter.GaussianBlur(sigma))).astype(np.float64)[h:2 * h, w:2 * w]
    return a / np.maximum(low, 1) * a.reshape(-1, 3).mean(0)


def to_jpg(a, path, size=None, q=90):
    img = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    if size:
        img = img.resize((size, size), Image.LANCZOS)
    img.save(path, quality=q, optimize=True)


def srgb_to_linear(c):
    c = c / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


# ---------------------------------------------------------- procedural concepts
def fbm(shape, octaves=6, seed=0, base=4):
    rng = np.random.default_rng(seed)
    h, w = shape
    out = np.zeros(shape)
    amp, total = 1.0, 0.0
    for o in range(octaves):
        n = base * 2 ** o
        g = rng.random((n + 1, n + 1))
        g[-1, :] = g[0, :]
        g[:, -1] = g[:, 0]  # periodic -> tileable
        img = Image.fromarray((g * 255).astype(np.uint8)).resize((w + w // n, h + h // n), Image.BICUBIC)
        layer = np.asarray(img).astype(np.float64)[:h, :w] / 255.0
        out += layer * amp
        total += amp
        amp *= 0.5
    return out / total


def concept_materials():
    """Materials NOT in the Mobica PDF, generated procedurally to show what the engine can do."""
    S = TEX
    yy, xx = np.mgrid[0:S, 0:S] / S
    items = []

    # Carrara-style white marble: turbulent sine veins (periodic in both axes -> tileable)
    turb = fbm((S, S), 7, seed=3, base=2)
    turb2 = fbm((S, S), 6, seed=4, base=4)
    main_v = np.exp(-np.abs(np.sin(2 * math.pi * (xx + yy) + turb * 7.0)) / 0.05)
    fine_v = np.exp(-np.abs(np.sin(2 * math.pi * (2 * xx - yy) + turb2 * 9.0)) / 0.025) * 0.45
    v = np.clip(main_v * (0.4 + 0.6 * turb2) + fine_v, 0, 1)
    cloud = fbm((S, S), 6, seed=9, base=4)
    base = np.array([240, 238, 234.0])[None, None] * (0.95 + 0.06 * cloud[..., None])
    vein = np.array([128, 130, 136.0])
    m = base * (1 - v[..., None] * 0.7) + vein * v[..., None] * 0.7
    m = make_tileable(m)
    items.append(("marble-carrara", "Carrara White", "Polished marble", m, dict(kind="stone", spec=0.55, gloss=0.85, sheen=0.0, relief=0.0), 70.0))

    # Nero Marquina-style black marble
    turb = fbm((S, S), 7, seed=11, base=2)
    turb2 = fbm((S, S), 6, seed=12, base=4)
    v = np.exp(-np.abs(np.sin(2 * math.pi * (xx - yy) + turb * 8.0)) / 0.03)
    v = np.clip(v + 0.5 * np.exp(-np.abs(np.sin(2 * math.pi * (xx + 2 * yy) + turb2 * 10.0)) / 0.015), 0, 1)
    dark = np.array([26, 26, 28.0])[None, None] * (0.85 + 0.3 * fbm((S, S), 5, seed=13, base=4)[..., None])
    m = dark * (1 - v[..., None]) + np.array([222, 218, 210.0]) * v[..., None]
    m = make_tileable(m)
    items.append(("marble-nero", "Nero Marquina", "Polished marble", m, dict(kind="stone", spec=0.6, gloss=0.9, sheen=0.0, relief=0.0), 70.0))

    # Brushed nickel: anisotropic streaks along x
    rng = np.random.default_rng(5)
    streak = rng.random((S, 1)) * 0.6 + np.asarray(Image.fromarray((rng.random((S, 8)) * 255).astype(np.uint8)).resize((S, S), Image.BICUBIC)) / 255 * 0.4
    streak = np.asarray(Image.fromarray((streak * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.6))) / 255.0
    nick = np.array([176, 172, 164.0])[None, None] * (0.86 + 0.22 * streak[..., None])
    items.append(("brushed-nickel", "Brushed Nickel", "Brushed metal", nick, dict(kind="metal", spec=0.9, gloss=0.45, sheen=0.0, relief=0.0, metallic=1.0, aniso=1.0), 8.0))

    # Polished chrome: shading-driven mirror look (texture is a neutral base)
    chrome = np.full((S, S, 3), [200, 204, 210.0])
    items.append(("chrome", "Polished Chrome", "Mirror metal", chrome, dict(kind="chrome", spec=1.0, gloss=1.0, sheen=0.0, relief=0.0, metallic=1.0), 10.0))

    # Glass (rendered against the white studio backdrop; see limitation note in the UI)
    clear = np.full((S, S, 3), [222, 236, 232.0])
    items.append(("glass-clear", "Clear Glass", "Transparent (assumes white backdrop)", clear, dict(kind="glass", spec=0.9, gloss=1.0, sheen=0.0, relief=0.0, opacity=0.18), 40.0))
    frost = np.full((S, S, 3), [232, 238, 238.0]) * (0.98 + 0.03 * fbm((S, S), 3, seed=2, base=32))[..., None]
    items.append(("glass-frosted", "Frosted Glass", "Satin etched (assumes white backdrop)", frost, dict(kind="glass", spec=0.35, gloss=0.4, sheen=0.0, relief=0.0, opacity=0.55), 40.0))
    return items


# ---------------------------------------------------------------------- main
def main(pdf):
    os.makedirs(os.path.join(OUT, "thumbs"), exist_ok=True)
    tmp = tempfile.mkdtemp()
    subprocess.run(["pdfimages", "-p", "-png", pdf, os.path.join(tmp, "i")], check=True)
    pages = {}
    for f in os.listdir(tmp):
        p, n = f[2:-4].split("-")
        img = Image.open(os.path.join(tmp, f))
        if img.size == (591, 591) and img.mode != "L":  # skip soft masks
            pages[int(p)] = img.convert("RGB")

    library = []
    with open(os.path.join(ROOT, "tools", "mobica_catalog.csv"), newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        page = int(row["page"])
        cat = row["category"]
        mid = f"{cat}-{row['code']}".lower().replace(" ", "")
        crop = np.asarray(pages[page].crop(CROP)).astype(np.float64)
        to_jpg(crop, os.path.join(OUT, "thumbs", mid + ".jpg"), 160, 88)
        flat = flatten_illumination(crop, crop.shape[0] / 3.5)
        tile = make_tileable(flat)
        # isotropic finishes must be statistically uniform across the tile; wood keeps its figure
        tile = flatten_periodic(tile, tile.shape[0] / (3.0 if cat == "wood" else 10.0))
        if cat != "wood":
            tile = equalize_bands(tile)
        to_jpg(tile, os.path.join(OUT, mid + ".jpg"), TEX)
        lin = srgb_to_linear(np.clip(crop, 0, 255)).reshape(-1, 3).mean(0)
        hexc = "#%02x%02x%02x" % tuple(int(v) for v in crop.reshape(-1, 3).mean(0))
        library.append(dict(
            id=mid, code=row["code"], name=row["name"], category=cat,
            categoryLabel=CATEGORY_LABEL[cat], finish=row["finish"], source=f"Mobica Finishes 2024-II, p.{page}",
            texture=f"materials/{mid}.jpg", thumb=f"materials/thumbs/{mid}.jpg",
            avgColor=hexc, avgLinear=[round(float(x), 4) for x in lin], tileCm=TILE_CM[cat],
            inCatalog=True, render=shader_for(row),
        ))

    for mid, name, finish, arr, render, tile_cm in concept_materials():
        to_jpg(arr, os.path.join(OUT, mid + ".jpg"), None, 92)
        to_jpg(arr, os.path.join(OUT, "thumbs", mid + ".jpg"), 160, 88)
        lin = srgb_to_linear(np.clip(arr, 0, 255)).reshape(-1, 3).mean(0)
        hexc = "#%02x%02x%02x" % tuple(int(v) for v in arr.reshape(-1, 3).mean(0))
        library.append(dict(
            id=mid, code=mid.upper(), name=name, category="concept", categoryLabel=CATEGORY_LABEL["concept"],
            finish=finish, source="Procedural demo material - not part of the Mobica catalog",
            texture=f"materials/{mid}.jpg", thumb=f"materials/thumbs/{mid}.jpg", avgColor=hexc,
            avgLinear=[round(float(x), 4) for x in lin], tileCm=tile_cm, inCatalog=False, render=render,
        ))

    with open(os.path.join(OUT, "materials.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "source": os.path.basename(pdf), "materials": library}, fh, indent=1, ensure_ascii=False)
    print(f"wrote {len(library)} materials to {OUT}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
