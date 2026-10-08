import { useMemo, useState } from "react";
import type { Material, MaterialCategory, MaterialGroup } from "../types";

const LABEL: Record<MaterialCategory, string> = {
  fabric: "Fabric",
  "leather-natural": "Leather",
  "leather-artificial": "Faux leather",
  wood: "Wood",
  metal: "Metal",
  concept: "Special",
};

interface Props {
  materials: Material[];
  group: MaterialGroup | null;
  selected?: string;
  onPick: (id: string | undefined) => void;
  onHover: (id: string | null) => void;
}

/** Swatch grid for one part: category chips (only when there is a choice), optional code search. */
export function Library({ materials, group, selected, onPick, onHover }: Props) {
  const allowed = group?.allowed ?? [];
  const [tab, setTab] = useState<MaterialCategory | null>(null);
  const [q, setQ] = useState("");
  const cats = allowed.filter((c) => materials.some((m) => m.category === c));
  const active = tab && cats.includes(tab) ? tab : cats[0];
  const list = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return materials.filter((m) =>
      needle ? allowed.includes(m.category) && `${m.code} ${m.name}`.toLowerCase().includes(needle) : m.category === active,
    );
  }, [materials, allowed, active, q]);

  if (!group) return <p className="muted pad">Tap a part of the product to see its finishes.</p>;

  return (
    <div className="library">
      <div className="lib-head">
        {cats.length > 1 && !q && (
          <div className="tabs" role="tablist">
            {cats.map((c) => (
              <button key={c} className={active === c ? "on" : ""} onClick={() => setTab(c)}>
                {LABEL[c]}
              </button>
            ))}
          </div>
        )}
        <input className="search" placeholder="Search by code or colour" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search finishes" />
      </div>
      {active === "concept" && !q && <p className="note warn small">Special finishes are demo materials, not part of the Mobica catalog.</p>}
      <div className="grid" onMouseLeave={() => onHover(null)}>
        <button className={`swatch ${!selected ? "on" : ""}`} onClick={() => onPick(undefined)} onMouseEnter={() => onHover("__original__")} title="Keep the finish from the photo">
          <span className="chip orig-chip">⟲</span>
          <span className="meta">
            <b>Original</b>
            <small>as photographed</small>
          </span>
        </button>
        {list.map((m) => (
          <button
            key={m.id}
            className={`swatch ${selected === m.id ? "on" : ""}`}
            onClick={() => onPick(m.id)}
            onMouseEnter={() => onHover(m.id)}
            title={`${m.code} · ${m.name} — ${m.finish}`}
            data-material={m.id}
          >
            <span className="chip" style={{ backgroundImage: `url(${m.thumb})`, backgroundColor: m.avgColor }} />
            <span className="meta">
              <b>{m.code}</b>
              <small>{m.name}</small>
            </span>
          </button>
        ))}
      </div>
      {!list.length && <p className="muted pad">No finish matches “{q}”.</p>}
    </div>
  );
}
