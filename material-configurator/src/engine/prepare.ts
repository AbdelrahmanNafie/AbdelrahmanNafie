/**
 * Turns a product photo + part definitions into everything the renderer needs:
 *  - a hard label map (which part owns each pixel, used for picking/editing)
 *  - a soft alpha per part (edges snapped to the photo with a guided filter)
 *  - a "lighting" map per part: the photo's luminance with the original material's
 *    pattern/grain removed. The new material is re-lit with this map.
 */
import type { PartKind, ProductSpec, RenderTuning } from "../types";
import {
  bbox,
  decodeBitmapMask,
  detectBackground,
  dilate,
  guidedFilter,
  imageToData,
  linearLuminance,
  loadImage,
  maskedBlur,
  rasterizePolygon,
  toLab,
  type BackgroundInfo,
} from "./imageOps";

export const DEFAULT_TUNING: Record<PartKind, RenderTuning> = {
  // smoothing is % of the image diagonal
  wood: { smoothing: 0.9, contrast: 1, highlights: 1, detail: 0.15, exposure: 1 },
  fabric: { smoothing: 1.1, contrast: 1, highlights: 0.6, detail: 0.12, exposure: 1 },
  leather: { smoothing: 0.6, contrast: 1, highlights: 1, detail: 0.3, exposure: 1 },
  metal: { smoothing: 0.18, contrast: 0.85, highlights: 1, detail: 0.5, exposure: 1 },
  stone: { smoothing: 0.9, contrast: 1, highlights: 1, detail: 0.15, exposure: 1 },
  glass: { smoothing: 0.4, contrast: 1, highlights: 1, detail: 0.4, exposure: 1 },
  other: { smoothing: 0.8, contrast: 1, highlights: 1, detail: 0.2, exposure: 1 },
};

export interface PreparedPart {
  index: number;
  hard: Uint8Array;
  alpha: Float32Array;
  /** smoothed linear luminance (lighting) */
  shade: Float32Array;
  /**
   * For anti-aliased pixels on the product silhouette: the local backdrop luminance (linear),
   * else 0. Those pixels are re-composited over the backdrop instead of over the old material,
   * which removes halos of the previous finish along the outline.
   */
  edgeBg: Float32Array;
  /** mean of the lighting map inside the part (normaliser) */
  meanShade: number;
  smoothingUsed: number;
  area: number;
  box: { x0: number; y0: number; x1: number; y1: number } | null;
}

export interface PreparedProduct {
  spec: ProductSpec;
  img: HTMLImageElement;
  w: number;
  h: number;
  pixels: ImageData;
  lab: { L: Float32Array; A: Float32Array; B: Float32Array };
  Y: Float32Array;
  bg: BackgroundInfo;
  /** -1 = not customisable, else index into spec.parts */
  labels: Int16Array;
  parts: PreparedPart[];
  /** product bounding box (pixels), used for physical scale & catalog crop */
  productBox: { x0: number; y0: number; x1: number; y1: number };
  pxPerCm: number;
}

export async function loadProductImage(spec: ProductSpec) {
  const img = await loadImage(spec.image);
  const w = img.naturalWidth;
  const h = img.naturalHeight;
  const pixels = imageToData(img, w, h);
  const lab = toLab(pixels);
  const Y = linearLuminance(pixels);
  const bg = detectBackground(lab, w, h);
  return { img, w, h, pixels, lab, Y, bg };
}

export type LoadedImage = Awaited<ReturnType<typeof loadProductImage>>;

/** Rasterise all regions into a label map. Later parts/regions override earlier ones. */
export async function buildLabels(spec: ProductSpec, li: LoadedImage): Promise<{ labels: Int16Array; hard: Uint8Array[] }> {
  const { w, h, lab, bg } = li;
  const n = w * h;
  const labels = new Int16Array(n).fill(-1);
  for (let pi = 0; pi < spec.parts.length; pi++) {
    for (const r of spec.parts[pi].regions) {
      let m: Uint8Array;
      if (r.type === "poly") {
        m = rasterizePolygon(r.points, w, h);
        const clip = r.clip ?? "strict";
        for (let i = 0; i < n; i++) {
          if (!m[i]) continue;
          if (clip === "strict" && bg.nearWhite[i]) m[i] = 0;
          else if (clip === "flood" && bg.backdrop[i]) m[i] = 0;
          else if (r.minL !== undefined && lab.L[i] < r.minL) m[i] = 0;
          else if (r.maxL !== undefined && lab.L[i] > r.maxL) m[i] = 0;
        }
      } else {
        m = await decodeBitmapMask(r.src, w, h);
      }
      if (r.op === "sub") {
        for (let i = 0; i < n; i++) if (m[i] && labels[i] === pi) labels[i] = -1;
      } else {
        for (let i = 0; i < n; i++) if (m[i]) labels[i] = pi;
      }
    }
  }
  closeSilhouettes(labels, spec.parts.length, w, h);
  return { labels, hard: hardMasksFromLabels(labels, spec.parts.length) };
}

/**
 * Morphological closing of each part into unlabelled pixels. Fills the small dents a
 * backdrop clip leaves where the old finish had near-white flecks touching the outline
 * (e.g. the light checks of a houndstooth), without growing smooth outlines.
 */
export function closeSilhouettes(labels: Int16Array, count: number, w: number, h: number) {
  const r = Math.max(1, Math.round(Math.hypot(w, h) / 350));
  const hard = hardMasksFromLabels(labels, count);
  for (let k = 0; k < count; k++) {
    const grown = dilate(hard[k], w, h, r);
    // erode = not(dilate(not grown))
    const inv = new Uint8Array(grown.length);
    for (let i = 0; i < inv.length; i++) inv[i] = grown[i] ? 0 : 1;
    const shrunk = dilate(inv, w, h, r);
    for (let i = 0; i < labels.length; i++) if (labels[i] < 0 && !shrunk[i]) labels[i] = k;
  }
}

