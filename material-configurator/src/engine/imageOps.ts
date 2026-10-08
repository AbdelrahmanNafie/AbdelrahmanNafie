/**
 * CPU image utilities: colour conversion, separable box filters, guided filter,
 * background detection and polygon/bitmap rasterisation.
 * Everything works on flat typed arrays (row-major, width w, height h).
 */

export interface Gray {
  w: number;
  h: number;
  data: Float32Array;
}

const SRGB_TO_LIN = new Float32Array(256);
for (let i = 0; i < 256; i++) {
  const c = i / 255;
  SRGB_TO_LIN[i] = c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}
export const srgbToLinear = (v: number) => SRGB_TO_LIN[v];

export function linearToSrgb8(c: number): number {
  const v = c <= 0.0031308 ? c * 12.92 : 1.055 * Math.pow(c, 1 / 2.4) - 0.055;
  return Math.max(0, Math.min(255, Math.round(v * 255)));
}

export async function loadImage(src: string): Promise<HTMLImageElement> {
  const img = new Image();
  img.decoding = "async";
  img.crossOrigin = "anonymous";
  img.src = src;
  await img.decode();
  return img;
}

export function imageToData(img: CanvasImageSource, w: number, h: number): ImageData {
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  const ctx = c.getContext("2d", { willReadFrequently: true })!;
  ctx.drawImage(img, 0, 0, w, h);
  return ctx.getImageData(0, 0, w, h);
}

/** Linear-light luminance (Rec.709). */
export function linearLuminance(id: ImageData): Float32Array {
  const n = id.width * id.height;
  const out = new Float32Array(n);
  const d = id.data;
  for (let i = 0; i < n; i++) {
    out[i] = 0.2126 * SRGB_TO_LIN[d[i * 4]] + 0.7152 * SRGB_TO_LIN[d[i * 4 + 1]] + 0.0722 * SRGB_TO_LIN[d[i * 4 + 2]];
  }
  return out;
}

/** CIE Lab (D65). L in 0..100. */
export function toLab(id: ImageData): { L: Float32Array; A: Float32Array; B: Float32Array } {
  const n = id.width * id.height;
  const L = new Float32Array(n);
  const A = new Float32Array(n);
  const B = new Float32Array(n);
  const d = id.data;
  const f = (t: number) => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116);
  for (let i = 0; i < n; i++) {
    const r = SRGB_TO_LIN[d[i * 4]];
    const g = SRGB_TO_LIN[d[i * 4 + 1]];
    const b = SRGB_TO_LIN[d[i * 4 + 2]];
    const x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047;
    const y = 0.2126 * r + 0.7152 * g + 0.0722 * b;
    const z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883;
    const fx = f(x);
    const fy = f(y);
    const fz = f(z);
    L[i] = 116 * fy - 16;
    A[i] = 500 * (fx - fy);
    B[i] = 200 * (fy - fz);
  }
  return { L, A, B };
}

/** Separable box blur with clamped borders. O(n) regardless of radius. */
export function boxBlur(src: Float32Array, w: number, h: number, r: number): Float32Array {
  if (r <= 0) return src.slice();
  const tmp = new Float32Array(w * h);
  const out = new Float32Array(w * h);
  const win = 2 * r + 1;
  for (let y = 0; y < h; y++) {
    const row = y * w;
    let acc = 0;
    for (let k = -r; k <= r; k++) acc += src[row + Math.min(w - 1, Math.max(0, k))];
    for (let x = 0; x < w; x++) {
      tmp[row + x] = acc / win;
      const add = Math.min(w - 1, x + r + 1);
      const rem = Math.max(0, x - r);
      acc += src[row + add] - src[row + rem];
    }
  }
  for (let x = 0; x < w; x++) {
    let acc = 0;
    for (let k = -r; k <= r; k++) acc += tmp[Math.min(h - 1, Math.max(0, k)) * w + x];
    for (let y = 0; y < h; y++) {
      out[y * w + x] = acc / win;
      const add = Math.min(h - 1, y + r + 1);
      const rem = Math.max(0, y - r);
      acc += tmp[add * w + x] - tmp[rem * w + x];
    }
  }
  return out;
}

