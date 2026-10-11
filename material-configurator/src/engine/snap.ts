/**
 * "Lock on" selection: turns a rough gesture into the exact area the user meant.
 *  - snapOutline: a loosely drawn outline snaps to the material the user mostly enclosed — it
 *    follows that material's real edges (also a little beyond a line drawn too tight) and drops
 *    the other materials the loop caught.
 *  - snapToPart: a selection that is (almost) an existing part becomes exactly that part.
 * Model-free and deterministic: same gesture, same result, a few tens of milliseconds.
 */
import { fillHoles, smoothedLab } from "./segment";
import type { LoadedImage } from "./prepare";

type Feat = [number, number, number];

/** Two-pass chamfer distance (in px) from every mask pixel to the nearest non-mask pixel. */
export function insideDistance(mask: Uint8Array, w: number, h: number): Float32Array {
  const INF = 1e9;
  const d = new Float32Array(w * h);
  for (let i = 0; i < d.length; i++) d[i] = mask[i] ? INF : 0;
  const a = 1, b = Math.SQRT2;
  for (let y = 0; y < h; y++)
    for (let x = 0; x < w; x++) {
      const i = y * w + x;
      if (!d[i]) continue;
      let v = d[i];
      if (x > 0) v = Math.min(v, d[i - 1] + a);
      if (y > 0) {
        v = Math.min(v, d[i - w] + a);
        if (x > 0) v = Math.min(v, d[i - w - 1] + b);
        if (x < w - 1) v = Math.min(v, d[i - w + 1] + b);
      }
      if (x === 0 || y === 0 || x === w - 1 || y === h - 1) v = Math.min(v, 1);
      d[i] = v;
    }
  for (let y = h - 1; y >= 0; y--)
    for (let x = w - 1; x >= 0; x--) {
      const i = y * w + x;
      if (!d[i]) continue;
      let v = d[i];
      if (x < w - 1) v = Math.min(v, d[i + 1] + a);
      if (y < h - 1) {
        v = Math.min(v, d[i + w] + a);
        if (x < w - 1) v = Math.min(v, d[i + w + 1] + b);
        if (x > 0) v = Math.min(v, d[i + w - 1] + b);
      }
      d[i] = v;
    }
  return d;
}

/** Square dilation (r>0) or erosion (r<0) with separable running max/min. */
export function morph(mask: Uint8Array, w: number, h: number, r: number): Uint8Array {
  if (!r) return mask.slice();
  const grow = r > 0;
  const k = Math.abs(r);
  const tmp = new Uint8Array(mask.length);
  const out = new Uint8Array(mask.length);
  for (let y = 0; y < h; y++) {
    let run = 0; // count of "on" (grow) or "off" (shrink) pixels in the window
    const row = y * w;
    const hit = (x: number) => (grow ? mask[row + x] : mask[row + x] ? 0 : 1);
    for (let x = 0; x < Math.min(k, w); x++) run += hit(x);
    for (let x = 0; x < w; x++) {
      if (x + k < w) run += hit(x + k);
      if (x - k - 1 >= 0) run -= hit(x - k - 1);
      tmp[row + x] = grow ? (run > 0 ? 1 : 0) : run > 0 ? 0 : 1;
    }
  }
  for (let x = 0; x < w; x++) {
    let run = 0;
    const hit = (y: number) => (grow ? tmp[y * w + x] : tmp[y * w + x] ? 0 : 1);
    for (let y = 0; y < Math.min(k, h); y++) run += hit(y);
    for (let y = 0; y < h; y++) {
      if (y + k < h) run += hit(y + k);
      if (y - k - 1 >= 0) run -= hit(y - k - 1);
      out[y * w + x] = grow ? (run > 0 ? 1 : 0) : run > 0 ? 0 : 1;
    }
  }
  return out;
}

