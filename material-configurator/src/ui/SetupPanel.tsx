import type { Mapping, MaterialCategory, Part, PartKind, ProductSpec } from "../types";
import { ALLOWED_BY_KIND } from "../engine/session";
import { PART_COLORS, type Tool } from "./Stage";

const KINDS: { id: PartKind; label: string }[] = [
  { id: "fabric", label: "Fabric" },
  { id: "leather", label: "Leather" },
  { id: "wood", label: "Wood" },
  { id: "metal", label: "Metal" },
  { id: "stone", label: "Stone" },
  { id: "glass", label: "Glass" },
  { id: "other", label: "Other" },
];
const CATS: { id: MaterialCategory; label: string }[] = [
  { id: "fabric", label: "Fabric" },
  { id: "leather-natural", label: "Leather" },
  { id: "leather-artificial", label: "Faux leather" },
  { id: "wood", label: "Wood" },
  { id: "metal", label: "Metal" },
  { id: "concept", label: "Special" },
];

export interface ToolState {
  tool: Tool;
  brush: number;
  tolerance: number;
  smoothing: number;
  ignoreBackground: boolean;
}

interface Props {
  spec: ProductSpec;
  active: number;
  areas: number[];
  tools: ToolState;
  onTools: (t: Partial<ToolState>) => void;
  onActive: (i: number) => void;
  onSpec: (s: ProductSpec) => void;
  onAuto: (k: number) => void;
  onAddPart: (kind: PartKind) => void;
  onDeletePart: (i: number) => void;
  onSave: () => void;
  onDownloadSpec: () => void;
  onDone: () => void;
  onDeleteProduct?: () => void;
  dirty: boolean;
  partBox: (i: number) => [number, number, number, number] | null;
}

/**
 * Product setup in three plain steps: check the parts found automatically, fix any area
 * with click-to-add / click-to-remove, save. Grouping, allowed finishes and perspective
 * mapping stay available under "Advanced".
 */
