// ---------------------------------------------------------------- materials
export type MaterialCategory =
  | "fabric"
  | "leather-natural"
  | "leather-artificial"
  | "wood"
  | "metal"
  | "concept";

/** How the shader treats the material. Independent from the catalog category. */
export type RenderKind = "fabric" | "leather" | "wood" | "metal" | "stone" | "chrome" | "glass";

export interface MaterialRender {
  kind: RenderKind;
  /** Strength of photographed specular highlights carried onto this material (0..1). */
  spec: number;
  /** 0 = matte/rough, 1 = mirror-like. Sharpens or softens highlights. */
  gloss: number;
  /** Fabric only: soft brightening of lit areas (fibre scatter). */
  sheen: number;
  /** How much the texture's own micro relief breaks up highlights. */
  relief: number;
  /** 0 = dielectric (white highlights), 1 = metal (highlights tinted by colour). */
  metallic?: number;
  /** Glass only: 0 = fully transparent, 1 = opaque. */
  opacity?: number;
  /** Brushed metals: streak anisotropy. */
  aniso?: number;
}

export interface Material {
  id: string;
  code: string;
  name: string;
  category: MaterialCategory;
  categoryLabel: string;
  finish: string;
  source: string;
  texture: string;
  thumb: string;
  avgColor: string;
  avgLinear: [number, number, number];
  /** Physical size of one texture tile, in centimetres. */
  tileCm: number;
  inCatalog: boolean;
  render: MaterialRender;
}

// ---------------------------------------------------------------- products
export type PartKind = "wood" | "fabric" | "leather" | "metal" | "stone" | "glass" | "other";

/** A region contributes pixels to a part. Regions are applied in order (later wins). */
export type Region =
  | {
      type: "poly";
      points: [number, number][];
      op?: "add" | "sub";
      /** strict: drop every near-white pixel; flood: drop only background connected to the border; none: keep all */
      clip?: "strict" | "flood" | "none";
      minL?: number;
      maxL?: number;
    }
  | { type: "bitmap"; src: string; op?: "add" | "sub" };

export type Mapping =
  | {
      mode: "planar";
      /** grain / weave direction in degrees, image space */
      angle: number;
      /** multiplier on the material's physical tile size */
      scale: number;
      offset?: [number, number];
    }
  | {
      mode: "perspective";
      /** image-space corners of a real rectangle on the surface: TL, TR, BR, BL */
      quad: [number, number][];
      /** real size of that rectangle in cm (width along TL→TR, height along TL→BL) */
      sizeCm: [number, number];
      angle: number;
      scale: number;
      offset?: [number, number];
    };

export interface RenderTuning {
  /** radius (as % of image diagonal) used to strip the old material's pattern from the lighting */
  smoothing: number;
  /** contrast applied to the extracted lighting (1 = as photographed) */
  contrast: number;
  /** multiplier on photographed highlights */
  highlights: number;
  /** amount of the original fine detail (seams, stitching, creases) to keep */
  detail: number;
  /** global exposure correction */
  exposure: number;
}

export interface Part {
  id: string;
  name: string;
  groupId: string;
  kind: PartKind;
  regions: Region[];
  mapping: Mapping;
  /** 3D products: mesh or glTF material names this part covers */
  meshes?: string[];
  tuning?: Partial<RenderTuning>;
}

export interface MaterialGroup {
  id: string;
  name: string;
  /** which library categories this group may use */
  allowed: MaterialCategory[];
  defaultMaterial?: string;
  /** catalog material that is closest to what the photo shows (used for the fidelity test) */
  referenceMaterial?: string;
}

export interface ProductSpec {
  id: string;
  name: string;
  category: string;
  sku?: string;
  /** real overall width of the product in cm: sets the physical scale of textures */
  widthCm: number;
  /** thumbnail / product photo (2D products are customised directly on it) */
  image: string;
  /** 3D products: glTF/GLB model rendered in the studio instead of re-texturing a photo */
  model?: string;
  /** credit line required by the model's licence */
  credit?: string;
  groups: MaterialGroup[];
  parts: Part[];
  notes?: string;
  /** curated material combinations shown as one-click presets */
  presets?: { name: string; selection: Selection }[];
  /** user created (stored in the browser) vs bundled demo */
  origin?: "demo" | "user";
}

/** material id per group */
export type Selection = Record<string, string | undefined>;
export type TuningOverrides = Record<string, Partial<RenderTuning>>;
export type MappingOverrides = Record<string, Partial<{ angle: number; scale: number; offset: [number, number] }>>;

export interface ConfigurationRecord {
  schema: "material-configurator/1";
  productId: string;
  productName: string;
  sku?: string;
  createdAt: string;
  selections: { groupId: string; groupName: string; materialId: string | null; code: string | null; name: string | null; category: string | null }[];
  configCode: string;
}