/** Keep the connected components that hold real weight (the one the user aimed at + big siblings). */
function keepMainComponents(mask: Uint8Array, weight: Float32Array, w: number, h: number): Uint8Array {
  const comp = new Int32Array(mask.length).fill(-1);
  const sizes: number[] = [];
  const weights: number[] = [];
  const stack: number[] = [];
  for (let s = 0; s < mask.length; s++) {
    if (!mask[s] || comp[s] >= 0) continue;
    const id = sizes.length;
    let n = 0, wt = 0;
    comp[s] = id;
    stack.push(s);
    while (stack.length) {
      const i = stack.pop()!;
      n++;
      wt += weight[i];
      const x = i % w;
      for (const j of [x > 0 ? i - 1 : -1, x < w - 1 ? i + 1 : -1, i - w, i + w])
        if (j >= 0 && j < mask.length && mask[j] && comp[j] < 0) {
          comp[j] = id;
          stack.push(j);
        }
    }
    sizes.push(n);
    weights.push(wt);
  }
  if (!sizes.length) return mask;
  const best = weights.indexOf(Math.max(...weights));
  const keep = sizes.map((n, i) => i === best || (weights[i] > weights[best] * 0.15 && n > sizes[best] * 0.1));
  const out = new Uint8Array(mask.length);
  for (let i = 0; i < mask.length; i++) if (comp[i] >= 0 && keep[comp[i]]) out[i] = 1;
  void h;
  return out;
}

/**
 * Snap a loosely drawn outline to the material it mainly encloses.
 * `poly` is the rasterised outline (already minus the backdrop / floor).
 */
