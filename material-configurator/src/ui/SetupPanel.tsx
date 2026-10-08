import type { Mapping, MaterialCategory, Part, PartKind, ProductSpec } from "../types";
import { PART_COLORS, type Tool } from "./Stage";

const KINDS: PartKind[] = ["wood", "fabric", "leather", "metal", "stone", "glass", "other"];
const CATS: { id: MaterialCategory; label: string }[] = [
  { id: "fabric", label: "Fabric" },
  { id: "leather-natural", label: "Nat. leather" },
  { id: "leather-artificial", label: "Art. leather" },
  { id: "wood", label: "Wood" },
  { id: "metal", label: "Metal" },
  { id: "concept", label: "Concept" },
];

const TOOLS: { id: Tool; label: string; hint: string }[] = [
  { id: "smart", label: "Smart select", hint: "Click a surface: similar connected pixels are added. Shift/Alt-click removes." },
  { id: "brush", label: "Brush", hint: "Paint pixels into the active part. Alt-drag erases." },
  { id: "erase", label: "Erase", hint: "Remove pixels from the active part." },
  { id: "poly", label: "Polygon", hint: "Click points, then click the first point / double-click / Enter to close." },
  { id: "quad", label: "Plane", hint: "Drag the 4 corners onto a real rectangle on the surface (perspective texture mapping)." },
  { id: "pick", label: "Pick part", hint: "Click the image to make that part active." },
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

export function SetupPanel(p: Props) {
  const s = p.spec;
  const setPart = (i: number, patch: Partial<Part>) => p.onSpec({ ...s, parts: s.parts.map((x, j) => (j === i ? { ...x, ...patch } : x)) });
  const part = s.parts[p.active];
  const group = part ? s.groups.find((g) => g.id === part.groupId) : undefined;

  const setMapping = (m: Mapping) => part && setPart(p.active, { mapping: m });
  const toPerspective = () => {
    if (!part) return;
    // initial plane: the part's bounding box (user drags the corners onto the real surface)
    const pts = p.partBox(p.active);
    const [x0, y0, x1, y1] = pts ?? [10, 10, 100, 100];
    setMapping({ mode: "perspective", quad: [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], sizeCm: [100, 60], angle: part.mapping.angle, scale: part.mapping.scale });
    p.onTools({ tool: "quad" });
  };

  return (
    <div className="setup-panel">
      <div className="setup-head">
        <div className="eyebrow">Product setup</div>
        <input className="title-input" value={s.name} onChange={(e) => p.onSpec({ ...s, name: e.target.value })} aria-label="Product name" />
        <div className="row gap">
          <label className="field">
            <span>SKU</span>
            <input value={s.sku ?? ""} onChange={(e) => p.onSpec({ ...s, sku: e.target.value })} />
          </label>
          <label className="field" title="Real overall width — sets the physical scale of grain and weave">
            <span>Width (cm)</span>
            <input type="number" min={10} max={1000} value={s.widthCm} onChange={(e) => p.onSpec({ ...s, widthCm: Math.max(1, Number(e.target.value) || 100) })} />
          </label>
        </div>
      </div>

      <div className="step">
        <div className="step-title">
          <span className="num">1</span> Detect parts automatically
        </div>
        <div className="row gap wrap">
          {[2, 3, 4, 5].map((k) => (
            <button key={k} className="pill" onClick={() => p.onAuto(k)} title={`Split the product into ${k} material regions by colour & texture`}>
              {k} materials
            </button>
          ))}
        </div>
        <p className="muted small">Replaces current parts with suggestions. Each suggestion gets a guessed material type you can change.</p>
      </div>

      <div className="step">
        <div className="step-title">
          <span className="num">2</span> Refine with tools
        </div>
        <div className="tools">
          {TOOLS.map((t) => (
            <button key={t.id} className={`tool ${p.tools.tool === t.id ? "on" : ""}`} title={t.hint} onClick={() => p.onTools({ tool: t.id })} disabled={t.id === "quad" && part?.mapping.mode !== "perspective"}>
              {t.label}
            </button>
          ))}
        </div>
        <p className="muted small">{TOOLS.find((t) => t.id === p.tools.tool)?.hint}</p>
        {p.tools.tool === "smart" && (
          <>
            <label className="slider">
              <span>
                Tolerance <em>{p.tools.tolerance}</em>
              </span>
              <input type="range" min={3} max={40} value={p.tools.tolerance} onChange={(e) => p.onTools({ tolerance: +e.target.value })} />
            </label>
            <label className="slider" title="Blur applied before comparing colours: raise for patterned fabrics">
              <span>
                Pattern tolerance <em>{p.tools.smoothing}px</em>
              </span>
              <input type="range" min={1} max={12} value={p.tools.smoothing} onChange={(e) => p.onTools({ smoothing: +e.target.value })} />
            </label>
          </>
        )}
        {(p.tools.tool === "brush" || p.tools.tool === "erase") && (
          <label className="slider">
            <span>
              Brush size <em>{p.tools.brush}px</em>
            </span>
            <input type="range" min={1} max={40} value={p.tools.brush} onChange={(e) => p.onTools({ brush: +e.target.value })} />
          </label>
        )}
        <label className="check">
          <input type="checkbox" checked={p.tools.ignoreBackground} onChange={(e) => p.onTools({ ignoreBackground: e.target.checked })} /> Never select the white backdrop
        </label>
      </div>

      <div className="step">
        <div className="step-title">
          <span className="num">3</span> Parts & material groups
        </div>
        <ul className="parts">
          {s.parts.map((pt, i) => (
            <li key={pt.id} className={i === p.active ? "on" : ""} onClick={() => p.onActive(i)}>
              <span className="dot" style={{ background: PART_COLORS[i % PART_COLORS.length] }} />
              <input value={pt.name} onChange={(e) => setPart(i, { name: e.target.value })} aria-label="Part name" />
              <select value={pt.kind} onChange={(e) => setPart(i, { kind: e.target.value as PartKind })} aria-label="Material type">
                {KINDS.map((k) => (
                  <option key={k}>{k}</option>
                ))}
              </select>
              <small className="muted">{p.areas[i] ? `${p.areas[i].toLocaleString()} px` : "empty"}</small>
              <button
                className="icon-btn"
                title="Delete part"
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
          {(["wood", "fabric", "leather", "metal"] as PartKind[]).map((k) => (
            <button key={k} className="pill ghost" onClick={() => p.onAddPart(k)}>
              + {k}
            </button>
          ))}
        </div>

        {part && (
          <div className="part-detail">
            <label className="field">
              <span>Changes together with (group)</span>
              <select
                value={part.groupId}
                onChange={(e) => {
                  const v = e.target.value;
                  if (v === "__new") {
                    const id = `g-${Math.random().toString(36).slice(2, 7)}`;
                    p.onSpec({ ...s, groups: [...s.groups, { id, name: part.name, allowed: group?.allowed ?? ["wood"] }], parts: s.parts.map((x, j) => (j === p.active ? { ...x, groupId: id } : x)) });
                  } else setPart(p.active, { groupId: v });
                }}
              >
                {s.groups.map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.name}
                  </option>
                ))}
                <option value="__new">+ New group</option>
              </select>
            </label>
            {group && (
              <>
                <label className="field">
                  <span>Group name</span>
                  <input value={group.name} onChange={(e) => p.onSpec({ ...s, groups: s.groups.map((g) => (g.id === group.id ? { ...g, name: e.target.value } : g)) })} />
                </label>
                <div className="field">
                  <span>Allowed finishes</span>
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
              </>
            )}
            <div className="field">
              <span>Texture mapping</span>
              <div className="row gap">
                <button className={`pill ${part.mapping.mode === "planar" ? "on" : ""}`} onClick={() => setMapping({ mode: "planar", angle: part.mapping.angle, scale: part.mapping.scale })}>
                  Flat
                </button>
                <button className={`pill ${part.mapping.mode === "perspective" ? "on" : ""}`} onClick={() => (part.mapping.mode === "perspective" ? p.onTools({ tool: "quad" }) : toPerspective())}>
                  Perspective plane
                </button>
              </div>
              {part.mapping.mode === "perspective" && (
                <div className="row gap">
                  <label className="field">
                    <span>Plane width cm</span>
                    <input type="number" value={part.mapping.sizeCm[0]} onChange={(e) => part.mapping.mode === "perspective" && setMapping({ ...part.mapping, sizeCm: [Number(e.target.value) || 1, part.mapping.sizeCm[1]] })} />
                  </label>
                  <label className="field">
                    <span>height cm</span>
                    <input type="number" value={part.mapping.sizeCm[1]} onChange={(e) => part.mapping.mode === "perspective" && setMapping({ ...part.mapping, sizeCm: [part.mapping.sizeCm[0], Number(e.target.value) || 1] })} />
                  </label>
                </div>
              )}
              <label className="slider">
                <span>
                  Grain direction <em>{part.mapping.angle}°</em>
                </span>
                <input type="range" min={-180} max={180} value={part.mapping.angle} onChange={(e) => setMapping({ ...part.mapping, angle: +e.target.value })} />
              </label>
            </div>
          </div>
        )}
      </div>

      <div className="setup-actions">
        <button className="btn primary" onClick={p.onSave} disabled={!p.dirty}>
          {p.dirty ? "Save product" : "Saved"}
        </button>
        <button className="btn" onClick={p.onDone}>
          Done → configure
        </button>
        <button className="btn ghost" onClick={p.onDownloadSpec} title="Product definition incl. masks — the format a product-management system would store">
          Export spec
        </button>
        {p.onDeleteProduct && (
          <button className="btn ghost danger" onClick={p.onDeleteProduct}>
            {s.origin === "demo" ? "Revert to demo" : "Delete product"}
          </button>
        )}
      </div>
    </div>
  );
}
