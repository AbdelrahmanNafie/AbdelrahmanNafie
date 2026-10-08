/** Data access: bundled demo catalog (static files) + user products kept in IndexedDB. */
import type { Material, ProductSpec } from "../types";

export async function loadMaterials(): Promise<Material[]> {
  const r = await fetch("materials/materials.json");
  if (!r.ok) throw new Error("Could not load the material library");
  return (await r.json()).materials;
}

export async function loadDemoProducts(): Promise<ProductSpec[]> {
  const idx = await (await fetch("products/index.json")).json();
  return Promise.all((idx.products as string[]).map(async (u) => (await fetch(u)).json()));
}

const DB = "material-configurator";
const STORE = "products";

function db(): Promise<IDBDatabase> {
  return new Promise((res, rej) => {
    const r = indexedDB.open(DB, 1);
    r.onupgradeneeded = () => r.result.createObjectStore(STORE, { keyPath: "id" });
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
}

export async function loadUserProducts(): Promise<ProductSpec[]> {
  try {
    const d = await db();
    return await new Promise((res, rej) => {
      const q = d.transaction(STORE).objectStore(STORE).getAll();
      q.onsuccess = () => res(q.result as ProductSpec[]);
      q.onerror = () => rej(q.error);
    });
  } catch {
    return [];
  }
}

export async function saveUserProduct(p: ProductSpec) {
  const d = await db();
  await new Promise<void>((res, rej) => {
    const tx = d.transaction(STORE, "readwrite");
    tx.objectStore(STORE).put({ ...p, origin: "user" });
    tx.oncomplete = () => res();
    tx.onerror = () => rej(tx.error);
  });
}

export async function deleteUserProduct(id: string) {
  const d = await db();
  await new Promise<void>((res, rej) => {
    const tx = d.transaction(STORE, "readwrite");
    tx.objectStore(STORE).delete(id);
    tx.oncomplete = () => res();
    tx.onerror = () => rej(tx.error);
  });
}

export function fileToDataUrl(f: Blob): Promise<string> {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(r.result as string);
    r.onerror = () => rej(r.error);
    r.readAsDataURL(f);
  });
}

/** Downscale very large uploads so the browser pipeline stays interactive. */
export async function normaliseUpload(f: File, maxEdge = 2400): Promise<string> {
  const url = await fileToDataUrl(f);
  const img = new Image();
  img.src = url;
  await img.decode();
  const k = Math.min(1, maxEdge / Math.max(img.naturalWidth, img.naturalHeight));
  const c = document.createElement("canvas");
  c.width = Math.round(img.naturalWidth * k);
  c.height = Math.round(img.naturalHeight * k);
  const ctx = c.getContext("2d")!;
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, c.width, c.height);
  ctx.drawImage(img, 0, 0, c.width, c.height);
  return c.toDataURL("image/jpeg", 0.95);
}
