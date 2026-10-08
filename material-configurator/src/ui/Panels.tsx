import type { Mapping, Material, ProductSpec, RenderTuning, Selection } from "../types";
import { tuningFor } from "../engine/prepare";
import type { Match } from "../engine/match";

// ------------------------------------------------------------------ groups
interface GroupsProps {
  spec: ProductSpec;
  selection: Selection;
  materials: Map<string, Material>;
  matches: Record<string, Match[]>;
  active: string | null;
  onSelect: (gid: string) => void;
  onClear: (gid: string) => void;
  onPreset: (sel: Selection) => void;
  onReference: () => void;
  onResetAll: () => void;
}

export function GroupsPanel(p: GroupsProps) {
  const hasRef = p.spec.groups.length > 0;
  return (
    <div className="groups">
      <div className="eyebrow">Customisable parts</div>
      <ul>
        {p.spec.groups.map((g) => {
          const m = p.selection[g.id] ? p.materials.get(p.selection[g.id]!) : undefined;
          const parts = p.spec.parts.filter((x) => x.groupId === g.id);
          return (
            <li key={g.id}>
              <button className={`group ${p.active === g.id ? "on" : ""}`} onClick={() => p.onSelect(g.id)} data-group={g.id}>
                <span className="chip" style={m ? { backgroundImage: `url(${m.thumb})`, backgroundColor: m.avgColor } : undefined}>
                  {!m && "⟲"}
                </span>
                <span className="meta">
                  <b>{g.name}</b>
                  <small>{m ? `${m.code} · ${m.name}` : "Original finish"}</small>
                  {!m && p.matches[g.id]?.[0] && (
                    <small className="muted" title="Closest catalog colour measured from the photo">
                      photo ≈ {p.matches[g.id][0].material.code} {p.matches[g.id][0].material.name}
                    </small>
                  )}
                  {parts.length > 1 && <small className="muted">{parts.length} linked surfaces: {parts.map((x) => x.name).join(", ")}</small>}
                </span>
                {m && (
                  <span
                    className="x"
                    role="button"
                    title="Back to original"
                    onClick={(e) => {
                      e.stopPropagation();
                      p.onClear(g.id);
                    }}
                  >
                    ×
                  </span>
                )}
              </button>
            </li>
          );
        })}
      </ul>
      {!!p.spec.presets?.length && (
        <>
          <div className="eyebrow">Presets</div>
          <div className="presets">
            {p.spec.presets.map((pr) => (
              <button key={pr.name} className="pill" onClick={() => p.onPreset(pr.selection)}>
                {pr.name}
              </button>
            ))}
          </div>
        </>
      )}
      <div className="row gap">
        {hasRef && (
          <button className="pill ghost" onClick={p.onReference} title="Apply, for every part, the catalog finish whose colour is closest to the photo">
            Closest-to-photo
          </button>
        )}
        <button className="pill ghost" onClick={p.onResetAll}>
          Reset all
        </button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ tuning
interface TuneProps {
  spec: ProductSpec;
  groupId: string;
  material?: Material;
  tuning: Record<string, Partial<RenderTuning>>;
  mapping: Record<string, Partial<Mapping>>;
  onTuning: (partIds: string[], t: Partial<RenderTuning>) => void;
  onMapping: (partIds: string[], m: Partial<Mapping>) => void;
  onReset: (partIds: string[]) => void;
}

function Slider(props: { label: string; hint: string; value: number; min: number; max: number; step: number; onChange: (v: number) => void; fmt?: (v: number) => string }) {
  return (
    <label className="slider" title={props.hint}>
      <span>
        {props.label}
        <em>{props.fmt ? props.fmt(props.value) : props.value.toFixed(2)}</em>
      </span>
      <input type="range" min={props.min} max={props.max} step={props.step} value={props.value} onChange={(e) => props.onChange(parseFloat(e.target.value))} />
    </label>
  );
}

export function TuningPanel(p: TuneProps) {
  const idx = p.spec.parts.map((x, i) => [x, i] as const).filter(([x]) => x.groupId === p.groupId);
  if (!idx.length) return null;
  const ids = idx.map(([x]) => x.id);
  const [first, fi] = idx[0];
  const t = tuningFor(p.spec, fi, p.tuning[first.id]);
  const mp = { ...first.mapping, ...(p.mapping[first.id] ?? {}) } as Mapping;
  return (
    <details className="tuning">
      <summary>
        Fine-tune rendering <span className="muted">(optional)</span>
      </summary>
      <p className="muted small">Applies to every surface of this group. Defaults come from the part type; most products never need this.</p>
      <Slider label="Texture scale" hint="Physical size of the material pattern" value={mp.scale ?? 1} min={0.25} max={4} step={0.05} onChange={(v) => p.onMapping(ids, { scale: v })} fmt={(v) => `${v.toFixed(2)}×`} />
      <Slider label="Grain / weave direction" hint="Rotate the pattern on the surface" value={mp.angle ?? 0} min={-180} max={180} step={1} onChange={(v) => p.onMapping(ids, { angle: v })} fmt={(v) => `${v.toFixed(0)}°`} />
      <Slider label="Old pattern removal" hint="How strongly the original material's pattern is filtered out of the lighting. Raise it for busy patterns, lower it to keep sharp creases." value={t.smoothing} min={0.1} max={3} step={0.05} onChange={(v) => p.onTuning(ids, { smoothing: v })} fmt={(v) => `${v.toFixed(2)}%`} />
      <Slider label="Shadow depth" hint="Contrast of the photographed lighting" value={t.contrast} min={0.4} max={1.6} step={0.01} onChange={(v) => p.onTuning(ids, { contrast: v })} />
      <Slider label="Highlights" hint="Strength of photographed reflections" value={t.highlights} min={0} max={2.5} step={0.01} onChange={(v) => p.onTuning(ids, { highlights: v })} />
      <Slider label="Original fine detail" hint="Seams, stitching and creases carried over from the photo" value={t.detail} min={0} max={1} step={0.01} onChange={(v) => p.onTuning(ids, { detail: v })} />
      <Slider label="Brightness" hint="Exposure correction for this group" value={t.exposure} min={0.5} max={1.6} step={0.01} onChange={(v) => p.onTuning(ids, { exposure: v })} />
      <button className="pill ghost" onClick={() => p.onReset(ids)}>
        Reset to defaults
      </button>
      {p.material && !p.material.inCatalog && (
        <p className="note warn">
          <b>{p.material.name}</b> is a procedural concept finish, not a Mobica catalog item.{" "}
          {p.material.render.kind === "glass" && "Glass is rendered against the white studio backdrop: what is behind the panel in reality cannot be recovered from a single photo."}
          {p.material.render.kind === "chrome" && "Chrome mirrors its environment; the reflection here is synthesised from the photo's lighting, not a real environment."}
        </p>
      )}
    </details>
  );
}
