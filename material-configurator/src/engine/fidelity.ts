/**
 * Fidelity test: render each group with the catalog finish closest to what the photo shows,
 * then measure the colour difference (CIE76 ΔE in Lab) against the original photo.
 *  ΔE < 2  : not perceptible at a glance
 *  ΔE 2–5  : perceptible on close inspection
 *  ΔE 5–10 : clearly visible difference
 * The residual mixes engine error with the real difference between the catalog swatch and the
 * finish on the photographed product (and different grain/weave), so it is an upper bound on
 * engine error. The lighting correlation isolates what the engine is responsible for: how well
 * the shape, shadows and highlights of the photo survive the material swap (1.0 = perfect).
 */
import { linearLuminance, maskedBlur, toLab } from "./imageOps";
import type { ProductSession } from "./session";

export interface FidelityResult {
  heat: ImageData;
  groups: { groupId: string; name: string; meanDE: number; p95DE: number; pixels: number; reference: string | null; lightingR: number }[];
  overall: number;
}

export function measureFidelity(session: ProductSession, rendered: Uint8ClampedArray, refs: Record<string, string | null>): FidelityResult {
  const { w, h } = session;
  const orig = session.li.lab;
  const ren = toLab(new ImageData(new Uint8ClampedArray(rendered), w, h));
  const heat = new ImageData(w, h);
  const per = new Map<string, number[]>();
  const labels = session.prepared.labels;
  for (let i = 0; i < w * h; i++) {
    const l = labels[i];
    if (l < 0) continue;
    const g = session.spec.parts[l].groupId;
    if (!refs[g]) continue;
    const dE = Math.hypot(orig.L[i] - ren.L[i], orig.A[i] - ren.A[i], orig.B[i] - ren.B[i]);
    let arr = per.get(g);
    if (!arr) per.set(g, (arr = []));
    arr.push(dE);
    // green (good) → yellow → red (bad), saturating at ΔE 15
    const t = Math.min(1, dE / 15);
    heat.data[i * 4] = Math.round(255 * Math.min(1, t * 2));
    heat.data[i * 4 + 1] = Math.round(255 * Math.min(1, 2 - t * 2));
    heat.data[i * 4 + 2] = 40;
    heat.data[i * 4 + 3] = 190;
  }
  // Lighting preservation: correlation of the (texture-free) luminance structure of render vs photo.
  const Yr = linearLuminance(new ImageData(new Uint8ClampedArray(rendered), w, h));
  const Yo = session.li.Y;
  let tot = 0, n = 0;
  const groups = session.spec.groups.map((g) => {
    const arr = (per.get(g.id) ?? []).sort((a, b) => a - b);
    const mean = arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : NaN;
    tot += arr.reduce((a, b) => a + b, 0);
    n += arr.length;
    let lightingR = NaN;
    if (arr.length) {
      const m = new Float32Array(w * h);
      for (let i = 0; i < m.length; i++) if (labels[i] >= 0 && session.spec.parts[labels[i]].groupId === g.id) m[i] = 1;
      const r = Math.max(2, Math.round(Math.hypot(w, h) / 120));
      const a = maskedBlur(Yo, m, w, h, r);
      const b = maskedBlur(Yr, m, w, h, r);
      let sa = 0, sb = 0, c = 0;
      for (let i = 0; i < m.length; i++) if (m[i]) { sa += Math.log(a[i] + 1e-3); sb += Math.log(b[i] + 1e-3); c++; }
      const ma = sa / c, mb = sb / c;
      let cov = 0, va = 0, vb = 0;
      for (let i = 0; i < m.length; i++)
        if (m[i]) {
          const da = Math.log(a[i] + 1e-3) - ma, db = Math.log(b[i] + 1e-3) - mb;
          cov += da * db; va += da * da; vb += db * db;
        }
      lightingR = cov / Math.sqrt(va * vb + 1e-12);
    }
    return { groupId: g.id, name: g.name, meanDE: mean, p95DE: arr.length ? arr[Math.floor(arr.length * 0.95)] : NaN, pixels: arr.length, reference: refs[g.id] ?? null, lightingR };
  });
  return { heat, groups, overall: n ? tot / n : NaN };
}
