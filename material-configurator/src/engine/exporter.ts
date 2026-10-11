/**
 * Export pipeline: render off-screen at the requested resolution, clean the backdrop,
 * optionally crop to a padded square (catalog / Excel cell friendly), then encode.
 * The configuration (all material codes) is embedded in the PNG as an iTXt chunk so an
 * exported image can be dropped back into the app to restore the exact configuration.
 */
import type { ConfigurationRecord, Material, ProductSpec, Selection } from "../types";

export const PNG_KEYWORD = "material-configuration";

export interface ExportOptions {
  scale: number; // render scale relative to the source photo
  framing: "original" | "square";
  /** square output edge in px (only for framing = square) */
  squareSize?: number;
  margin: number; // fraction of the product box
  whiten: boolean;
  format: "png" | "jpeg";
}

export function buildRecord(spec: ProductSpec, selection: Selection, materials: Map<string, Material>): ConfigurationRecord {
  const selections = spec.groups.map((g) => {
    const m = selection[g.id] ? materials.get(selection[g.id]!) : undefined;
    return {
      groupId: g.id,
      groupName: g.name,
      materialId: m?.id ?? null,
      code: m?.code ?? null,
      name: m?.name ?? null,
      category: m?.categoryLabel ?? null,
    };
  });
  const configCode =
    (spec.sku ?? spec.id) +
    " | " +
    selections.map((s) => `${s.groupName}: ${s.code ? `${s.code} ${s.name}` : "Original"}`).join(" | ");
  return {
    schema: "material-configurator/1",
    productId: spec.id,
    productName: spec.name,
    sku: spec.sku,
    createdAt: new Date().toISOString(),
    selections,
    configCode,
  };
}

export function fileStem(spec: ProductSpec, rec: ConfigurationRecord) {
  const parts = rec.selections.map((s) => `${s.groupId}-${(s.code ?? "orig").replace(/[^A-Za-z0-9-]/g, "")}`);
  return `${spec.sku ?? spec.id}__${parts.join("_")}`;
}

/** Push the studio backdrop to pure white while keeping the product's soft floor shadow. */
export function whitenBackdrop(px: Uint8ClampedArray, keep: (i: number) => boolean) {
  for (let i = 0; i < px.length / 4; i++) {
    if (keep(i)) continue;
    const r = px[i * 4], g = px[i * 4 + 1], b = px[i * 4 + 2];
    const mx = Math.max(r, g, b);
    const mn = Math.min(r, g, b);
    if (mx - mn > 18) continue; // coloured pixel: leave it
    // levels: 235..255 → 255 with a smooth ramp from 215, so shadows fade naturally
    const lift = Math.min(1, Math.max(0, (mn - 215) / 22));
    if (lift <= 0) continue;
    px[i * 4] = r + (255 - r) * lift;
    px[i * 4 + 1] = g + (255 - g) * lift;
    px[i * 4 + 2] = b + (255 - b) * lift;
  }
}

export function pixelsToCanvas(px: Uint8ClampedArray, w: number, h: number): HTMLCanvasElement {
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  c.getContext("2d")!.putImageData(new ImageData(px, w, h), 0, 0);
  return c;
}

export function frameSquare(src: HTMLCanvasElement, box: { x0: number; y0: number; x1: number; y1: number }, scale: number, margin: number, size: number) {
  const bw = (box.x1 - box.x0 + 1) * scale;
  const bh = (box.y1 - box.y0 + 1) * scale;
  const side = Math.max(bw, bh) * (1 + margin * 2);
  const cx = ((box.x0 + box.x1 + 1) / 2) * scale;
  const cy = ((box.y0 + box.y1 + 1) / 2) * scale;
  const out = document.createElement("canvas");
  out.width = size;
  out.height = size;
  const ctx = out.getContext("2d")!;
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, size, size);
  ctx.imageSmoothingQuality = "high";
  const k = size / side;
  ctx.drawImage(src, (size - src.width * k) / 2 + (src.width / 2 - cx) * k, (size - src.height * k) / 2 + (src.height / 2 - cy) * k, src.width * k, src.height * k);
  return out;
}

// --------------------------------------------------------------- PNG iTXt
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();
function crc32(bytes: Uint8Array) {
  let c = 0xffffffff;
  for (const b of bytes) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

export async function pngWithText(blob: Blob, keyword: string, text: string): Promise<Blob> {
  const buf = new Uint8Array(await blob.arrayBuffer());
  const enc = new TextEncoder();
  const data = new Uint8Array([...enc.encode(keyword), 0, 0, 0, 0, 0, ...enc.encode(text)]);
  const type = enc.encode("iTXt");
  const chunk = new Uint8Array(12 + data.length);
  const dv = new DataView(chunk.buffer);
  dv.setUint32(0, data.length);
  chunk.set(type, 4);
  chunk.set(data, 8);
  dv.setUint32(8 + data.length, crc32(chunk.subarray(4, 8 + data.length)));
  // insert right after IHDR (8-byte signature + 25-byte IHDR chunk)
  const at = 8 + 25;
  const out = new Uint8Array(buf.length + chunk.length);
  out.set(buf.subarray(0, at), 0);
  out.set(chunk, at);
  out.set(buf.subarray(at), at + chunk.length);
  return new Blob([out], { type: "image/png" });
}

export async function readPngText(file: Blob, keyword: string): Promise<string | null> {
  const buf = new Uint8Array(await file.arrayBuffer());
  if (buf[0] !== 0x89 || buf[1] !== 0x50) return null;
  const dv = new DataView(buf.buffer);
  const dec = new TextDecoder();
  let p = 8;
  while (p + 8 <= buf.length) {
    const len = dv.getUint32(p);
    const type = dec.decode(buf.subarray(p + 4, p + 8));
    if (type === "iTXt" || type === "tEXt") {
      const body = buf.subarray(p + 8, p + 8 + len);
      const z = body.indexOf(0);
      if (dec.decode(body.subarray(0, z)) === keyword) {
        if (type === "tEXt") return dec.decode(body.subarray(z + 1));
        // iTXt: keyword\0 flag method lang\0 translated\0 text
        let q = z + 3;
        q = body.indexOf(0, q) + 1;
        q = body.indexOf(0, q) + 1;
        return dec.decode(body.subarray(q));
      }
    }
    if (type === "IEND") break;
    p += 12 + len;
  }
  return null;
}

export function canvasToBlob(c: HTMLCanvasElement, type: string, q?: number): Promise<Blob> {
  return new Promise((res, rej) => c.toBlob((b) => (b ? res(b) : rej(new Error("encode failed"))), type, q));
}

type ClaudeHost = { use?: (name: string) => Promise<{ save: (r: { filename: string; data: Blob }) => Promise<unknown> } | null> };

/**
 * Save a file. Inside a claude.ai Artifact the frame cannot download directly, so the
 * platform's `downloads` capability is used (viewer confirms); elsewhere a normal link click.
 */
export async function download(blob: Blob, name: string) {
  const host = (window as unknown as { claude?: ClaudeHost }).claude;
  if (host?.use) {
    try {
      const d = await host.use("downloads");
      if (d) {
        await d.save({ filename: name, data: blob });
        return;
      }
    } catch {
      return; // declined or unavailable: the viewer already saw the platform prompt
    }
  }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
