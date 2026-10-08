/**
 * "Which catalog finish is this?" — measures the photographed colour of a material group
 * (mid-tones only, so highlights and deep shadows do not bias it) and ranks the allowed
 * catalog finishes by CIE ΔE. Used for the "closest to photo" preset, the fidelity test and
 * as a hint next to each part.
 */
import type { Material, MaterialCategory, MaterialGroup, PartKind } from "../types";
import { srgbToLinear } from "./imageOps";
import type { ProductSession } from "./session";

export function linToLab([r, g, b]: [number, number, number]): [number, number, number] {
  const f = (t: number) => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116);
  const x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047;
  const y = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  const z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883;
  return [116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))];
}

/** Mean linear RGB of the group's pixels whose luminance is between the 20th and 80th percentile. */
export function robustGroupColor(session: ProductSession, groupId: string): [number, number, number] | null {
  const { labels } = session.prepared;
  const d = session.li.pixels.data;
  const Y = session.li.Y;
  const idx: number[] = [];
  session.spec.parts.forEach((p, pi) => {
    if (p.groupId !== groupId) return;
    for (let i = 0; i < labels.length; i++) if (labels[i] === pi && session.prepared.parts[pi].alpha[i] >= 1) idx.push(i);
  });
  if (idx.length < 10) return null;
  const ys = idx.map((i) => Y[i]).sort((a, b) => a - b);
  const lo = ys[Math.floor(ys.length * 0.2)];
  const hi = ys[Math.floor(ys.length * 0.8)];
  let r = 0, g = 0, b = 0, n = 0;
  for (const i of idx) {
    if (Y[i] < lo || Y[i] > hi) continue;
    r += srgbToLinear(d[i * 4]);
    g += srgbToLinear(d[i * 4 + 1]);
    b += srgbToLinear(d[i * 4 + 2]);
    n++;
  }
  return n ? [r / n, g / n, b / n] : null;
}

export interface Match {
  material: Material;
  dE: number;
}

const KIND_CATS: Partial<Record<PartKind, MaterialCategory[]>> = {
  fabric: ["fabric"],
  leather: ["leather-natural", "leather-artificial"],
  wood: ["wood"],
  metal: ["metal"],
};

export function closestFinishes(session: ProductSession, group: MaterialGroup, materials: Material[], limit = 3): Match[] {
  const c = robustGroupColor(session, group.id);
  if (!c) return [];
  const L = linToLab(c);
  // colour alone cannot tell leather from fabric: restrict to the type the part was declared as
  const kinds = new Set(session.spec.parts.filter((p) => p.groupId === group.id).map((p) => p.kind));
  const preferred = [...kinds].flatMap((k) => KIND_CATS[k] ?? []).filter((cat) => group.allowed.includes(cat));
  const cats = preferred.length ? preferred : group.allowed;
  return materials
    .filter((m) => m.inCatalog && cats.includes(m.category))
    .map((m) => {
      const M = linToLab(m.avgLinear);
      return { material: m, dE: Math.hypot(L[0] - M[0], L[1] - M[1], L[2] - M[2]) };
    })
    .sort((a, b) => a.dE - b.dE)
    .slice(0, limit);
}
