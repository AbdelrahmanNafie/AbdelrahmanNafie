/**
 * Fast, model-free segmentation helpers for studio product photos.
 *  - smartSelect: click-to-select a material region (region growing on texture-smoothed Lab,
 *    stopped by strong luminance edges)
 *  - autoSuggest: k-means over the product pixels to propose initial parts, with a guess
 *    of the material type for each one
 * All of it runs in the browser in milliseconds: no server, no model download, no per-image cost.
 */
import type { PartKind } from "../types";
import { boxBlur } from "./imageOps";
import type { LoadedImage } from "./prepare";

const smoothCache = new WeakMap<LoadedImage, Map<number, { L: Float32Array; A: Float32Array; B: Float32Array; G: Float32Array }>>();

/** Lab blurred enough to wash out fabric weaves / wood grain, plus a gradient-magnitude map. */
export function smoothedLab(li: LoadedImage, r: number) {
  let m = smoothCache.get(li);
  if (!m) smoothCache.set(li, (m = new Map()));
  const hit = m.get(r);
  if (hit) return hit;
  const { w, h, lab } = li;
  const L = boxBlur(boxBlur(lab.L, w, h, r), w, h, Math.max(1, r >> 1));
  const A = boxBlur(boxBlur(lab.A, w, h, r), w, h, Math.max(1, r >> 1));
  const B = boxBlur(boxBlur(lab.B, w, h, r), w, h, Math.max(1, r >> 1));
  const G = new Float32Array(w * h);
  for (let y = 1; y < h - 1; y++)
    for (let x = 1; x < w - 1; x++) {
      const i = y * w + x;
      const gx = L[i + 1] - L[i - 1];
      const gy = L[i + w] - L[i - w];
      G[i] = Math.hypot(gx, gy);
    }
  const v = { L, A, B, G };
  m.set(r, v);
  return v;
}

export interface SmartSelectOptions {
  tolerance: number; // Lab ΔE
  smoothing: number; // px
  edgeStop: number; // gradient threshold
  ignoreBackground: boolean;
}

export function smartSelect(li: LoadedImage, sx: number, sy: number, o: SmartSelectOptions): Uint8Array {
  const { w, h, bg } = li;
  const s = smoothedLab(li, Math.max(1, Math.round(Math.max(o.smoothing, Math.hypot(w, h) / 450))));
  const out = new Uint8Array(w * h);
  const x0 = Math.round(sx);
  const y0 = Math.round(sy);
  if (x0 < 0 || y0 < 0 || x0 >= w || y0 >= h) return out;
  const seed = y0 * w + x0;
  // running mean colour of the region, so gradual shading changes are followed
  let mL = s.L[seed], mA = s.A[seed], mB = s.B[seed], cnt = 1;
  const queue = new Int32Array(w * h);
  let qh = 0, qt = 0;
  queue[qt++] = seed;
  out[seed] = 1;
  const tol2 = o.tolerance * o.tolerance;
  while (qh < qt) {
    const i = queue[qh++];
    const x = i % w;
    const nb = [x > 0 ? i - 1 : -1, x < w - 1 ? i + 1 : -1, i - w, i + w];
    for (const j of nb) {
      if (j < 0 || j >= w * h || out[j]) continue;
      if (o.ignoreBackground && bg.floor[j]) continue;
      const dL = (s.L[j] - mL) * 0.8; // lighting varies a lot on one material: weigh L less than chroma
      const dA = s.A[j] - mA;
      const dB = s.B[j] - mB;
      const d2 = dL * dL + 1.6 * (dA * dA + dB * dB);
      if (d2 > tol2) continue;
      if (s.G[j] > o.edgeStop && d2 > tol2 * 0.25) continue;
      out[j] = 1;
      queue[qt++] = j;
      if (cnt < 4000) {
        cnt++;
        mL += (s.L[j] - mL) / cnt;
        mA += (s.A[j] - mA) / cnt;
        mB += (s.B[j] - mB) / cnt;
      }
    }
  }
  return fillHoles(out, w, h, 40);
}

