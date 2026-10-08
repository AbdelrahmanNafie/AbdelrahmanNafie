import { useMemo, useState } from "react";
import type { Material, MaterialCategory, MaterialGroup } from "../types";

const LABEL: Record<MaterialCategory, string> = {
  fabric: "Fabric",
  "leather-natural": "Natural leather",
  "leather-artificial": "Artificial leather",
  wood: "Wood",
  metal: "Metal",
  concept: "Concept",
};

interface Props {
  materials: Material[];
  group: MaterialGroup | null;
  selected?: string;
  onPick: (id: string | undefined) => void;
  onHover: (id: string | null) => void;
}

export function Library({ materials, group, selected, onPick, onHover }: Props) {
  const [tab, setTab] = useState<MaterialCategory | "all">("all");
  const [q, setQ] = useState("");
  const allowed = group?.allowed ?? [];
  const activeTab = tab !== "all" && !allowed.includes(tab) ? "all" : tab;
  const list = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return materials.filter(
      (m) =>
        allowed.includes(m.category) &&
        (activeTab === "all" || m.category === activeTab) &&
        (!needle || `${m.code} ${m.name} ${m.finish} ${m.categoryLabel}`.toLowerCase().includes(needle)),
    );
  }, [materials, allowed, activeTab, q]);

  if (!group)
    return (
      <div className="library empty-state">
        <p>Select a part on the product, or a group on the left, to browse the finishes available for it.</p>
      </div>
    );

  const byCat = allowed.map((c) => ({ c, items: list.filter((m) => m.category === c) })).filter((x) => x.items.length);
  return (
    <div className="library">
      <div className="lib-head">
        <div>
          <div className="eyebrow">Finishes for</div>
          <h3>{group.name}</h3>
        </div>
        <input className="search" placeholder="Search code or colour…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search materials" />
      </div>
      <div className="tabs" role="tablist">
        <button className={activeTab === "all" ? "on" : ""} onClick={() => setTab("all")}>
          All
        </button>
        {allowed.map((c) => (
          <button key={c} className={activeTab === c ? "on" : ""} onClick={() => setTab(c)}>
            {LABEL[c]}
          </button>
        ))}
      </div>
      <div className="lib-scroll" onMouseLeave={() => onHover(null)}>
        <button className={`swatch original ${!selected ? "on" : ""}`} onClick={() => onPick(undefined)} onMouseEnter={() => onHover("__original__")}>
          <span className="chip orig-chip">⟲</span>
          <span className="meta">
            <b>Original</b>
            <small>As photographed</small>
          </span>
        </button>
        {byCat.map(({ c, items }) => (
          <section key={c}>
            <h4>
              {LABEL[c]}
              {c === "concept" && <span className="badge warn" title="Procedural materials that are NOT in the Mobica catalog. Included to show the engine can render stone, chrome, brushed metal and glass.">not in catalog</span>}
            </h4>
            <div className="grid">
              {items.map((m) => (
                <button
                  key={m.id}
                  className={`swatch ${selected === m.id ? "on" : ""}`}
                  onClick={() => onPick(m.id)}
                  onMouseEnter={() => onHover(m.id)}
                  title={`${m.code} · ${m.name}\n${m.finish}\n${m.source}`}
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
          </section>
        ))}
        {!list.length && <p className="muted pad">No finishes match “{q}”.</p>}
      </div>
    </div>
  );
}
