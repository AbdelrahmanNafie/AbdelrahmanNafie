import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Mapping, Material, MaterialGroup, Part, PartKind, ProductSpec, RenderTuning, Selection } from "./types";
import { deleteUserProduct, loadDemoProducts, loadMaterials, loadUserProducts, normaliseUpload, saveUserProduct } from "./data/store";
import { ProductSession, newPart } from "./engine/session";
import { DEFAULT_TUNING } from "./engine/prepare";
import { autoSuggest } from "./engine/segment";
import { buildRecord, download, PNG_KEYWORD, readPngText } from "./engine/exporter";
import { measureFidelity, type FidelityResult } from "./engine/fidelity";
import { closestFinishes, type Match } from "./engine/match";
import type { Renderer, PartDraw } from "./engine/renderer";
import type { DrawItem } from "./engine/session";
import { Stage, type ViewMode } from "./ui/Stage";
import { Library } from "./ui/Library";
import { TuningPanel } from "./ui/Panels";
import { ExportDialog } from "./ui/ExportDialog";
import { SetupPanel, type ToolState } from "./ui/SetupPanel";

interface ProductState {
  selection: Selection;
  tuning: Record<string, Partial<RenderTuning>>;
  mapping: Record<string, Partial<Mapping>>;
}
const EMPTY: ProductState = { selection: {}, tuning: {}, mapping: {} };

