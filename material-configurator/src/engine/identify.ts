/**
 * "What is this material?" — analyses an area the user marked on the photo:
 * material type (from colour, texture, grain and thickness), its measured colour, the closest
 * Mobica finishes of that type, and which configured part(s) the area belongs to.
 */
import type { Material, MaterialCategory, PartKind } from "../types";
import { srgbToLinear } from "./imageOps";
import { linToLab } from "./match";
import { guessKind } from "./segment";
import type { ProductSession } from "./session";

export interface Identification {
  mask: Uint8Array;
  area: number;
  kind: PartKind;
  /** measured colour of the area (mid-tones only), as CSS hex */
  colorHex: string;
  matches: { material: Material; dE: number; label: string }[];
  /** configured parts covering the area, largest share first */
  parts: { partIndex: number; groupId: string; share: number }[];
}

const CATS: Record<PartKind, MaterialCategory[]> = {
  fabric: ["fabric"],
  leather: ["leather-natural", "leather-artificial"],
  wood: ["wood"],
  metal: ["metal"],
  // stone and glass are not in the Mobica catalog: only the demo "special" finishes exist
  stone: ["concept"],
  glass: ["concept"],
  other: ["fabric", "leather-natural", "leather-artificial", "wood", "metal"],
};

export const KIND_LABEL: Record<PartKind, string> = { fabric: "Fabric", leather: "Leather", wood: "Wood", metal: "Metal", stone: "Stone", glass: "Glass", other: "Not sure" };

/** How a colour difference reads to a client (CIE76 ΔE). */
export function matchLabel(dE: number) {
  return dE < 5 ? "Very close" : dE < 10 ? "Close" : dE < 18 ? "Similar" : "Nearest available";
}

function linToHex([r, g, b]: [number, number, number]) {
  const s = (v: number) => {
    const c = v <= 0.0031308 ? 12.92 * v : 1.055 * Math.pow(v, 1 / 2.4) - 0.055;
    return Math.round(Math.min(1, Math.max(0, c)) * 255).toString(16).padStart(2, "0");
  };
  return `#${s(r)}${s(g)}${s(b)}`;
}

export function identifyArea(session: ProductSession, mask: Uint8Array, materials: Material[], kindOverride?: PartKind): Identification | null {
  const { labels } = session.prepared;
  const d = session.li.pixels.data;
  const Y = session.li.Y;
  const idx: number[] = [];
  const per = new Map<number, number>();
  for (let i = 0; i < mask.length; i++) {
    if (!mask[i]) continue;
    idx.push(i);
    if (labels[i] >= 0) per.set(labels[i], (per.get(labels[i]) ?? 0) + 1);
  }
  if (idx.length < 30) return null;
  // mid-tones only: highlights and deep shadows say little about the finish itself
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
  const lin: [number, number, number] = n ? [r / n, g / n, b / n] : [0.5, 0.5, 0.5];
  const kind = kindOverride ?? guessKind(session.li, mask);
  const L = linToLab(lin);
  const matches = materials
    .filter((m) => (m.inCatalog || m.category === "concept") && CATS[kind].includes(m.category))
    .map((m) => {
      const M = linToLab(m.avgLinear);
      return { material: m, dE: Math.hypot(L[0] - M[0], L[1] - M[1], L[2] - M[2]) };
    })
    .sort((a, b) => a.dE - b.dE)
    .slice(0, 4)
    .map((m) => ({ ...m, label: matchLabel(m.dE) }));
  const parts = [...per.entries()]
    .map(([pi, c]) => ({ partIndex: pi, groupId: session.spec.parts[pi].groupId, share: c / idx.length }))
    .filter((p) => p.share > 0.03)
    .sort((a, b) => b.share - a.share);
  return { mask, area: idx.length, kind, colorHex: linToHex(lin), matches, parts };
}
