import type { Material, MaterialGroup, PartKind } from "../types";
import { KIND_LABEL, type Identification } from "../engine/identify";

export type SelectMode = "add" | "remove";

interface Props {
  ident: Identification | null;
  locked: boolean;
  mode: SelectMode;
  canUndo: boolean;
  groups: MaterialGroup[];
  selection: Record<string, string | undefined>;
  materials: Map<string, Material>;
  onMode: (m: SelectMode) => void;
  onGrow: (dir: 1 | -1) => void;
  onUndo: () => void;
  onLock: (locked: boolean) => void;
  onKind: (k: PartKind) => void;
  onApply: (groupId: string, materialId: string) => void;
  onOpenGroup: (groupId: string) => void;
  onSplit: () => void;
  onClear: () => void;
  onClose: () => void;
}

const KINDS: PartKind[] = ["fabric", "leather", "wood", "metal"];

/**
 * Right-hand panel of the "Identify a material" tool, in three steps:
 *  1. mark (click a part or draw around an area — the outline snaps to the material),
 *  2. fix it if needed (add / remove / bigger / smaller / undo), then lock it,
 *  3. act on the locked selection (apply a code, open the part, make it its own part).
 */
export function IdentifyPanel(p: Props) {
  const { ident, locked } = p;
  const main = ident?.parts[0];
  const mainGroup = main ? p.groups.find((g) => g.id === main.groupId) : undefined;
  const current = mainGroup && p.selection[mainGroup.id] ? p.materials.get(p.selection[mainGroup.id]!) : undefined;
  const mixed = !!ident && ident.parts.length > 1;
  const step = !ident ? 1 : locked ? 3 : 2;

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

      <ol className="ident-steps" aria-label="Steps">
        <li className={step === 1 ? "on" : "done"}>Mark</li>
        <li className={step === 2 ? "on" : step > 2 ? "done" : ""}>Check &amp; lock</li>
        <li className={step === 3 ? "on" : ""}>Use it</li>
      </ol>

      {!ident && (
        <div className="identify-help">
          <p>
            <b>Click</b> a part of the product to select all of it.
          </p>
          <p>
            <b>Draw a loop</b> around an area (press and drag). It doesn&apos;t need to be neat: the outline snaps to the
            material you circled and follows its real edges.
          </p>
        </div>
      )}

      {ident && !locked && (
        <div className="refine" data-testid="refine">
          <p className="small">
            Is the <b className="hl">yellow area</b> exactly what you mean? If not, fix it — clicks and loops now change
            this selection instead of starting over.
          </p>
          <div className="seg" role="radiogroup" aria-label="What clicks and loops do">
            <button role="radio" aria-checked={p.mode === "add"} className={p.mode === "add" ? "on" : ""} onClick={() => p.onMode("add")} data-testid="mode-add">
              ＋ Add to it
            </button>
            <button role="radio" aria-checked={p.mode === "remove"} className={p.mode === "remove" ? "on remove" : ""} onClick={() => p.onMode("remove")} data-testid="mode-remove">
              − Remove from it
            </button>
          </div>
          <p className="muted small">
            {p.mode === "add" ? "Loops snap to the material you circle; a click adds the patch of material around it." : "A loop cuts out exactly what you draw; a click removes the patch of material around it."}
          </p>
          <div className="row gap wrap">
            <button className="btn small" onClick={() => p.onGrow(-1)} title="Pull the edge in a little">
              Smaller
            </button>
            <button className="btn small" onClick={() => p.onGrow(1)} title="Push the edge out a little">
              Bigger
            </button>
            <button className="btn small" onClick={p.onUndo} disabled={!p.canUndo} data-testid="undo">
              ↶ Undo
            </button>
            <button className="btn small ghost" onClick={p.onClear}>
              Start over
            </button>
          </div>
          <button className="btn primary big wide" onClick={() => p.onLock(true)} data-testid="lock">
            ✓ Looks right — lock selection
          </button>
        </div>
      )}

      {ident && locked && (
        <div className="locked-bar" data-testid="locked">
          <span>🔒 Selection locked</span>
          <button className="link" onClick={() => p.onLock(false)}>
            Edit selection
          </button>
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
                    Your selection covers more than one part ({ident.parts.map((x) => `${p.groups.find((g) => g.id === x.groupId)?.name} ${Math.round(x.share * 100)}%`).join(", ")}).
                    {locked ? " Codes apply to the biggest one; make it its own part to change only this area." : " Use “Remove from it” to take the extra bit out."}
                  </p>
                )}
              </>
            ) : (
              <p className="note small">This area is not a customisable part yet. Lock it, then make it its own part to change its finish.</p>
            )}
          </div>

          <div className="eyebrow">Closest Mobica finishes</div>
          <div className="ident-matches">
            {ident.matches.map((m) => {
              const canApply = locked && !!mainGroup && mainGroup.allowed.includes(m.material.category);
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

          {locked ? (
            <div className="ident-actions">
              {mainGroup && <p className="muted small">Click a finish to apply it to {mainGroup.name.toLowerCase()}.</p>}
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
          ) : (
            <p className="muted small">Lock the selection to apply a finish or make it its own part.</p>
          )}
        </>
      )}
    </div>
  );
}
