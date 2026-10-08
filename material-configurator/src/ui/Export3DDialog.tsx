import { useState } from "react";
import type { ConfigurationRecord, ProductSpec } from "../types";
import type { Studio3D } from "../engine/studio3d";
import { PNG_KEYWORD, canvasToBlob, download, fileStem, pngWithText } from "../engine/exporter";

interface Props {
  spec: ProductSpec;
  record: ConfigurationRecord;
  studio: Studio3D;
  onClose: () => void;
}

type Size = "catalog" | "excel" | "wide";
const SIZES: Record<Size, { label: string; sub: string; w: number; h: number }> = {
  catalog: { label: "For the catalog", sub: "1600 × 1600, white studio", w: 1600, h: 1600 },
  excel: { label: "For Excel / quotations", sub: "500 × 500, small file", w: 500, h: 500 },
  wide: { label: "Wide banner", sub: "2400 × 1600", w: 2400, h: 1600 },
};

/** Exports a 3D product: quick (instant) or studio render (path traced, ~30–90 s). */
export function Export3DDialog(p: Props) {
  const [size, setSize] = useState<Size>("catalog");
  const [quality, setQuality] = useState<"studio" | "quick">("quick");
  const [progress, setProgress] = useState<number | null>(null);
  const [preview, setPreview] = useState<{ url: string; blob: Blob } | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const stem = fileStem(p.spec, p.record);

  const render = async () => {
    const s = SIZES[size];
    setProgress(0);
    const canvas =
      quality === "studio" ? await p.studio.renderStill(s.w, s.h, s.w > 1000 ? 400 : 200, (f) => setProgress(f)) : p.studio.snapshot(s.w, s.h);
    let blob = await canvasToBlob(canvas, "image/png");
    blob = await pngWithText(blob, PNG_KEYWORD, JSON.stringify(p.record));
    setPreview((old) => {
      if (old) URL.revokeObjectURL(old.url);
      return { url: URL.createObjectURL(blob), blob };
    });
    setProgress(null);
  };

  return (
    <div className="modal-back" onClick={() => progress === null && p.onClose()}>
      <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Download image">
        <header>
          <h2>Download image</h2>
          <button className="icon-btn" onClick={p.onClose} aria-label="Close" disabled={progress !== null}>
            ×
          </button>
        </header>
        <div className="export-body">
          <div className="export-preview checker">
            {preview ? <img src={preview.url} alt="Rendered product" data-testid="export-preview" /> : <p className="muted pad">Choose a size and press Render. The image uses the current camera angle.</p>}
            {progress !== null && (
              <div className="render-progress">
                <span>{quality === "studio" ? "Rendering studio image…" : "Rendering…"}</span>
                <div className="bar">
                  <i style={{ width: `${Math.round(progress * 100)}%` }} />
                </div>
              </div>
            )}
          </div>
          <div className="export-opts">
            <div className="opt-list">
              {(Object.keys(SIZES) as Size[]).map((k) => (
                <button key={k} className={`opt ${size === k ? "on" : ""}`} onClick={() => setSize(k)} disabled={progress !== null}>
                  <b>{SIZES[k].label}</b>
                  <small>{SIZES[k].sub}</small>
                </button>
              ))}
            </div>
            <div className="row gap">
              <button className={`pill ${quality === "quick" ? "on" : ""}`} onClick={() => setQuality("quick")}>
                High quality (instant)
              </button>
              <button className={`pill ${quality === "studio" ? "on" : ""}`} onClick={() => setQuality("studio")}>
                Path traced (slower)
              </button>
            </div>
            <p className="muted small">
              {quality === "studio"
                ? "Path traced like rendering software (light bounces, true soft shadows). About a minute on a computer with a graphics card."
                : "Studio lighting, soft shadow, rendered at double resolution and downsampled for crisp edges."}
            </p>
            <ul className="codes">
              {p.record.selections.map((s) => (
                <li key={s.groupId}>
                  <span>{s.groupName}</span>
                  <b>{s.code ?? "Original"}</b>
                  <small>{s.name ?? ""}</small>
                </li>
              ))}
            </ul>
            <button className="btn primary big wide" onClick={render} disabled={progress !== null} data-testid="render-3d">
              {preview ? "Render again" : "Render"}
            </button>
            <button className="btn big wide" disabled={!preview} onClick={() => preview && download(preview.blob, `${stem}.png`)} data-testid="download-image">
              Download image
            </button>
            <button
              className="btn wide"
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(p.record.configCode);
                  setCopied("Material codes copied");
                } catch {
                  setCopied("Clipboard blocked");
                }
              }}
            >
              Copy material codes
            </button>
            {copied && <p className="toast-inline">{copied}</p>}
            {p.spec.credit && <p className="muted small">{p.spec.credit}</p>}
          </div>
        </div>
      </div>
    </div>
  );
}