/**
 * Normalised convolution: blur `v` using only pixels where mask > 0, so neighbouring
 * parts (or the white background) never leak into the estimate. Two box passes ≈ Gaussian.
 */
export function maskedBlur(v: Float32Array, mask: Float32Array, w: number, h: number, r: number): Float32Array {
  const n = w * h;
  const vm = new Float32Array(n);
  for (let i = 0; i < n; i++) vm[i] = v[i] * mask[i];
  const r1 = Math.max(1, Math.round(r * 0.75));
  const a = boxBlur(boxBlur(vm, w, h, r1), w, h, r1);
  const b = boxBlur(boxBlur(mask, w, h, r1), w, h, r1);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) out[i] = b[i] > 1e-4 ? a[i] / b[i] : v[i];
  return out;
}

/** He et al. guided filter (gray guide). Used to snap mask edges to image edges. */
export function guidedFilter(I: Float32Array, p: Float32Array, w: number, h: number, r: number, eps: number): Float32Array {
  const n = w * h;
  const Ip = new Float32Array(n);
  const II = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    Ip[i] = I[i] * p[i];
    II[i] = I[i] * I[i];
  }
  const mI = boxBlur(I, w, h, r);
  const mp = boxBlur(p, w, h, r);
  const mIp = boxBlur(Ip, w, h, r);
  const mII = boxBlur(II, w, h, r);
  const a = new Float32Array(n);
  const b = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const cov = mIp[i] - mI[i] * mp[i];
    const v = mII[i] - mI[i] * mI[i];
    a[i] = cov / (v + eps);
    b[i] = mp[i] - a[i] * mI[i];
  }
  const ma = boxBlur(a, w, h, r);
  const mb = boxBlur(b, w, h, r);
  const q = new Float32Array(n);
  for (let i = 0; i < n; i++) q[i] = ma[i] * I[i] + mb[i];
  return q;
}

export function dilate(mask: Uint8Array, w: number, h: number, r: number): Uint8Array {
  const f = new Float32Array(mask.length);
  for (let i = 0; i < mask.length; i++) f[i] = mask[i];
  const b = boxBlur(f, w, h, r);
  const out = new Uint8Array(mask.length);
  for (let i = 0; i < mask.length; i++) out[i] = b[i] > 1e-3 ? 1 : 0;
  return out;
}

export interface BackgroundInfo {
  /** 1 where the pixel is near-white (anywhere in the image) */
  nearWhite: Uint8Array;
  /** 1 where the pixel belongs to the backdrop connected to the image border */
  backdrop: Uint8Array;
  /** backdrop plus the soft, neutral floor shadow connected to it (never product) */
  floor: Uint8Array;
}

/**
 * Studio-photo background detection: near-white, low-chroma pixels, and the subset
 * connected to the image border (so white parts *inside* the product survive).
 */