function parseHash(): { id?: string; sel: Selection } {
  const m = location.hash.match(/^#\/([^?]+)(?:\?(.*))?$/);
  if (!m) return { sel: {} };
  const sel: Selection = {};
  new URLSearchParams(m[2] ?? "").forEach((v, k) => (sel[k] = v));
  return { id: decodeURIComponent(m[1]), sel };
}

export default function App() {
  const [materials, setMaterials] = useState<Material[]>([]);
  const [products, setProducts] = useState<ProductSpec[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [session, setSession] = useState<ProductSession | null>(null);
  const [version, setVersion] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [states, setStates] = useState<Record<string, ProductState>>({});
  const [mode, setMode] = useState<"configure" | "setup">("configure");
  const [activeGroup, setActiveGroup] = useState<string | null>(null);
  const [hoverPart, setHoverPart] = useState(-1);
  const [preview, setPreview] = useState<string | null>(null);
  const [view, setView] = useState<ViewMode>("result");
  const [zoom, setZoom] = useState(1);
  const [exportOpen, setExportOpen] = useState(false);
  const [fidelity, setFidelity] = useState<FidelityResult | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [moreOpen, setMoreOpen] = useState(false);
  const [tuneOpen, setTuneOpen] = useState(false);
  // setup mode
  const [draft, setDraft] = useState<ProductSpec | null>(null);
  const [activePart, setActivePart] = useState(0);
  const [tools, setTools] = useState<ToolState>({ tool: "smart", brush: 6, tolerance: 14, smoothing: 3, ignoreBackground: true });
  const [dirty, setDirty] = useState(false);
  const rendererRef = useRef<Renderer | null>(null);
  const toDraw = useRef<((items: DrawItem[]) => PartDraw[]) | null>(null);
  const uploadRef = useRef<HTMLInputElement>(null);
  const pendingHash = useRef(parseHash());

  const matMap = useMemo(() => new Map(materials.map((m) => [m.id, m])), [materials]);
  const notify = (t: string) => {
    setToast(t);
    setTimeout(() => setToast(null), 2600);
  };

  // ------------------------------------------------------------ boot
  useEffect(() => {
    (async () => {
      try {
        const [mats, demos, users] = await Promise.all([loadMaterials(), loadDemoProducts(), loadUserProducts()]);
        setMaterials(mats);
        const merged = demos.map((d) => users.find((u) => u.id === d.id) ?? d).concat(users.filter((u) => !demos.some((d) => d.id === u.id)));
        setProducts(merged);
        const h = pendingHash.current;
        const start = merged.find((p) => p.id === h.id) ?? merged[0];
        if (h.id === start.id) setStates((s) => ({ ...s, [start.id]: { ...EMPTY, selection: h.sel } }));
        setActiveId(start.id);
      } catch (e) {
        setError((e as Error).message);
      }
    })();
  }, []);

  const spec = useMemo(() => products.find((p) => p.id === activeId) ?? null, [products, activeId]);
  const st = (activeId && states[activeId]) || EMPTY;
  const patchState = useCallback(
    (fn: (s: ProductState) => ProductState) => activeId && setStates((all) => ({ ...all, [activeId]: fn(all[activeId] ?? EMPTY) })),
    [activeId],
  );

  // ------------------------------------------------------------ open product
  useEffect(() => {
    if (!spec) return;
    let cancelled = false;
    setLoading(true);
    setFidelity(null);
    ProductSession.open(spec, states[spec.id]?.tuning)
      .then((s) => {
        if (cancelled) return;
        setSession(s);
        setVersion((v) => v + 1);
        setActiveGroup(spec.groups[0]?.id ?? null);
        setLoading(false);
        if (!spec.parts.length) {
          enterSetup(s, spec);
          autoDetectFor(s, spec, 3);
        }
      })
      .catch((e) => setError(String(e)));
    return () => {
      cancelled = true;
    };
    // only when the product itself changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, spec?.image]);

  // keep the URL shareable
  useEffect(() => {
    if (!activeId) return;
    const q = new URLSearchParams(Object.entries(st.selection).filter(([, v]) => v) as [string, string][]).toString();
    history.replaceState(null, "", `#/${encodeURIComponent(activeId)}${q ? `?${q}` : ""}`);
  }, [activeId, st.selection]);

  // ------------------------------------------------------------ drawing
  const effectiveSelection = useMemo(() => {
    if (!preview || !activeGroup || mode !== "configure") return st.selection;
    return { ...st.selection, [activeGroup]: preview === "__original__" ? undefined : preview };
  }, [st.selection, preview, activeGroup, mode]);

  const items = useMemo(
    () => (session && materials.length ? session.drawList(effectiveSelection, matMap, st.tuning, st.mapping) : []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [session, version, effectiveSelection, matMap, st.tuning, st.mapping, materials.length],
  );

  const renderAt = useCallback(
    async (scale: number, sel?: Selection) => {
      const r = rendererRef.current!;
      const s = session!;
      const list = sel ? s.drawList(sel, matMap, st.tuning, st.mapping) : items;
      await r.ensureTextures(list.map((i) => i.material));
      const w = Math.round(s.w * scale);
      const h = Math.round(s.h * scale);
      const px = r.renderToPixels(toDraw.current!(list), w, h);
      const labels = s.prepared.labels;
      const bd = s.li.bg.backdrop;
      const isProduct = (i: number) => {
        const x = Math.min(s.w - 1, Math.floor((i % w) / scale));
        const y = Math.min(s.h - 1, Math.floor(Math.floor(i / w) / scale));
        const j = y * s.w + x;
        return labels[j] >= 0 || !bd[j];
      };
      return { px, w, h, isProduct };
    },
    [session, items, matMap, st.tuning, st.mapping],
  );

  // ------------------------------------------------------------ configure actions
  const pick = (gid: string, mid: string | undefined) => {
    patchState((s) => ({ ...s, selection: { ...s.selection, [gid]: mid } }));
    setPreview(null);
    setFidelity(null);
    if (view === "diff") setView("result");
  };

  const applyTuning = (partIds: string[], t: Partial<RenderTuning>) => {
    patchState((s) => {
      const tuning = { ...s.tuning };
      partIds.forEach((id) => (tuning[id] = { ...tuning[id], ...t }));
      return { ...s, tuning };
    });
    if (t.smoothing !== undefined && session) {
      partIds.forEach((id) => session.setSmoothing(session.spec.parts.findIndex((p) => p.id === id), t.smoothing!));
      setVersion((v) => v + 1);
    }
  };
  const applyMapping = (partIds: string[], m: Partial<Mapping>) =>
    patchState((s) => {
      const mapping = { ...s.mapping };
      partIds.forEach((id) => (mapping[id] = { ...mapping[id], ...m } as Partial<Mapping>));
      return { ...s, mapping };
    });
  const resetTuning = (partIds: string[]) => {
    patchState((s) => {
      const tuning = { ...s.tuning };
      const mapping = { ...s.mapping };
      partIds.forEach((id) => {
        delete tuning[id];
        delete mapping[id];
      });
      return { ...s, tuning, mapping };
    });
    if (session) {
      partIds.forEach((id) => {
        const i = session.spec.parts.findIndex((p) => p.id === id);
        const part = session.spec.parts[i];
        session.setSmoothing(i, part.tuning?.smoothing ?? DEFAULT_TUNING[part.kind].smoothing);
      });
      setVersion((v) => v + 1);
    }
  };

  // closest catalog finish per group, measured from the photo (recomputed when masks change)
  const matches = useMemo<Record<string, Match[]>>(
    () => (session && materials.length && mode === "configure" ? Object.fromEntries(session.spec.groups.map((g) => [g.id, closestFinishes(session, g, materials)])) : {}),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [session, materials, mode, version],
  );
  const referenceSelection = (): Selection =>
    Object.fromEntries((spec?.groups ?? []).map((g) => [g.id, g.referenceMaterial ?? matches[g.id]?.[0]?.material.id]));
  const runFidelity = async () => {
    if (!session || !spec) return;
    const ref = referenceSelection();
    patchState((s) => ({ ...s, selection: ref }));
    const { px } = await renderAt(1, ref);
    const res = measureFidelity(session, px, ref as Record<string, string | null>);
    setFidelity(res);
    setView("diff");
  };

  // drop an exported PNG to restore its configuration
  const restoreFromFile = async (f: File) => {
    const txt = await readPngText(f, PNG_KEYWORD);
    if (!txt) return notify("No configuration found in that image.");
    try {
      const rec = JSON.parse(txt);
      const target = products.find((p) => p.id === rec.productId);
      if (!target) return notify(`Product “${rec.productName}” is not in this library.`);
      const sel: Selection = {};
      for (const s of rec.selections) sel[s.groupId] = s.materialId ?? undefined;
      setStates((all) => ({ ...all, [target.id]: { ...(all[target.id] ?? EMPTY), selection: sel } }));
      setActiveId(target.id);
      notify("Configuration restored from image");
    } catch {
      notify("Could not read that configuration.");
    }
  };

  // ------------------------------------------------------------ setup mode
  function enterSetup(s = session!, sp = spec!) {
    const d = structuredClone(sp);
    setDraft(d);
    s.setSpec(d);
    setActivePart(0);
    setMode("setup");
    setView("result");
    setDirty(false);
    setTools((t) => ({ ...t, tool: d.parts.length ? "pick" : "smart" }));
  }

  const updateDraft = (next: ProductSpec, labels?: Int16Array, changed?: Set<number>) => {
    if (!session) return;
    const countChanged = next.parts.length !== session.spec.parts.length;
    session.setSpec(next);
    if (labels || countChanged) session.setLabels(labels ?? session.prepared.labels, countChanged ? undefined : changed, st.tuning);
    setDraft(next);
    setDirty(true);
    setVersion((v) => v + 1);
  };

  const autoDetect = (k: number) => session && draft && autoDetectFor(session, draft, k);
  function autoDetectFor(session: ProductSession, draft: ProductSpec, k: number) {
    const sugg = autoSuggest(session.li, k);
    if (!sugg.length) return notify("Nothing to split — is the background white?");
    const parts: Part[] = [];
    const groups: MaterialGroup[] = [];
    const labels = new Int16Array(session.w * session.h).fill(-1);
    sugg.forEach((sg, i) => {
      const { part, group } = newPart(draft, sg.kind, sg.name);
      parts.push(part);
      if (group) groups.push(group);
      for (let j = 0; j < labels.length; j++) if (sg.mask[j]) labels[j] = i;
    });
    const next = { ...draft, parts, groups };
    session.setSpec(next);
    session.setLabels(labels, undefined, st.tuning);
    setDraft(next);
    setDirty(true);
    setVersion((v) => v + 1);
    setActivePart(0);
    setTools((t) => ({ ...t, tool: "smart" }));
    notify(`Found ${parts.length} parts. Check their names and types, then fix any area that is wrong.`);
  }

  const addPart = (kind: PartKind) => {
    if (!draft) return;
    const { part, group } = newPart(draft, kind, `${kind[0].toUpperCase()}${kind.slice(1)} ${draft.parts.filter((p) => p.kind === kind).length + 1}`);
    updateDraft({ ...draft, parts: [...draft.parts, part], groups: group ? [...draft.groups, group] : draft.groups });
    setActivePart(draft.parts.length);
    setTools((t) => ({ ...t, tool: "smart" }));
  };

  const deletePart = (i: number) => {
    if (!draft || !session) return;
    const labels = session.prepared.labels.slice();
    for (let j = 0; j < labels.length; j++) {
      if (labels[j] === i) labels[j] = -1;
      else if (labels[j] > i) labels[j]--;
    }
    const parts = draft.parts.filter((_, j) => j !== i);
    const groups = draft.groups.filter((g) => parts.some((p) => p.groupId === g.id));
    updateDraft({ ...draft, parts, groups }, labels);
    setActivePart(Math.max(0, Math.min(i, parts.length - 1)));
  };

  const saveDraft = async () => {
    if (!session || !draft) return;
    // drop groups nobody uses, ensure every part has a group
    const groups = draft.groups.filter((g) => draft.parts.some((p) => p.groupId === g.id));
    const full = { ...session.toSpecWithBitmaps(), groups };
    await saveUserProduct(full);
    setProducts((ps) => (ps.some((p) => p.id === full.id) ? ps.map((p) => (p.id === full.id ? { ...full, origin: "user" as const } : p)) : [...ps, { ...full, origin: "user" as const }]));
    setDirty(false);
    notify("Product saved in this browser");
  };

  const leaveSetup = () => {
    if (!session || !draft) return;
    setProducts((ps) => ps.map((p) => (p.id === draft.id ? { ...draft } : p)));
    session.setSpec(draft);
    setMode("configure");
    setActiveGroup(draft.groups[0]?.id ?? null);
    setVersion((v) => v + 1);
  };

  const onUpload = async (f: File) => {
    if (f.type === "image/png") {
      const txt = await readPngText(f, PNG_KEYWORD);
      if (txt) return restoreFromFile(f);
    }
    const image = await normaliseUpload(f);
    const id = `user-${Date.now().toString(36)}`;
    const p: ProductSpec = {
      id,
      name: f.name.replace(/\.[^.]+$/, "").replace(/[-_]+/g, " "),
      category: "Uploaded",
      widthCm: 100,
      image,
      groups: [],
      parts: [],
      origin: "user",
    };
    setProducts((ps) => [...ps, p]);
    setActiveId(id);
  };

  const removeProduct = async () => {
    if (!spec) return;
    await deleteUserProduct(spec.id);
    if (spec.origin === "demo" || (await loadDemoProducts()).some((d) => d.id === spec.id)) {
      const demo = (await loadDemoProducts()).find((d) => d.id === spec.id)!;
      setProducts((ps) => ps.map((p) => (p.id === spec.id ? demo : p)));
      setMode("configure");
      notify("Reverted to the bundled demo definition");
    } else {
      const rest = products.filter((p) => p.id !== spec.id);
      setProducts(rest);
      setMode("configure");
      setActiveId(rest[0]?.id ?? null);
    }
  };

  // ------------------------------------------------------------ render
  if (error) return <div className="fatal">Could not start: {error}</div>;
  const group = spec?.groups.find((g) => g.id === activeGroup) ?? null;
  const record = spec ? buildRecord(spec, st.selection, matMap) : null;
  const changed = spec ? spec.groups.some((g) => st.selection[g.id]) : false;
  const selectedMat = group && st.selection[group.id] ? matMap.get(st.selection[group.id]!) : undefined;

  return (
    <div
      className="app"
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        const f = e.dataTransfer.files[0];
        if (f) onUpload(f);
      }}
    >
      <header className="topbar">
        <div className="brand">
          <span className="logo">◆</span>
          <b>Material Studio</b>
        </div>
        <nav className="products" aria-label="Products">
          {products.map((p) => (
            <button key={p.id} className={`prod ${p.id === activeId ? "on" : ""}`} onClick={() => mode === "configure" && setActiveId(p.id)} disabled={mode === "setup" && p.id !== activeId} title={p.name} data-product={p.id}>
              <img src={p.image} alt="" />
              <span>{p.name}</span>
            </button>
          ))}
          <button className="prod add" onClick={() => uploadRef.current?.click()} disabled={mode === "setup"} title="Add your own product photo (white background works best)">
            <span className="plus">+</span>
            <span>Add product</span>
          </button>
          <input ref={uploadRef} type="file" accept="image/*" hidden onChange={(e) => e.target.files?.[0] && onUpload(e.target.files[0])} />
        </nav>
      </header>

      <main className={`workspace ${mode}`}>
        {mode === "setup" && draft && session && (
          <aside className="left">
            <SetupPanel
              spec={draft}
              active={activePart}
              areas={session.prepared.parts.map((p) => p.area)}
              tools={tools}
              onTools={(t) => setTools((x) => ({ ...x, ...t }))}
              onActive={setActivePart}
              onSpec={(s) => updateDraft(s)}
              onAuto={autoDetect}
              onAddPart={addPart}
              onDeletePart={deletePart}
              onSave={async () => {
                await saveDraft();
                leaveSetup();
              }}
              onDownloadSpec={() => download(new Blob([JSON.stringify(session.toSpecWithBitmaps(), null, 1)], { type: "application/json" }), `${draft.id}.product.json`)}
              onDone={leaveSetup}
              onDeleteProduct={removeProduct}
              dirty={dirty}
              partBox={(i) => {
                const b = session.prepared.parts[i]?.box;
                return b ? [b.x0, b.y0, b.x1, b.y1] : null;
              }}
            />
          </aside>
        )}

        <section className="center">
          {mode === "configure" && (
            <div className="viewbar">
              <div className="prod-title">
                <h1>{spec?.name}</h1>
                <small className="muted">Tap a part of the product, then pick a finish.</small>
              </div>
              <div className="more">
                <button className="btn ghost" onClick={() => setMoreOpen((o) => !o)} aria-expanded={moreOpen} data-testid="more">
                  More ▾
                </button>
                {moreOpen && (
                  <div className="menu" onClick={() => setMoreOpen(false)}>
                    <button onClick={() => setView(view === "side" ? "result" : "side")}>{view === "side" ? "Single view" : "Before / after side by side"}</button>
                    <button onClick={() => setZoom(zoom === 1 ? 2 : 1)}>{zoom === 1 ? "Zoom in 2×" : "Fit to screen"}</button>
                    <button onClick={() => setTuneOpen((o) => !o)}>{tuneOpen ? "Hide fine-tuning" : "Fine-tune the selected part"}</button>
                    <button onClick={runFidelity}>Accuracy check</button>
                    <button onClick={() => enterSetup()}>Edit parts of this product</button>
                  </div>
                )}
              </div>
            </div>
          )}
          {session && (
            <Stage
              session={session}
              version={version}
              items={items}
              view={view}
              zoom={zoom}
              highlightGroup={null}
              hoverPart={hoverPart}
              onHoverPart={setHoverPart}
              onPickPart={(i) => {
                if (mode === "setup") return setActivePart(i);
                if (i >= 0) setActiveGroup(session.spec.parts[i].groupId);
              }}
              diff={fidelity?.heat}
              rendererRef={rendererRef}
              toDraw={toDraw}
              editor={
                mode === "setup" && draft
                  ? {
                      tool: tools.tool,
                      activePart: draft.parts.length ? activePart : -1,
                      brush: tools.brush,
                      tolerance: tools.tolerance,
                      smoothing: tools.smoothing,
                      ignoreBackground: tools.ignoreBackground,
                      onLabels: (labels, changedParts) => updateDraft(draft, labels, changedParts),
                      quad: draft.parts[activePart]?.mapping.mode === "perspective" ? (draft.parts[activePart].mapping as Extract<Mapping, { mode: "perspective" }>).quad : null,
                      onQuad: (q) => {
                        const pt = draft.parts[activePart];
                        if (pt.mapping.mode !== "perspective") return;
                        updateDraft({ ...draft, parts: draft.parts.map((x, j) => (j === activePart ? { ...x, mapping: { ...pt.mapping, quad: q } as Mapping } : x)) });
                      },
                    }
                  : null
              }
            />
          )}
          {loading && <div className="loading">Preparing product…</div>}
          {hoverPart >= 0 && session && mode === "configure" && (
            <div className="hover-tag">{session.spec.groups.find((g) => g.id === session.spec.parts[hoverPart]?.groupId)?.name}</div>
          )}
          {fidelity && view === "diff" && (
            <div className="fidelity-card" data-testid="fidelity">
              <div className="row between">
                <b>Accuracy check</b>
                <button className="icon-btn" onClick={() => setView("result")} aria-label="Close">
                  ×
                </button>
              </div>
              <p className="muted small">Each part is re-rendered with the catalog finish closest to the photo and compared with the photo. Green = close, red = different.</p>
              <table>
                <thead>
                  <tr>
                    <th>Part</th>
                    <th>Finish</th>
                    <th className="num" title="Colour difference (ΔE)">Colour</th>
                    <th className="num" title="How well shape, shadows and highlights are kept (1.00 = identical)">Shape &amp; light</th>
                  </tr>
                </thead>
                <tbody>
                  {fidelity.groups.map((g) => (
                    <tr key={g.groupId}>
                      <td>{g.name}</td>
                      <td>{g.reference ? matMap.get(g.reference)?.code : "—"}</td>
                      <td className="num">{Number.isFinite(g.meanDE) ? g.meanDE.toFixed(1) : "—"}</td>
                      <td className="num">{Number.isFinite(g.lightingR) ? g.lightingR.toFixed(2) : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {mode === "configure" && session && (
            <footer className="actionbar">
              <button
                className="btn"
                onPointerDown={() => setView("original")}
                onPointerUp={() => setView("result")}
                onPointerLeave={() => view === "original" && setView("result")}
                onKeyDown={(e) => e.key === " " && setView("original")}
                onKeyUp={() => setView("result")}
                disabled={!changed}
                title="Press and hold to see the original photo"
              >
                ◐ Hold to compare
              </button>
              <button
                className="btn ghost"
                disabled={!changed}
                onClick={() => {
                  patchState(() => EMPTY);
                  session.spec.parts.forEach((p, i) => session.setSmoothing(i, p.tuning?.smoothing ?? DEFAULT_TUNING[p.kind].smoothing));
                  setVersion((v) => v + 1);
                  setFidelity(null);
                  setView("result");
                }}
              >
                Reset
              </button>
              <span className="spacer" />
              <button className="btn primary big" onClick={() => setExportOpen(true)} data-testid="open-export">
                Download image
              </button>
            </footer>
          )}
        </section>

        {mode === "configure" && spec && (
          <aside className="right">
            <div className="step-head">
              <span className="num">1</span> Choose a part
            </div>
            <div className="part-tabs" role="tablist">
              {spec.groups.map((g) => {
                const m = st.selection[g.id] ? matMap.get(st.selection[g.id]!) : undefined;
                return (
                  <button key={g.id} role="tab" aria-selected={g.id === activeGroup} className={`part-tab ${g.id === activeGroup ? "on" : ""}`} onClick={() => setActiveGroup(g.id)} data-group={g.id}>
                    <span className="chip" style={m ? { backgroundImage: `url(${m.thumb})`, backgroundColor: m.avgColor } : undefined} />
                    <span className="meta">
                      <b>{g.name}</b>
                      <small>{m ? `${m.code} · ${m.name}` : "Original"}</small>
                    </span>
                  </button>
                );
              })}
            </div>
            {tuneOpen && group && (
              <TuningPanel
                spec={spec}
                groupId={group.id}
                material={selectedMat}
                tuning={st.tuning}
                mapping={st.mapping}
                onTuning={applyTuning}
                onMapping={applyMapping}
                onReset={resetTuning}
              />
            )}
            <div className="step-head">
              <span className="num">2</span> Choose a finish{group ? ` for ${group.name.toLowerCase()}` : ""}
            </div>
            <Library materials={materials} group={group} selected={group ? st.selection[group.id] : undefined} onPick={(id) => group && pick(group.id, id)} onHover={setPreview} />
            {!!spec.presets?.length && (
              <div className="ideas">
                <div className="eyebrow">Suggested looks</div>
                <div className="presets">
                  {spec.presets.map((pr) => (
                    <button
                      key={pr.name}
                      className="pill"
                      onClick={() => {
                        patchState((s) => ({ ...s, selection: { ...pr.selection } }));
                        setFidelity(null);
                        setView("result");
                      }}
                    >
                      {pr.name}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </aside>
        )}
      </main>

      {exportOpen && spec && record && session && (
        <ExportDialog spec={spec} record={record} srcSize={[session.w, session.h]} productBox={session.prepared.productBox} render={(s) => renderAt(s)} onClose={() => setExportOpen(false)} />
      )}
      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}
