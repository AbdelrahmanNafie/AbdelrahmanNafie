import { useEffect, useState } from "react";
import type { ConfigurationRecord, ProductSpec } from "../types";
import { PNG_KEYWORD, canvasToBlob, download, fileStem, frameSquare, pngWithText, pixelsToCanvas, whitenBackdrop, type ExportOptions } from "../engine/exporter";

interface Props {
  spec: ProductSpec;
  record: ConfigurationRecord;
  srcSize: [number, number];
  productBox: { x0: number; y0: number; x1: number; y1: number };
  /** renders the current configuration at `scale` and returns RGBA pixels + a backdrop test */
  render: (scale: number) => Promise<{ px: Uint8ClampedArray; w: number; h: number; isProduct: (i: number) => boolean }>;
  onClose: () => void;
}

type Preset = "orig" | "x2" | "x3" | "square1200" | "excel400";
const PRESETS: Record<Preset, { label: string; sub: string; opts: Omit<ExportOptions, "whiten" | "format"> }> = {
  orig: { label: "Original framing", sub: "1× source resolution", opts: { scale: 1, framing: "original", margin: 0 } },
  x2: { label: "High resolution", sub: "2× re-rendered", opts: { scale: 2, framing: "original", margin: 0 } },
  x3: { label: "Print", sub: "3× re-rendered", opts: { scale: 3, framing: "original", margin: 0 } },
  square1200: { label: "Catalog square", sub: "1200 × 1200, centred", opts: { scale: 3, framing: "square", squareSize: 1200, margin: 0.08 } },
  excel400: { label: "Quotation / Excel", sub: "400 × 400, light file", opts: { scale: 2, framing: "square", squareSize: 400, margin: 0.06 } },
};

export function ExportDialog(p: Props) {
  const [preset, setPreset] = useState<Preset>("square1200");
  const [whiten, setWhiten] = useState(true);
  const [format, setFormat] = useState<"png" | "jpeg">("png");
  const [preview, setPreview] = useState<{ url: string; w: number; h: number; bytes: number; blob: Blob } | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setBusy(true);
      const o = PRESETS[preset].opts;
      const { px, w, h, isProduct } = await p.render(o.scale);
      if (whiten) whitenBackdrop(px, isProduct);
      let c = pixelsToCanvas(px, w, h);
      if (o.framing === "square") c = frameSquare(c, p.productBox, o.scale, o.margin, o.squareSize!);
      let blob = await canvasToBlob(c, format === "png" ? "image/png" : "image/jpeg", 0.93);
      if (format === "png") blob = await pngWithText(blob, PNG_KEYWORD, JSON.stringify(p.record));
      if (cancelled) return;
      setPreview((old) => {
        if (old) URL.revokeObjectURL(old.url);
        return { url: URL.createObjectURL(blob), w: c.width, h: c.height, bytes: blob.size, blob };
      });
      setBusy(false);
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preset, whiten, format]);

  const stem = fileStem(p.spec, p.record);
  const tsv = [p.spec.sku ?? p.spec.id, p.spec.name, ...p.record.selections.map((s) => `${s.groupName}: ${s.code ?? "Original"}${s.name ? ` ${s.name}` : ""}`), `${stem}.${format === "png" ? "png" : "jpg"}`].join("\t");
  const copy = async (text: string, what: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(what);
      setTimeout(() => setCopied(null), 1500);
    } catch {
      setCopied("Clipboard blocked");
    }
  };

  const scaleWarn = PRESETS[preset].opts.scale > 1;
  return (
    <div className="modal-back" onClick={p.onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Export image">
        <header>
          <h2>Export configured image</h2>
          <button className="icon-btn" onClick={p.onClose} aria-label="Close">
            ×
          </button>
        </header>
        <div className="export-body">
          <div className="export-preview checker">
            {preview && <img src={preview.url} alt="Export preview" data-testid="export-preview" />}
            {busy && <div className="busy">Rendering…</div>}
          </div>
          <div className="export-opts">
            <div className="eyebrow">Size & framing</div>
            <div className="opt-list">
              {(Object.keys(PRESETS) as Preset[]).map((k) => (
                <button key={k} className={`opt ${preset === k ? "on" : ""}`} onClick={() => setPreset(k)}>
                  <b>{PRESETS[k].label}</b>
                  <small>{PRESETS[k].sub}</small>
                </button>
              ))}
            </div>
            <label className="check">
              <input type="checkbox" checked={whiten} onChange={(e) => setWhiten(e.target.checked)} /> Pure white background (keeps the soft floor shadow)
            </label>
            <div className="row gap">
              <button className={`pill ${format === "png" ? "on" : ""}`} onClick={() => setFormat("png")}>
                PNG + embedded codes
              </button>
              <button className={`pill ${format === "jpeg" ? "on" : ""}`} onClick={() => setFormat("jpeg")}>
                JPG (smaller)
              </button>
            </div>
            {preview && (
              <p className="muted small">
                {preview.w} × {preview.h}px · {(preview.bytes / 1024).toFixed(0)} KB · <code>{stem}</code>
              </p>
            )}
            {scaleWarn && (
              <p className="note">
                Source photo is {p.srcSize[0]}×{p.srcSize[1]}px. Above 1×, material textures are re-rendered at full resolution, but lighting and silhouette are interpolated from the
                source — supply larger studio photos for sharper catalog output.
              </p>
            )}
            <div className="eyebrow">Configuration</div>
            <ul className="codes">
              {p.record.selections.map((s) => (
                <li key={s.groupId}>
                  <span>{s.groupName}</span>
                  <b>{s.code ?? "Original"}</b>
                  <small>{s.name ?? ""}</small>
                </li>
              ))}
            </ul>
            <div className="row gap wrap">
              <button className="btn primary" disabled={!preview} onClick={() => preview && download(preview.blob, `${stem}.${format === "png" ? "png" : "jpg"}`)} data-testid="download-image">
                Download image
              </button>
              <button className="btn" onClick={() => download(new Blob([JSON.stringify(p.record, null, 2)], { type: "application/json" }), `${stem}.json`)}>
                Configuration JSON
              </button>
              <button className="btn" onClick={() => copy(tsv, "Excel row copied")} title="Tab-separated: paste straight into a quotation sheet row">
                Copy Excel row
              </button>
              <button className="btn" onClick={() => copy(p.record.configCode, "Code copied")}>
                Copy config code
              </button>
            </div>
            {copied && <p className="toast-inline">{copied}</p>}
          </div>
        </div>
      </div>
    </div>
  );
}