/** Fill small enclosed holes (specular glints, grommets…) up to maxArea pixels. */
export function fillHoles(mask: Uint8Array, w: number, h: number, maxArea: number): Uint8Array {
  const seen = new Uint8Array(mask.length);
  const out = mask.slice();
  const stack: number[] = [];
  for (let s = 0; s < mask.length; s++) {
    if (mask[s] || seen[s]) continue;
    const comp: number[] = [];
    let touchesBorder = false;
    stack.push(s);
    seen[s] = 1;
    while (stack.length) {
      const i = stack.pop()!;
      comp.push(i);
      const x = i % w;
      const y = (i / w) | 0;
      if (x === 0 || y === 0 || x === w - 1 || y === h - 1) touchesBorder = true;
      for (const j of [x > 0 ? i - 1 : -1, x < w - 1 ? i + 1 : -1, y > 0 ? i - w : -1, y < h - 1 ? i + w : -1]) {
        if (j >= 0 && !mask[j] && !seen[j]) {
          seen[j] = 1;
          stack.push(j);
        }
      }
    }
    if (!touchesBorder && comp.length <= maxArea) for (const i of comp) out[i] = 1;
  }
  return out;
}

export function paintCircle(mask: Uint8Array, w: number, h: number, cx: number, cy: number, r: number, value: 0 | 1, limit?: Uint8Array) {
  const x0 = Math.max(0, Math.floor(cx - r));
  const x1 = Math.min(w - 1, Math.ceil(cx + r));
  const y0 = Math.max(0, Math.floor(cy - r));
  const y1 = Math.min(h - 1, Math.ceil(cy + r));
  const r2 = r * r;
  for (let y = y0; y <= y1; y++)
    for (let x = x0; x <= x1; x++) {
      const dx = x - cx;
      const dy = y - cy;
      if (dx * dx + dy * dy <= r2) {
        const i = y * w + x;
        if (value && limit && !limit[i]) continue;
        mask[i] = value;
      }
    }
}

export interface Suggestion {
  mask: Uint8Array;
  kind: PartKind;
  name: string;
  meanColor: [number, number, number];
}

/**
 * Material-type guess from colour, local texture, grain direction and thickness of a region.
 * Calibrated on the demo products; it is only a starting suggestion that the user can change.
 */
export function guessKind(li: LoadedImage, mask: Uint8Array): PartKind {
  const { w, h, lab } = li;
  // measure texture at a fixed physical-ish scale, independent of the working resolution
  const st = Math.max(1, Math.round(Math.hypot(w, h) / 680));
  const sw = st * w;
  let n = 0, sL = 0, sA = 0, sB = 0, tex = 0, gxx = 0, gyy = 0, area = 0, perim = 0;
  for (let y = st; y < h - st; y++)
    for (let x = st; x < w - st; x++) {
      const i = y * w + x;
      if (!mask[i]) continue;
      area++;
      if (!mask[i - 1] || !mask[i + 1] || !mask[i - w] || !mask[i + w]) perim++;
      if (!mask[i - st] || !mask[i + st] || !mask[i - sw] || !mask[i + sw]) continue;
      n++;
      sL += lab.L[i];
      sA += lab.A[i];
      sB += lab.B[i];
      const gx = lab.L[i + st] - lab.L[i - st];
      const gy = lab.L[i + sw] - lab.L[i - sw];
      tex += Math.abs(gx) + Math.abs(gy);
      gxx += gx * gx;
      gyy += gy * gy;
    }
  if (n < 20) return "other";
  const L = sL / n;
  const C = Math.hypot(sA / n, sB / n);
  const hue = ((Math.atan2(sB / n, sA / n) * 180) / Math.PI + 360) % 360;
  const texture = tex / n; // mean |∇L|
  const directional = Math.abs(gxx - gyy) / (gxx + gyy + 1e-6); // grain runs one way
  const thickness = (2 * area) / Math.max(1, perim) / st; // ≈ mean stroke width, in 680-px-diagonal units
  const warm = hue > 25 && hue < 90;
  if (thickness < 680 * 0.012) return hue > 40 && hue < 90 && C > 12 && C < 35 ? "wood" : "metal"; // legs, frames, rails
  if (C < 6 && L < 45) return "metal";
  if (texture > 12) return "fabric"; // patterned upholstery
  if (C >= 12 && texture < 6.5) return "leather"; // saturated & smooth
  if (warm && directional > 0.42) return "wood";
  if (texture > 6.5) return "fabric";
  if (warm && C > 5) return "wood";
  return "other";
}

