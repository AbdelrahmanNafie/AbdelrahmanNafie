import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Renderer, type PartDraw } from "../engine/renderer";
import type { DrawItem, ProductSession } from "../engine/session";
import type { PreparedPart } from "../engine/prepare";
import { paintCircle, smartSelect } from "../engine/segment";
import { rasterizePolygon } from "../engine/imageOps";

export type ViewMode = "result" | "original" | "split" | "side" | "diff";
export type Tool = "pick" | "smart" | "unsmart" | "brush" | "erase" | "poly" | "quad";

export const PART_COLORS = ["#e4572e", "#29a3a3", "#f3a712", "#6a4c93", "#4c956c", "#d1495b", "#1982c4", "#8ac926", "#ff924c", "#b5179e"];

export interface EditorProps {
  tool: Tool;
  activePart: number;
  brush: number;
  tolerance: number;
  smoothing: number;
  ignoreBackground: boolean;
  onLabels: (labels: Int16Array, changed: Set<number>) => void;
  quad?: [number, number][] | null;
  onQuad?: (q: [number, number][]) => void;
}

interface Props {
  session: ProductSession;
  version: number;
  items: DrawItem[];
  view: ViewMode;
  zoom: number;
  highlightGroup: string | null;
  hoverPart: number;
  onHoverPart: (i: number) => void;
  onPickPart: (i: number) => void;
  diff?: ImageData | null;
  editor?: EditorProps | null;
  rendererRef?: React.MutableRefObject<Renderer | null>;
  toDraw?: React.MutableRefObject<((items: DrawItem[]) => PartDraw[]) | null>;
}

function hexRgb(h: string): [number, number, number] {
  return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
}

