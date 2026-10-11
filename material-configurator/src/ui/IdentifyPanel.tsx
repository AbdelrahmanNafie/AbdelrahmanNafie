import type { Material, MaterialGroup, PartKind } from "../types";
import { KIND_LABEL, type Identification } from "../engine/identify";

interface Props {
  ident: Identification | null;
  groups: MaterialGroup[];
  selection: Record<string, string | undefined>;
  materials: Map<string, Material>;
  onKind: (k: PartKind) => void;
  onApply: (groupId: string, materialId: string) => void;
  onOpenGroup: (groupId: string) => void;
  onSplit: () => void;
  onClear: () => void;
  onClose: () => void;
}

const KINDS: PartKind[] = ["fabric", "leather", "wood", "metal"];

/** Right-hand panel of the "Identify a material" tool. */
export function IdentifyPanel(p: Props) {
  const { ident } = p;
  const main = ident?.parts[0];
  const mainGroup = main ? p.groups.find((g) => g.id === main.groupId) : undefined;
  const current = mainGroup && p.selection[mainGroup.id] ? p.materials.get(p.selection[mainGroup.id]!) : undefined;
  const mixed = !!ident && ident.parts.length > 1;

  return (
    <div className="identify" data-testid="identify-panel">
      <div className="row between">
        <div className="step-head flush">
          <span className="num">?</span> Identify a material
        </div>
        <button className="icon-btn" onClick={p.onClose} aria-label="Close identify">
          ×
        </button>
      </div>

      {!ident && (
        <div className="identify-help">
          <p>
            <b>Click</b> any part of the product to see exactly what it is.
          </p>
          <p>
            <b>Draw around</b> an area (press and drag) to check a specific spot.
          </p>
          <p className="muted small">The area lights up in yellow, and you see its material type and the closest Mobica codes.</p>
        </div>
      )}

      {ident && (
        <>
          <div className="ident-card" data-testid="ident-result">
            <span className="ident-color" style={{ background: ident.colorHex }} title={`Measured colour ${ident.colorHex}`} />
            <div>
              <small className="muted">This looks like</small>
              <div className="ident-kind">
                {KINDS.map((k) => (
                  <button key={k} className={`pill ${ident.kind === k ? "on" : ""}`} onClick={() => p.onKind(k)} title="Not right? Choose the correct type">
                    {KIND_LABEL[k]}
                  </button>
                ))}
              </div>
            </div>
          </div>

          <div className="ident-part">
            {mainGroup ? (
              <>
                <small className="muted">Part</small>
                <b>{mainGroup.name}</b>
                <small>Now: {current ? `${current.code} · ${current.name}` : "original finish"}</small>
                {mixed && (
                  <p className="note warn small">
                    Your area covers more than one part ({ident.parts.map((x) => `${p.groups.find((g) => g.id === x.groupId)?.name} ${Math.round(x.share * 100)}%`).join(", ")}). Draw around it more tightly, or make it its own part.
                  </p>
                )}
              </>
            ) : (
              <p className="note small">This area is not a customisable part yet. Make it its own part to change its finish.</p>
            )}
          </div>

          <div className="eyebrow">Closest Mobica finishes</div>
          <div className="ident-matches">
            {ident.matches.map((m) => {
              const canApply = !!mainGroup && mainGroup.allowed.includes(m.material.category);
              return (
                <button
                  key={m.material.id}
                  className="swatch"
                  disabled={!canApply}
                  onClick={() => mainGroup && p.onApply(mainGroup.id, m.material.id)}
                  title={canApply ? `Apply ${m.material.code} to ${mainGroup!.name}` : `${m.material.code} · ${m.material.name}`}
                  data-material={m.material.id}
                >
                  <span className="chip" style={{ backgroundImage: `url(${m.material.thumb})`, backgroundColor: m.material.avgColor }} />
                  <span className="meta">
                    <b>{m.material.code}</b>
                    <small>{m.material.name}</small>
                    <small className={`match-tag ${m.dE < 10 ? "good" : ""}`}>{m.label}</small>
                  </span>
                </button>
              );
            })}
          </div>
          {!!mainGroup && <p className="muted small">Click a finish to apply it to {mainGroup.name.toLowerCase()}.</p>}

          <div className="ident-actions">
            {mainGroup && (
              <button className="btn primary wide" onClick={() => p.onOpenGroup(mainGroup.id)}>
                See all finishes for this part
              </button>
            )}
            <button className="btn wide" onClick={p.onSplit} data-testid="ident-split">
              Make this area its own part
            </button>
            <button className="btn ghost wide" onClick={p.onClear}>
              Mark another area
            </button>
          </div>
        </>
      )}
    </div>
  );
}
