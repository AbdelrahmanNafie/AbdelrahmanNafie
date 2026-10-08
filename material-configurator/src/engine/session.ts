/**
 * A ProductSession owns the CPU-side state of the product being configured or edited:
 * the decoded photo, the label map and the prepared parts. It is renderer-agnostic;
 * the Stage turns it into GPU draws.
 */
import type { Mapping, Material, MaterialCategory, MaterialGroup, Part, PartKind, ProductSpec, RenderTuning, Selection } from "../types";
import { encodeBitmapMask } from "./imageOps";
import { mappingMatrix, type Mat3 } from "./mapping";
import {
  assemble,
  buildLabels,
  computeShade,
  hardMasksFromLabels,
  loadProductImage,
  preparePart,
  productBox,
  tuningFor,
  type LoadedImage,
  type PreparedProduct,
} from "./prepare";

export const ALLOWED_BY_KIND: Record<PartKind, MaterialCategory[]> = {
  wood: ["wood", "concept"],
  fabric: ["fabric", "leather-natural", "leather-artificial"],
  leather: ["leather-natural", "leather-artificial", "fabric"],
  metal: ["metal", "concept"],
  stone: ["concept", "wood"],
  glass: ["concept"],
  other: ["fabric", "leather-natural", "leather-artificial", "wood", "metal", "concept"],
};

export interface DrawItem {
  partIndex: number;
  material: Material;
  map: Mat3;
  meanShade: number;
  tuning: RenderTuning;
}

export class ProductSession {
  spec: ProductSpec;
  li: LoadedImage;
  prepared: PreparedProduct;
  /** bumps whenever masks/lighting change so views can resync */
  version = 0;

  private constructor(spec: ProductSpec, li: LoadedImage, prepared: PreparedProduct) {
    this.spec = spec;
    this.li = li;
    this.prepared = prepared;
  }

  static async open(spec: ProductSpec, tuning?: Record<string, Partial<RenderTuning>>) {
    const li = await loadProductImage(spec);
    const { labels, hard } = await buildLabels(spec, li);
    return new ProductSession(spec, li, assemble(spec, li, labels, hard, tuning));
  }

  get w() {
    return this.li.w;
  }
  get h() {
    return this.li.h;
  }

  partAt(x: number, y: number): number {
    const xi = Math.floor(x);
    const yi = Math.floor(y);
    if (xi < 0 || yi < 0 || xi >= this.w || yi >= this.h) return -1;
    return this.prepared.labels[yi * this.w + xi];
  }

  /** Re-extract lighting for one part with a new pattern-removal radius. */
  setSmoothing(partIndex: number, pct: number) {
    const p = this.prepared.parts[partIndex];
    if (!p || Math.abs(p.smoothingUsed - pct) < 1e-6) return;
    const { shade, meanShade } = computeShade(this.li.Y, p.hard, this.w, this.h, pct);
    this.prepared.parts[partIndex] = { ...p, shade, meanShade, smoothingUsed: pct };
    this.version++;
  }

  /** Replace the label map (editor) and rebuild the parts whose pixels changed. */
  setLabels(labels: Int16Array, changed?: Set<number>, tuning?: Record<string, Partial<RenderTuning>>) {
    const hard = hardMasksFromLabels(labels, this.spec.parts.length);
    const parts = this.spec.parts.map((p, i) => {
      const old = this.prepared.parts[i];
      if (old && changed && !changed.has(i)) return old;
      return preparePart(this.li, i, hard[i], tuningFor(this.spec, i, tuning?.[p.id]).smoothing);
    });
    const pb = productBox(this.li, labels);
    this.prepared = { ...this.prepared, spec: this.spec, labels, parts, productBox: pb, pxPerCm: (pb.x1 - pb.x0 + 1) / Math.max(1, this.spec.widthCm) };
    this.version++;
  }

  setSpec(spec: ProductSpec) {
    this.spec = spec;
    this.prepared = {
      ...this.prepared,
      spec,
      pxPerCm: (this.prepared.productBox.x1 - this.prepared.productBox.x0 + 1) / Math.max(1, spec.widthCm),
    };
    this.version++;
  }

  /** Lighting normaliser shared by every part of a group, so relative face brightness survives. */
  groupMeanShade(groupId: string) {
    let s = 0;
    let a = 0;
    this.spec.parts.forEach((p, i) => {
      if (p.groupId !== groupId) return;
      const pp = this.prepared.parts[i];
      s += pp.meanShade * pp.area;
      a += pp.area;
    });
    return a ? s / a : 0.18;
  }

  drawList(
    selection: Selection,
    materials: Map<string, Material>,
    tuning: Record<string, Partial<RenderTuning>>,
    mapping: Record<string, Partial<Mapping>>,
  ): DrawItem[] {
    const out: DrawItem[] = [];
    const means = new Map<string, number>();
    this.spec.parts.forEach((part, i) => {
      const mid = selection[part.groupId];
      const m = mid ? materials.get(mid) : undefined;
      if (!m || !this.prepared.parts[i]?.area) return;
      if (!means.has(part.groupId)) means.set(part.groupId, this.groupMeanShade(part.groupId));
      const mp = { ...part.mapping, ...(mapping[part.id] ?? {}) } as Mapping;
      const meanShade = means.get(part.groupId)!;
      const t = tuningFor(this.spec, i, tuning[part.id]);
      // Cameras compress the shading of very light objects (tone curve near white). When a light
      // original (white boucle, pale oak) becomes a darker finish, restore that lost depth.
      const toneRestore = 1 + Math.min(0.6, Math.max(0, (meanShade - 0.3) * 1.2));
      out.push({
        partIndex: i,
        material: m,
        map: mappingMatrix(mp, this.prepared.pxPerCm, m.tileCm),
        meanShade,
        tuning: { ...t, contrast: t.contrast * toneRestore },
      });
    });
    return out;
  }

  /** Serialise the current masks as bitmap regions (used when saving an edited product). */
  toSpecWithBitmaps(): ProductSpec {
    const hard = hardMasksFromLabels(this.prepared.labels, this.spec.parts.length);
    return {
      ...this.spec,
      parts: this.spec.parts.map((p, i) => ({ ...p, regions: [{ type: "bitmap", src: encodeBitmapMask(hard[i], this.w, this.h) }] })),
    };
  }
}

export function newPart(spec: ProductSpec, kind: PartKind, name: string, group?: MaterialGroup): { part: Part; group: MaterialGroup | null } {
  const id = `${kind}-${Math.random().toString(36).slice(2, 7)}`;
  const g: MaterialGroup | null = group
    ? null
    : { id: `g-${id}`, name, allowed: ALLOWED_BY_KIND[kind] };
  const part: Part = {
    id,
    name,
    groupId: group?.id ?? g!.id,
    kind,
    regions: [],
    mapping: { mode: "planar", angle: kind === "metal" ? 90 : 0, scale: 1 },
  };
  void spec;
  return { part, group: g };
}