export function Stage(p: Props) {
  const { session } = p;
  const wrap = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const overlay = useRef<HTMLCanvasElement>(null);
  const renderer = useRef<Renderer | null>(null);
  const gpu = useRef(new WeakMap<PreparedPart, WebGLTexture>());
  const live = useRef(new Set<WebGLTexture>());
  const [box, setBox] = useState({ w: 800, h: 600 });
  const [split, setSplit] = useState(0.5);
  const [glError, setGlError] = useState<string | null>(null);
  const [polyPts, setPolyPts] = useState<[number, number][]>([]);
  const [cursor, setCursor] = useState<[number, number] | null>(null);
  const stroke = useRef<{ mask: Uint8Array; value: 0 | 1 } | null>(null);
  const dragHandle = useRef<number>(-1);
  const [, force] = useState(0);

  // ------------------------------------------------------------ layout
  useLayoutEffect(() => {
    const el = wrap.current!;
    const ro = new ResizeObserver(() => setBox({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    setBox({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);

  const panes = p.view === "side" ? 2 : 1;
  // Frame the product, not the whole photo: crop to its bounding box plus a margin.
  const pb = session.prepared.productBox;
  const padX = (pb.x1 - pb.x0) * 0.08 + 8;
  const padY = (pb.y1 - pb.y0) * 0.08 + 8;
  const crop = {
    x0: Math.max(0, pb.x0 - padX),
    y0: Math.max(0, pb.y0 - padY),
    x1: Math.min(session.w, pb.x1 + padX),
    y1: Math.min(session.h, pb.y1 + padY),
  };
  const cropW = crop.x1 - crop.x0;
  const cropH = crop.y1 - crop.y0;
  const fit = Math.max(0.05, Math.min((box.w - 40 - (panes - 1) * 16) / panes / cropW, (box.h - 40) / cropH)) * p.zoom;
  const cssW = Math.max(50, Math.round(session.w * fit));
  const cssH = Math.max(50, Math.round(session.h * fit));
  const frameW = Math.round(cropW * fit);
  const frameH = Math.round(cropH * fit);
  const layerPos = { left: -Math.round(crop.x0 * fit), top: -Math.round(crop.y0 * fit), width: cssW, height: cssH };
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const backW = Math.min(4096, Math.round(cssW * dpr));
  const backH = Math.min(4096, Math.round(cssH * dpr));

  // ------------------------------------------------------------ renderer
  useEffect(() => {
    try {
      renderer.current = new Renderer(canvas.current!);
      if (p.rendererRef) p.rendererRef.current = renderer.current;
    } catch (e) {
      setGlError((e as Error).message);
    }
    return () => {
      renderer.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const r = renderer.current;
    if (!r) return;
    r.setImage(session.li.img, session.w, session.h);
    live.current.forEach((t) => r.deleteTexture(t));
    live.current.clear();
    gpu.current = new WeakMap();
  }, [session]);

  const toPartDraw = useCallback(
    (items: DrawItem[]): PartDraw[] => {
      const r = renderer.current!;
      const used = new Set<WebGLTexture>();
      const out = items.map((it) => {
        const pp = session.prepared.parts[it.partIndex];
        let t = gpu.current.get(pp);
        if (!t) {
          t = r.partTexture(pp.alpha, pp.shade, pp.edgeBg, session.w, session.h);
          gpu.current.set(pp, t);
          live.current.add(t);
        }
        used.add(t);
        return { partTex: t, meanShade: it.meanShade, material: it.material, map: it.map, tuning: it.tuning };
      });
      // free textures of parts that were re-prepared
      const alive = new Set(session.prepared.parts.map((pp) => gpu.current.get(pp)).filter(Boolean) as WebGLTexture[]);
      live.current.forEach((t) => {
        if (!alive.has(t) && !used.has(t)) {
          r.deleteTexture(t);
          live.current.delete(t);
        }
      });
      return out;
    },
    [session],
  );
  if (p.toDraw) p.toDraw.current = toPartDraw;

  const raf = useRef(0);
  const draw = useCallback(() => {
    cancelAnimationFrame(raf.current);
    raf.current = requestAnimationFrame(() => {
      const r = renderer.current;
      if (!r) return;
      const c = canvas.current!;
      if (c.width !== backW || c.height !== backH) {
        c.width = backW;
        c.height = backH;
      }
      r.draw(toPartDraw(p.items), backW, backH);
    });
  }, [p.items, backW, backH, toPartDraw]);

  useEffect(() => {
    const r = renderer.current;
    if (r) r.onTextureReady = draw;
    draw();
  }, [draw, p.version]);

  // ------------------------------------------------------------ overlay (hover / selection / editor masks)
  useEffect(() => {
    const c = overlay.current!;
    const { w, h } = session;
    if (c.width !== w || c.height !== h) {
      c.width = w;
      c.height = h;
    }
    const ctx = c.getContext("2d")!;
    if (p.view === "diff" && p.diff) {
      ctx.putImageData(p.diff, 0, 0);
      return;
    }
    const id = ctx.createImageData(w, h);
    const labels = session.prepared.labels;
    const ed = p.editor;
    const groupOf = session.spec.parts.map((pt) => pt.groupId);
    const cols = PART_COLORS.map(hexRgb);
    const st = stroke.current;
    for (let i = 0; i < labels.length; i++) {
      let l = labels[i];
      if (st && st.mask[i]) l = st.value ? ed!.activePart : -1;
      if (l < 0) continue;
      let a = 0;
      let col = cols[l % cols.length];
      if (ed) {
        a = l === ed.activePart ? 120 : 70;
      } else if (l === p.hoverPart) {
        a = 28;
        col = [255, 255, 255];
      } else if (p.highlightGroup && groupOf[l] === p.highlightGroup && p.view !== "original") {
        // edge only for the selected group
        const x = i % w;
        const edge = x === 0 || x === w - 1 || labels[i - 1] !== l || labels[i + 1] !== l || labels[i - w] !== l || labels[i + w] !== l;
        if (edge) {
          a = 150;
          col = [255, 255, 255];
        }
      }
      if (!a) continue;
      id.data[i * 4] = col[0];
      id.data[i * 4 + 1] = col[1];
      id.data[i * 4 + 2] = col[2];
      id.data[i * 4 + 3] = a;
    }
    ctx.putImageData(id, 0, 0);
  }, [session, p.version, p.hoverPart, p.highlightGroup, p.editor, p.view, p.diff, cursor]);

  // ------------------------------------------------------------ pointer → image coords
  const toImg = (e: React.PointerEvent | React.MouseEvent): [number, number] => {
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    return [((e.clientX - r.left) / r.width) * session.w, ((e.clientY - r.top) / r.height) * session.h];
  };

  const commitMask = (mask: Uint8Array, value: 0 | 1) => {
    const ed = p.editor!;
    const labels = session.prepared.labels.slice();
    const changed = new Set<number>([ed.activePart]);
    for (let i = 0; i < mask.length; i++) {
      if (!mask[i]) continue;
      if (value) {
        if (labels[i] >= 0 && labels[i] !== ed.activePart) changed.add(labels[i]);
        labels[i] = ed.activePart;
      } else if (labels[i] === ed.activePart) labels[i] = -1;
    }
    ed.onLabels(labels, changed);
  };

  const onDown = (e: React.PointerEvent) => {
    const [x, y] = toImg(e);
    const ed = p.editor;
    if (!ed) {
      p.onPickPart(session.partAt(x, y));
      return;
    }
    if (ed.tool === "pick") {
      const l = session.partAt(x, y);
      if (l >= 0) p.onPickPart(l);
      return;
    }
    if (ed.activePart < 0) return;
    if (ed.tool === "smart" || ed.tool === "unsmart") {
      const m = smartSelect(session.li, x, y, { tolerance: ed.tolerance, smoothing: ed.smoothing, edgeStop: 6, ignoreBackground: ed.ignoreBackground });
      commitMask(m, ed.tool === "unsmart" || e.altKey || e.shiftKey ? 0 : 1);
    } else if (ed.tool === "brush" || ed.tool === "erase") {
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
      const mask = new Uint8Array(session.w * session.h);
      const value: 0 | 1 = ed.tool === "brush" && !e.altKey ? 1 : 0;
      const limit = ed.ignoreBackground && value ? notBackdrop(session) : undefined;
      paintCircle(mask, session.w, session.h, x, y, ed.brush, 1, limit);
      stroke.current = { mask, value };
      force((n) => n + 1);
    } else if (ed.tool === "poly") {
      if (polyPts.length >= 3) {
        const [fx, fy] = polyPts[0];
        const near = Math.hypot(fx - x, fy - y) < (8 * session.w) / cssW;
        if (near || e.detail >= 2) return closePoly(e.detail >= 2 ? polyPts : polyPts);
      }
      setPolyPts([...polyPts, [x, y]]);
    } else if (ed.tool === "quad" && ed.quad) {
      const tol = (12 * session.w) / cssW;
      dragHandle.current = ed.quad.findIndex(([qx, qy]) => Math.hypot(qx - x, qy - y) < tol);
      if (dragHandle.current >= 0) (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    }
  };

  const closePoly = (pts: [number, number][]) => {
    const ed = p.editor!;
    const m = rasterizePolygon(pts, session.w, session.h);
    if (ed.ignoreBackground) {
      const bd = session.li.bg.floor;
      for (let i = 0; i < m.length; i++) if (bd[i]) m[i] = 0;
    }
    commitMask(m, 1);
    setPolyPts([]);
  };

  const onMove = (e: React.PointerEvent) => {
    const [x, y] = toImg(e);
    setCursor([x, y]);
    const ed = p.editor;
    if (!ed) {
      const l = session.partAt(x, y);
      if (l !== p.hoverPart) p.onHoverPart(l);
      return;
    }
    if (stroke.current && (ed.tool === "brush" || ed.tool === "erase")) {
      const limit = ed.ignoreBackground && stroke.current.value ? notBackdrop(session) : undefined;
      paintCircle(stroke.current.mask, session.w, session.h, x, y, ed.brush, 1, limit);
      force((n) => n + 1);
    }
    if (ed.tool === "quad" && dragHandle.current >= 0 && ed.quad && ed.onQuad) {
      const q = ed.quad.map((pt) => [...pt]) as [number, number][];
      q[dragHandle.current] = [Math.round(x * 10) / 10, Math.round(y * 10) / 10];
      ed.onQuad(q);
    }
  };

  const onUp = () => {
    if (stroke.current) {
      const s = stroke.current;
      stroke.current = null;
      commitMask(s.mask, s.value);
    }
    dragHandle.current = -1;
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPolyPts([]);
      if (e.key === "Enter" && polyPts.length >= 3 && p.editor) closePoly(polyPts);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });
  useEffect(() => setPolyPts([]), [p.editor?.tool, p.editor?.activePart]);

  const ed = p.editor;
  const scale = cssW / session.w;
  const cursorStyle = !ed ? (p.hoverPart >= 0 ? "pointer" : "default") : ed.tool === "pick" || ed.tool === "quad" ? "default" : "crosshair";

  const svg = useMemo(() => {
    if (!ed) return null;
    return (
      <svg className="stage-svg" viewBox={`0 0 ${session.w} ${session.h}`} style={{ width: cssW, height: cssH }}>
        {ed.tool === "poly" && polyPts.length > 0 && (
          <>
            <polyline points={[...polyPts, ...(cursor ? [cursor] : [])].map((q) => q.join(",")).join(" ")} fill="rgba(255,255,255,0.15)" stroke="#fff" strokeWidth={1.5 / scale} />
            {polyPts.map((q, i) => (
              <circle key={i} cx={q[0]} cy={q[1]} r={(i === 0 ? 5 : 3) / scale} fill={i === 0 ? "#f3a712" : "#fff"} stroke="#000" strokeWidth={0.8 / scale} />
            ))}
          </>
        )}
        {(ed.tool === "brush" || ed.tool === "erase") && cursor && (
          <circle cx={cursor[0]} cy={cursor[1]} r={ed.brush} fill="none" stroke={ed.tool === "erase" ? "#ff6b6b" : "#fff"} strokeWidth={1.2 / scale} />
        )}
        {ed.tool === "quad" && ed.quad && (
          <>
            <polygon points={ed.quad.map((q) => q.join(",")).join(" ")} fill="rgba(243,167,18,0.12)" stroke="#f3a712" strokeWidth={1.5 / scale} />
            <line x1={ed.quad[0][0]} y1={ed.quad[0][1]} x2={ed.quad[1][0]} y2={ed.quad[1][1]} stroke="#fff" strokeWidth={2.5 / scale} />
            {ed.quad.map((q, i) => (
              <g key={i}>
                <circle cx={q[0]} cy={q[1]} r={7 / scale} fill="#f3a712" stroke="#fff" strokeWidth={1.5 / scale} />
                <text x={q[0] + 9 / scale} y={q[1] - 9 / scale} fontSize={11 / scale} fill="#111" stroke="#fff" strokeWidth={3 / scale} paintOrder="stroke">
                  {["TL", "TR", "BR", "BL"][i]}
                </text>
              </g>
            ))}
          </>
        )}
      </svg>
    );
  }, [ed, polyPts, cursor, session, cssW, cssH, scale]);

  const imgStyle = { width: cssW, height: cssH };
  const frameStyle = { width: frameW, height: frameH };
  const splitPx = crop.x0 * fit + split * frameW;
  return (
    <div className="stage" ref={wrap}>
      {glError && <div className="stage-error">Rendering needs WebGL2: {glError}</div>}
      <div className={`stage-inner ${p.view === "side" ? "side" : ""}`}>
        {p.view === "side" && (
          <figure className="pane">
            <div className="canvas-box" style={frameStyle}>
              <div className="layers" style={layerPos}>
                <img src={session.spec.image} style={imgStyle} alt="Original photo" draggable={false} />
              </div>
            </div>
            <figcaption>Original photo</figcaption>
          </figure>
        )}
        <figure className="pane">
          <div className="canvas-box" style={frameStyle}>
            <div
              className="layers"
              style={{ ...layerPos, cursor: cursorStyle }}
              onPointerDown={onDown}
              onPointerMove={onMove}
              onPointerUp={onUp}
              onPointerLeave={() => {
                setCursor(null);
                if (!p.editor) p.onHoverPart(-1);
              }}
              onDoubleClick={() => ed?.tool === "poly" && polyPts.length >= 3 && closePoly(polyPts)}
            >
              <canvas ref={canvas} style={imgStyle} data-testid="render-canvas" />
              {(p.view === "original" || p.view === "split") && (
                <img
                  className="orig-layer"
                  src={session.spec.image}
                  style={{ ...imgStyle, clipPath: p.view === "split" ? `inset(0 0 0 ${splitPx}px)` : undefined }}
                  alt=""
                  draggable={false}
                />
              )}
              <canvas ref={overlay} className="overlay" style={imgStyle} />
              {svg}
            </div>
            {p.view === "split" && (
              <div
                className="split-handle"
                style={{ left: `${split * 100}%` }}
                onPointerDown={(e) => {
                  e.stopPropagation();
                  (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
                }}
                onPointerMove={(e) => {
                  if (!(e.currentTarget as HTMLElement).hasPointerCapture(e.pointerId)) return;
                  const r = (e.currentTarget.parentElement as HTMLElement).getBoundingClientRect();
                  setSplit(Math.min(0.98, Math.max(0.02, (e.clientX - r.left) / r.width)));
                }}
              >
                <span>Customised ◂ ▸ Original</span>
              </div>
            )}
            {p.view === "original" && <span className="badge-orig">Original photo</span>}
          </div>
          {p.view === "side" && <figcaption>Customised</figcaption>}
        </figure>
      </div>
    </div>
  );
}

const nb = new WeakMap<ProductSession, Uint8Array>();
function notBackdrop(s: ProductSession) {
  let m = nb.get(s);
  if (!m) {
    m = new Uint8Array(s.w * s.h);
    for (let i = 0; i < m.length; i++) m[i] = s.li.bg.floor[i] ? 0 : 1;
    nb.set(s, m);
  }
  return m;
}