/** k-means (k-means++ seeding) on smoothed Lab of product pixels → proposed parts. */
export function autoSuggest(li: LoadedImage, k: number): Suggestion[] {
  const { w, h, bg } = li;
  const s = smoothedLab(li, Math.max(3, Math.round(Math.hypot(w, h) / 140)));
  const idx: number[] = [];
  for (let i = 0; i < w * h; i++) if (!bg.floor[i] && !bg.nearWhite[i]) idx.push(i);
  if (idx.length < k * 10) return [];
  const feat = (i: number): [number, number, number] => [s.L[i] * 0.7, s.A[i] * 1.6, s.B[i] * 1.6];
  // deterministic sub-sample for speed
  const step = Math.max(1, Math.floor(idx.length / 20000));
  const sample = idx.filter((_, j) => j % step === 0).map(feat);
  let rng = 1234567;
  const rand = () => ((rng = (rng * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
  const centers: [number, number, number][] = [sample[Math.floor(rand() * sample.length)]];
  const d2 = (a: number[], b: number[]) => (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2;
  while (centers.length < k) {
    const dist = sample.map((p) => Math.min(...centers.map((c) => d2(p, c))));
    const tot = dist.reduce((a, b) => a + b, 0);
    let r = rand() * tot;
    let pick = 0;
    for (; pick < dist.length - 1 && r > dist[pick]; pick++) r -= dist[pick];
    centers.push([...sample[pick]] as [number, number, number]);
  }
  for (let it = 0; it < 20; it++) {
    const acc = centers.map(() => [0, 0, 0, 0]);
    for (const p of sample) {
      let best = 0;
      let bd = Infinity;
      centers.forEach((c, ci) => {
        const d = d2(p, c);
        if (d < bd) {
          bd = d;
          best = ci;
        }
      });
      acc[best][0] += p[0];
      acc[best][1] += p[1];
      acc[best][2] += p[2];
      acc[best][3]++;
    }
    acc.forEach((a, ci) => {
      if (a[3]) centers[ci] = [a[0] / a[3], a[1] / a[3], a[2] / a[3]];
    });
  }
  // assign every product pixel, then a 5x5 majority filter to remove speckle
  const lab = new Int8Array(w * h).fill(-1);
  for (const i of idx) {
    const p = feat(i);
    let best = 0;
    let bd = Infinity;
    centers.forEach((c, ci) => {
      const d = d2(p, c);
      if (d < bd) {
        bd = d;
        best = ci;
      }
    });
    lab[i] = best;
  }
  const clean = lab.slice();
  const votes = new Int32Array(k);
  for (const i of idx) {
    const x = i % w;
    const y = (i / w) | 0;
    votes.fill(0);
    for (let dy = -2; dy <= 2; dy++)
      for (let dx = -2; dx <= 2; dx++) {
        const xx = x + dx, yy = y + dy;
        if (xx < 0 || yy < 0 || xx >= w || yy >= h) continue;
        const v = lab[yy * w + xx];
        if (v >= 0) votes[v]++;
      }
    let best = lab[i];
    for (let c = 0; c < k; c++) if (votes[c] > votes[best]) best = c;
    clean[i] = best;
  }
  const out: Suggestion[] = [];
  const total = idx.length;
  const counter: Record<string, number> = {};
  for (let c = 0; c < k; c++) {
    const mask = new Uint8Array(w * h);
    let n = 0;
    for (const i of idx) if (clean[i] === c) {
      mask[i] = 1;
      n++;
    }
    if (n < total * 0.01) continue;
    const kind = guessKind(li, mask);
    counter[kind] = (counter[kind] ?? 0) + 1;
    const label = kind === "other" ? "Part" : kind[0].toUpperCase() + kind.slice(1);
    const [cl, ca, cb] = centers[c];
    out.push({ mask: fillHoles(mask, w, h, 60), kind, name: `${label} ${counter[kind]}`, meanColor: [cl / 0.7, ca / 1.6, cb / 1.6] });
  }
  return out;
}
