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

type Preset = "square1200" | "excel400" | "full";
const PRESETS: Record<Preset, { label: string; sub: string; opts: Omit<ExportOptions, "whiten" | "format"> }> = {
  square1200: { label: "For the catalog", sub: "1200 × 1200 square, white", opts: { scale: 2, framing: "square", squareSize: 1200, margin: 0.08 } },
  excel400: { label: "For Excel / quotations", sub: "400 × 400 square, small file", opts: { scale: 1, framing: "square", squareSize: 400, margin: 0.06 } },
  full: { label: "Full photo", sub: "same framing as the photo, 2×", opts: { scale: 2, framing: "original", margin: 0 } },
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

  const scaleWarn = p.srcSize[0] < 1000 && p.srcSize[1] < 1000;
  return (
    <div className="modal-back" onClick={p.onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Export image">
        <header>
          <h2>Download image</h2>
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
            <div className="opt-list">
              {(Object.keys(PRESETS) as Preset[]).map((k) => (
                <button key={k} className={`opt ${preset === k ? "on" : ""}`} onClick={() => setPreset(k)}>
                  <b>{PRESETS[k].label}</b>
                  <small>{PRESETS[k].sub}</small>
                </button>
              ))}
            </div>
            <ul className="codes">
              {p.record.selections.map((s) => (
                <li key={s.groupId}>
                  <span>{s.groupName}</span>
                  <b>{s.code ?? "Original"}</b>
                  <small>{s.name ?? ""}</small>
                </li>
              ))}
            </ul>
            <button className="btn primary big wide" disabled={!preview} onClick={() => preview && download(preview.blob, `${stem}.${format === "png" ? "png" : "jpg"}`)} data-testid="download-image">
              Download image
            </button>
            <button className="btn wide" onClick={() => copy(p.record.configCode, "Material codes copied")}>
              Copy material codes
            </button>
            {copied && <p className="toast-inline">{copied}</p>}
            <details className="more-opts">
              <summary>More options</summary>
              <label className="check">
                <input type="checkbox" checked={whiten} onChange={(e) => setWhiten(e.target.checked)} /> Pure white background
              </label>
              <div className="row gap">
                <button className={`pill ${format === "png" ? "on" : ""}`} onClick={() => setFormat("png")}>
                  PNG (codes stored inside)
                </button>
                <button className={`pill ${format === "jpeg" ? "on" : ""}`} onClick={() => setFormat("jpeg")}>
                  JPG
                </button>
              </div>
              <div className="row gap wrap">
                <button className="btn small" onClick={() => download(new Blob([JSON.stringify(p.record, null, 2)], { type: "application/json" }), `${stem}.json`)}>
                  Configuration file (JSON)
                </button>
                <button className="btn small" onClick={() => copy(tsv, "Excel row copied")}>
                  Copy Excel row
                </button>
              </div>
              {preview && (
                <p className="muted small">
                  {preview.w} × {preview.h}px · {(preview.bytes / 1024).toFixed(0)} KB
                </p>
              )}
              {scaleWarn && <p className="muted small">Source photo: {p.srcSize[0]}×{p.srcSize[1]}px. Larger source photos give sharper exports.</p>}
            </details>
          </div>
        </div>
      </div>
    </div>
  );
}