export function snapOutline(li: LoadedImage, poly: Uint8Array): Uint8Array {
  const { w, h, bg } = li;
  const diag = Math.hypot(w, h);
  let area = 0;
  for (let i = 0; i < poly.length; i++) area += poly[i];
  if (area < 60) return poly;
  const s = smoothedLab(li, Math.max(2, Math.round(diag / 450)));
  const feat = (i: number): Feat => [s.L[i] * 0.6, s.A[i] * 1.6, s.B[i] * 1.6];
  const d2 = (p: Feat, c: Feat) => (p[0] - c[0]) ** 2 + (p[1] - c[1]) ** 2 + (p[2] - c[2]) ** 2;

  // Pixels deep inside the loop are what the user aimed at; pixels near the line are uncertain.
  const dist = insideDistance(poly, w, h);
  let dmax = 0;
  const idx: number[] = [];
  for (let i = 0; i < poly.length; i++)
    if (poly[i]) {
      idx.push(i);
      if (dist[i] > dmax) dmax = dist[i];
    }
  const step = Math.max(1, Math.floor(idx.length / 25000));
  const sample = idx.filter((_, j) => j % step === 0);
  const wOf = (i: number) => dist[i] / dmax;

  // k-means (k=3), deterministic seeding: weighted centre of the deep interior, then farthest points
  const k = 3;
  const deep = sample.filter((i) => dist[i] > dmax * 0.6);
  const c0: Feat = [0, 0, 0];
  for (const i of deep.length ? deep : sample) {
    const f = feat(i);
    c0[0] += f[0];
    c0[1] += f[1];
    c0[2] += f[2];
  }
  const nDeep = (deep.length ? deep : sample).length;
  const centers: Feat[] = [[c0[0] / nDeep, c0[1] / nDeep, c0[2] / nDeep]];
  while (centers.length < k) {
    let far = sample[0], fd = -1;
    for (const i of sample) {
      const f = feat(i);
      const m = Math.min(...centers.map((c) => d2(f, c)));
      if (m > fd) {
        fd = m;
        far = i;
      }
    }
    centers.push(feat(far));
  }
  for (let it = 0; it < 12; it++) {
    const acc = centers.map(() => [0, 0, 0, 0]);
    for (const i of sample) {
      const f = feat(i);
      let b = 0, bd = Infinity;
      centers.forEach((c, ci) => {
        const v = d2(f, c);
        if (v < bd) {
          bd = v;
          b = ci;
        }
      });
      acc[b][0] += f[0];
      acc[b][1] += f[1];
      acc[b][2] += f[2];
      acc[b][3]++;
    }
    acc.forEach((a, ci) => a[3] && (centers[ci] = [a[0] / a[3], a[1] / a[3], a[2] / a[3]]));
  }
  // the target material = the cluster with most interior weight
  const score = centers.map(() => 0);
  const assign = (f: Feat) => {
    let b = 0, bd = Infinity;
    centers.forEach((c, ci) => {
      const v = d2(f, c);
      if (v < bd) {
        bd = v;
        b = ci;
      }
    });
    return b;
  };
  for (const i of sample) score[assign(feat(i))] += wOf(i) ** 2;
  const target = score.indexOf(Math.max(...score));
  // One material in light and in shadow splits into clusters that differ in lightness but not in
  // colour: merge those. (Leather vs fabric, or fabric vs a dark metal frame, differ in colour or a lot in lightness.)
  const t = centers[target];
  const same = centers.map((c, ci) => ci === target || (Math.hypot(c[1] - t[1], c[2] - t[2]) / 1.6 < 8 && Math.abs(c[0] - t[0]) / 0.6 < 28));
  let spread = 0, sn = 0;
  for (const i of sample) {
    const f = feat(i);
    if (same[assign(f)]) {
      spread += Math.min(...centers.filter((_, ci) => same[ci]).map((c) => d2(f, c)));
      sn++;
    }
  }
  const tol2 = Math.max(36, (spread / Math.max(1, sn)) * 4); // ≈ 2 σ, never tighter than ΔE 6
  const isTarget = (i: number) => {
    const f = feat(i);
    if (!same[assign(f)]) return false;
    return Math.min(...centers.filter((_, ci) => same[ci]).map((c) => d2(f, c))) <= tol2;
  };

  // inside the loop: the target material; then grow a little past the line where it continues
  const out = new Uint8Array(w * h);
  const queue: number[] = [];
  for (const i of idx)
    if (isTarget(i)) {
      out[i] = 1;
      queue.push(i);
    }
  const reach = morph(poly, w, h, Math.max(3, Math.round(diag * 0.05)));
  for (let q = 0; q < queue.length; q++) {
    const i = queue[q];
    const x = i % w;
    for (const j of [x > 0 ? i - 1 : -1, x < w - 1 ? i + 1 : -1, i - w, i + w]) {
      if (j < 0 || j >= out.length || out[j] || !reach[j] || bg.floor[j]) continue;
      if (s.G[j] > 8 && !poly[j]) continue; // stop at strong edges outside the drawn line
      if (!isTarget(j)) continue;
      out[j] = 1;
      queue.push(j);
    }
  }
  // tidy: majority smoothing, drop crumbs, fill pinholes
  const r = Math.max(1, Math.round(diag / 900));
  let m = morph(morph(out, w, h, r), w, h, -r); // close
  m = morph(morph(m, w, h, -r), w, h, r); // open
  for (let i = 0; i < m.length; i++) if (bg.floor[i]) m[i] = 0;
  const weight = new Float32Array(w * h);
  for (let i = 0; i < weight.length; i++) weight[i] = poly[i] ? 0.05 + dist[i] / dmax : 0.01;
  m = keepMainComponents(m, weight, w, h);
  let n = 0;
  for (let i = 0; i < m.length; i++) n += m[i];
  return n > 30 ? fillHoles(m, w, h, Math.max(40, Math.round(n * 0.02))) : poly;
}

/** If a selection is essentially one existing part, return exactly that part's mask (clean, known edges). */
export function snapToPart(mask: Uint8Array, labels: Int16Array): { mask: Uint8Array; part: number } {
  const inter = new Map<number, number>();
  const size = new Map<number, number>();
  let n = 0;
  for (let i = 0; i < mask.length; i++) {
    const l = labels[i];
    if (l >= 0) size.set(l, (size.get(l) ?? 0) + 1);
    if (!mask[i]) continue;
    n++;
    if (l >= 0) inter.set(l, (inter.get(l) ?? 0) + 1);
  }
  for (const [l, c] of inter) {
    // almost all of the selection is this part, and it covers most of the part
    if (c / n > 0.9 && c / size.get(l)! > 0.7) {
      const m = new Uint8Array(mask.length);
      for (let i = 0; i < m.length; i++) m[i] = labels[i] === l ? 1 : 0;
      return { mask: m, part: l };
    }
  }
  return { mask, part: -1 };
}