export function SetupPanel(p: Props) {
  const s = p.spec;
  const setPart = (i: number, patch: Partial<Part>) => p.onSpec({ ...s, parts: s.parts.map((x, j) => (j === i ? { ...x, ...patch } : x)) });
  const part = s.parts[p.active];
  const group = part ? s.groups.find((g) => g.id === part.groupId) : undefined;
  const setMapping = (m: Mapping) => part && setPart(p.active, { mapping: m });
  const brushMode = p.tools.tool === "brush" || p.tools.tool === "erase";

  const changeKind = (i: number, kind: PartKind) => {
    const pt = s.parts[i];
    // keep the finishes offered in line with the type, unless the group is shared with other parts
    const shared = s.parts.filter((x) => x.groupId === pt.groupId).length > 1;
    p.onSpec({
      ...s,
      parts: s.parts.map((x, j) => (j === i ? { ...x, kind } : x)),
      groups: shared ? s.groups : s.groups.map((g) => (g.id === pt.groupId ? { ...g, allowed: ALLOWED_BY_KIND[kind] } : g)),
    });
  };

  return (
    <div className="setup-panel">
      <div className="setup-head">
        <div className="eyebrow">Set up product</div>
        <input className="title-input" value={s.name} onChange={(e) => p.onSpec({ ...s, name: e.target.value, parts: s.parts })} aria-label="Product name" />
        <label className="field inline" title="Used to give grain and weave their real size">
          <span>Real width</span>
          <input type="number" min={10} max={1000} value={s.widthCm} onChange={(e) => p.onSpec({ ...s, widthCm: Math.max(1, Number(e.target.value) || 100) })} />
          <span>cm</span>
        </label>
      </div>

      <div className="step">
        <div className="step-title">
          <span className="num">1</span> Check the parts
        </div>
        <p className="muted small">Each colour on the photo is one part. Give it a name and say what it is made of.</p>
        <ul className="parts">
          {s.parts.map((pt, i) => (
            <li key={pt.id} className={i === p.active ? "on" : ""} onClick={() => p.onActive(i)}>
              <span className="dot" style={{ background: PART_COLORS[i % PART_COLORS.length] }} />
              <input value={pt.name} onChange={(e) => setPart(i, { name: e.target.value })} aria-label="Part name" />
              <select value={pt.kind} onChange={(e) => changeKind(i, e.target.value as PartKind)} aria-label="Material type">
                {KINDS.map((k) => (
                  <option key={k.id} value={k.id}>
                    {k.label}
                  </option>
                ))}
              </select>
              <button
                className="icon-btn"
                title="Remove this part"
                onClick={(e) => {
                  e.stopPropagation();
                  p.onDeletePart(i);
                }}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
        <div className="row gap wrap">
          <button className="pill ghost" onClick={() => p.onAddPart("fabric")}>
            + Add a part
          </button>
          <label className="small muted row gap">
            Detect again with
            <select onChange={(e) => e.target.value && p.onAuto(Number(e.target.value))} value="">
              <option value="">…</option>
              {[2, 3, 4, 5].map((k) => (
                <option key={k} value={k}>
                  {k} parts
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>

      <div className="step">
        <div className="step-title">
          <span className="num">2</span> Fix an area
        </div>
        <p className="muted small">
          Select a part above, then click the photo. <b>Add</b> grabs the whole surface you click; <b>Remove</b> takes it away.
        </p>
        <div className="tools two">
          <button className={`tool big ${p.tools.tool === "smart" ? "on" : ""}`} onClick={() => p.onTools({ tool: "smart" })}>
            ＋ Add area
          </button>
          <button className={`tool big ${p.tools.tool === "unsmart" ? "on" : ""}`} onClick={() => p.onTools({ tool: "unsmart" })}>
            − Remove area
          </button>
        </div>
        <label className="check">
          <input type="checkbox" checked={brushMode} onChange={(e) => p.onTools({ tool: e.target.checked ? "brush" : "smart" })} /> Paint by hand instead (for small details)
        </label>
        {brushMode && (
          <>
            <div className="tools two">
              <button className={`tool ${p.tools.tool === "brush" ? "on" : ""}`} onClick={() => p.onTools({ tool: "brush" })}>
                Paint
              </button>
              <button className={`tool ${p.tools.tool === "erase" ? "on" : ""}`} onClick={() => p.onTools({ tool: "erase" })}>
                Erase
              </button>
            </div>
            <label className="slider">
              <span>
                Brush size <em>{p.tools.brush}px</em>
              </span>
              <input type="range" min={2} max={60} value={p.tools.brush} onChange={(e) => p.onTools({ brush: +e.target.value })} />
            </label>
          </>
        )}
      </div>

      <details className="step advanced">
        <summary>Advanced</summary>
        {(p.tools.tool === "smart" || p.tools.tool === "unsmart") && (
          <label className="slider" title="How different a colour may be and still be added">
            <span>
              Selection reach <em>{p.tools.tolerance}</em>
            </span>
            <input type="range" min={3} max={40} value={p.tools.tolerance} onChange={(e) => p.onTools({ tolerance: +e.target.value })} />
          </label>
        )}
        <div className="row gap wrap">
          <button className={`pill ${p.tools.tool === "poly" ? "on" : ""}`} onClick={() => p.onTools({ tool: "poly" })}>
            Draw outline
          </button>
          {part?.mapping.mode === "perspective" && (
            <button className={`pill ${p.tools.tool === "quad" ? "on" : ""}`} onClick={() => p.onTools({ tool: "quad" })}>
              Edit surface plane
            </button>
          )}
        </div>
        {part && (
          <div className="part-detail">
            <label className="field">
              <span>“{part.name}” changes together with</span>
              <select
                value={part.groupId}
                onChange={(e) => {
                  const v = e.target.value;
                  if (v === "__new") {
                    const id = `g-${Math.random().toString(36).slice(2, 7)}`;
                    p.onSpec({ ...s, groups: [...s.groups, { id, name: part.name, allowed: ALLOWED_BY_KIND[part.kind] }], parts: s.parts.map((x, j) => (j === p.active ? { ...x, groupId: id } : x)) });
                  } else setPart(p.active, { groupId: v });
                }}
              >
                {s.groups.map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.name}
                  </option>
                ))}
                <option value="__new">nothing else (own choice)</option>
              </select>
            </label>
            {group && (
              <div className="field">
                <span>Finishes offered</span>
                <div className="cats">
                  {CATS.map((c) => (
                    <label key={c.id} className="check small">
                      <input
                        type="checkbox"
                        checked={group.allowed.includes(c.id)}
                        onChange={(e) =>
                          p.onSpec({
                            ...s,
                            groups: s.groups.map((g) => (g.id === group.id ? { ...g, allowed: e.target.checked ? [...g.allowed, c.id] : g.allowed.filter((x) => x !== c.id) } : g)),
                          })
                        }
                      />
                      {c.label}
                    </label>
                  ))}
                </div>
              </div>
            )}
            <label className="slider">
              <span>
                Grain direction <em>{part.mapping.angle}°</em>
              </span>
              <input type="range" min={-180} max={180} value={part.mapping.angle} onChange={(e) => setMapping({ ...part.mapping, angle: +e.target.value })} />
            </label>
            {part.mapping.mode === "planar" ? (
              <button
                className="pill ghost"
                onClick={() => {
                  const [x0, y0, x1, y1] = p.partBox(p.active) ?? [10, 10, 100, 100];
                  setMapping({ mode: "perspective", quad: [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], sizeCm: [100, 60], angle: part.mapping.angle, scale: part.mapping.scale });
                  p.onTools({ tool: "quad" });
                }}
              >
                Use a perspective plane (flat surfaces seen at an angle)
              </button>
            ) : (
              <button className="pill ghost" onClick={() => setMapping({ mode: "planar", angle: part.mapping.angle, scale: part.mapping.scale })}>
                Back to flat mapping
              </button>
            )}
          </div>
        )}
        <div className="row gap wrap">
          <button className="btn small ghost" onClick={p.onDownloadSpec}>
            Export product file
          </button>
          {p.onDeleteProduct && (
            <button className="btn small ghost danger" onClick={p.onDeleteProduct}>
              {s.origin === "demo" ? "Undo my changes" : "Delete product"}
            </button>
          )}
        </div>
      </details>

      <div className="setup-actions">
        <button className="btn primary big" onClick={p.onSave}>
          Save and start customising
        </button>
        <button className="btn ghost" onClick={p.onDone}>
          Cancel
        </button>
      </div>
    </div>
  );
}