export function detectBackground(lab: { L: Float32Array; A: Float32Array; B: Float32Array }, w: number, h: number, minL = 90): BackgroundInfo {
  const n = w * h;
  const nearWhite = new Uint8Array(n);
  for (let i = 0; i < n; i++) {
    const c = Math.hypot(lab.A[i], lab.B[i]);
    nearWhite[i] = lab.L[i] > minL && c < 8 ? 1 : 0;
  }
  // The backdrop level is estimated from the image border (median). Backdrop pixels must be light AND
  // nearly neutral: light-but-tinted flecks of the product (cream yarns, beige leather glints) stay product.
  const border: number[] = [];
  for (let x = 0; x < w; x += 2) border.push(lab.L[x], lab.L[(h - 1) * w + x]);
  for (let y = 0; y < h; y += 2) border.push(lab.L[y * w], lab.L[y * w + w - 1]);
  border.sort((a, b) => a - b);
  const level = border[border.length >> 1];
  const isBackdrop = new Uint8Array(n);
  for (let i = 0; i < n; i++) isBackdrop[i] = lab.L[i] > Math.min(level - 10, minL) && Math.hypot(lab.A[i], lab.B[i]) < 4.5 ? 1 : 0;
  const backdrop = new Uint8Array(n);
  const stack: number[] = [];
  const seed = (i: number) => {
    if (isBackdrop[i] && !backdrop[i]) {
      backdrop[i] = 1;
      stack.push(i);
    }
  };
  for (let x = 0; x < w; x++) {
    seed(x);
    seed((h - 1) * w + x);
  }
  for (let y = 0; y < h; y++) {
    seed(y * w);
    seed(y * w + w - 1);
  }
  while (stack.length) {
    const i = stack.pop()!;
    const x = i % w;
    const y = (i / w) | 0;
    if (x > 0) seed(i - 1);
    if (x < w - 1) seed(i + 1);
    if (y > 0) seed(i - w);
    if (y < h - 1) seed(i + w);
  }
  // grow the backdrop through soft, neutral-grey shadow pixels
  const floor = backdrop.slice();
  const q: number[] = [];
  for (let i = 0; i < n; i++) if (backdrop[i]) q.push(i);
  while (q.length) {
    const i = q.pop()!;
    const x = i % w;
    for (const j of [x > 0 ? i - 1 : -1, x < w - 1 ? i + 1 : -1, i - w, i + w]) {
      if (j < 0 || j >= n || floor[j]) continue;
      // shadows are light, weakly tinted and smooth (no yarn/grain texture)
      const jx = j % w;
      const gx = jx > 0 && jx < w - 1 ? Math.abs(lab.L[j + 1] - lab.L[j - 1]) : 0;
      const gy = j >= w && j < n - w ? Math.abs(lab.L[j + w] - lab.L[j - w]) : 0;
      if (lab.L[j] > 60 && Math.hypot(lab.A[j], lab.B[j]) < 10 && gx + gy < 2.5) {
        floor[j] = 1;
        q.push(j);
      }
    }
  }
  return { nearWhite, backdrop, floor };
}

export function rasterizePolygon(points: [number, number][], w: number, h: number): Uint8Array {
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  const ctx = c.getContext("2d", { willReadFrequently: true })!;
  ctx.fillStyle = "#fff";
  ctx.beginPath();
  points.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.closePath();
  ctx.fill();
  const d = ctx.getImageData(0, 0, w, h).data;
  const out = new Uint8Array(w * h);
  for (let i = 0; i < out.length; i++) out[i] = d[i * 4] > 127 ? 1 : 0;
  return out;
}

export async function decodeBitmapMask(src: string, w: number, h: number): Promise<Uint8Array> {
  const img = await loadImage(src);
  const d = imageToData(img, w, h).data;
  const out = new Uint8Array(w * h);
  for (let i = 0; i < out.length; i++) out[i] = d[i * 4 + 3] > 127 && d[i * 4] > 127 ? 1 : 0;
  return out;
}

export function encodeBitmapMask(mask: Uint8Array, w: number, h: number): string {
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  const ctx = c.getContext("2d")!;
  const id = ctx.createImageData(w, h);
  for (let i = 0; i < mask.length; i++) {
    const v = mask[i] ? 255 : 0;
    id.data[i * 4] = v;
    id.data[i * 4 + 1] = v;
    id.data[i * 4 + 2] = v;
    id.data[i * 4 + 3] = v;
  }
  ctx.putImageData(id, 0, 0);
  return c.toDataURL("image/png");
}

export function bbox(mask: Uint8Array | Float32Array, w: number, h: number, pad = 0) {
  let x0 = w, y0 = h, x1 = -1, y1 = -1;
  for (let y = 0; y < h; y++)
    for (let x = 0; x < w; x++)
      if (mask[y * w + x] > 0) {
        if (x < x0) x0 = x;
        if (x > x1) x1 = x;
        if (y < y0) y0 = y;
        if (y > y1) y1 = y;
      }
  if (x1 < 0) return null;
  return {
    x0: Math.max(0, x0 - pad),
    y0: Math.max(0, y0 - pad),
    x1: Math.min(w - 1, x1 + pad),
    y1: Math.min(h - 1, y1 + pad),
  };
}