export function hardMasksFromLabels(labels: Int16Array, count: number): Uint8Array[] {
  const hard = Array.from({ length: count }, () => new Uint8Array(labels.length));
  for (let i = 0; i < labels.length; i++) if (labels[i] >= 0) hard[labels[i]][i] = 1;
  return hard;
}

/** Edge-aware soft alpha: guided filter of the hard mask, guided by the photo. */
export function softAlpha(hard: Uint8Array, L: Float32Array, w: number, h: number): Float32Array {
  const n = w * h;
  const p = new Float32Array(n);
  const I = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    p[i] = hard[i];
    I[i] = L[i] / 100;
  }
  const q = guidedFilter(I, p, w, h, 2, 2e-3);
  const near = dilate(hard, w, h, 2);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    let a = near[i] ? Math.min(1, Math.max(0, q[i])) : 0;
    // keep the interior solid; let only boundary pixels go soft
    if (hard[i] && a > 0.75) a = 1;
    out[i] = a;
  }
  return out;
}

export function computeShade(Y: Float32Array, hard: Uint8Array, w: number, h: number, smoothingPct: number) {
  const n = w * h;
  const m = new Float32Array(n);
  for (let i = 0; i < n; i++) m[i] = hard[i];
  const diag = Math.hypot(w, h);
  const r = Math.max(1, Math.round((smoothingPct / 100) * diag));
  const shade = maskedBlur(Y, m, w, h, r);
  // Highlight-robust normaliser: mean of the lighting between its 10th and 90th percentile,
  // so a specular reflection band does not darken the whole surface.
  let area = 0;
  for (let i = 0; i < n; i++) area += hard[i];
  const step = Math.max(1, Math.floor(area / 60000));
  const vals: number[] = [];
  for (let i = 0, k = 0; i < n; i++) if (hard[i] && k++ % step === 0) vals.push(shade[i]);
  vals.sort((a, b) => a - b);
  const lo = vals[Math.floor(vals.length * 0.1)] ?? 0;
  const hi = vals[Math.floor(vals.length * 0.9)] ?? 1;
  let s = 0;
  let c = 0;
  for (const v of vals)
    if (v >= lo && v <= hi) {
      s += v;
      c++;
    }
  return { shade, meanShade: c ? s / c : 0.18 };
}

/** Backdrop luminance map, estimated only from backdrop pixels (normalised convolution). */
const bgCache = new WeakMap<LoadedImage, { near: Uint8Array; bgY: Float32Array }>();
function backdropInfo(li: LoadedImage) {
  let v = bgCache.get(li);
  if (v) return v;
  const { w, h, Y, bg } = li;
  const m = new Float32Array(w * h);
  for (let i = 0; i < m.length; i++) m[i] = bg.backdrop[i];
  const bgY = maskedBlur(Y, m, w, h, 4);
  const near = dilate(bg.backdrop, w, h, 2);
  v = { near, bgY };
  bgCache.set(li, v);
  return v;
}

export function preparePart(li: LoadedImage, index: number, hard: Uint8Array, smoothingPct: number): PreparedPart {
  const { w, h, lab, Y } = li;
  const alpha = softAlpha(hard, lab.L, w, h);
  const { shade, meanShade } = computeShade(Y, hard, w, h, smoothingPct);
  const { near, bgY } = backdropInfo(li);
  const edgeBg = new Float32Array(w * h);
  let area = 0;
  for (let i = 0; i < hard.length; i++) {
    area += hard[i];
    if (alpha[i] > 0 && alpha[i] < 1 && near[i]) edgeBg[i] = Math.max(1e-3, bgY[i]);
  }
  return { index, hard, alpha, shade, edgeBg, meanShade, smoothingUsed: smoothingPct, area, box: bbox(hard, w, h) };
}

export function tuningFor(spec: ProductSpec, partIndex: number, overrides?: Partial<RenderTuning>): RenderTuning {
  const part = spec.parts[partIndex];
  return { ...DEFAULT_TUNING[part.kind], ...(part.tuning ?? {}), ...(overrides ?? {}) };
}

export function productBox(li: LoadedImage, labels: Int16Array) {
  const fg = new Uint8Array(labels.length);
  for (let i = 0; i < fg.length; i++) fg[i] = labels[i] >= 0 || !li.bg.nearWhite[i] ? 1 : 0;
  return bbox(fg, li.w, li.h) ?? { x0: 0, y0: 0, x1: li.w - 1, y1: li.h - 1 };
}

export async function prepareProduct(spec: ProductSpec, li?: LoadedImage, tuning?: Record<string, Partial<RenderTuning>>): Promise<PreparedProduct> {
  const loaded = li ?? (await loadProductImage(spec));
  const { labels, hard } = await buildLabels(spec, loaded);
  return assemble(spec, loaded, labels, hard, tuning);
}

export function assemble(
  spec: ProductSpec,
  li: LoadedImage,
  labels: Int16Array,
  hard: Uint8Array[],
  tuning?: Record<string, Partial<RenderTuning>>,
): PreparedProduct {
  const parts = spec.parts.map((p, i) => preparePart(li, i, hard[i], tuningFor(spec, i, tuning?.[p.id]).smoothing));
  const pb = productBox(li, labels);
  // Physical scale: the widest extent of the product in pixels equals its real width.
  const pxPerCm = (pb.x1 - pb.x0 + 1) / Math.max(1, spec.widthCm);
  return { spec, ...li, labels, parts, productBox: pb, pxPerCm };
}
