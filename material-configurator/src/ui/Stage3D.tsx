import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Studio3D, type Part3D } from "../engine/studio3d";
import type { Material, ProductSpec, Selection } from "../types";

interface Props {
  spec: ProductSpec;
  selection: Selection;
  materials: Map<string, Material>;
  onPickGroup: (groupId: string) => void;
  onSlots?: (slots: { mesh: string; material: string }[], widthCm: number) => void;
  studioRef: React.MutableRefObject<Studio3D | null>;
}

export function parts3d(spec: ProductSpec): Part3D[] {
  return spec.parts.map((p) => ({ id: p.id, name: p.name, groupId: p.groupId, meshes: p.meshes ?? [] }));
}

/** Interactive 3D view: drag to rotate, scroll to zoom, click a surface to choose that part. */
export function Stage3D(p: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const down = useRef<[number, number] | null>(null);

  useEffect(() => {
    try {
      p.studioRef.current = new Studio3D(canvas.current!);
      const el = wrap.current!;
      p.studioRef.current.resize(el.clientWidth, el.clientHeight);
    } catch (e) {
      setError((e as Error).message);
    }
    return () => {
      p.studioRef.current?.dispose();
      p.studioRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useLayoutEffect(() => {
    const el = wrap.current!;
    const ro = new ResizeObserver(() => p.studioRef.current?.resize(el.clientWidth, el.clientHeight));
    ro.observe(el);
    p.studioRef.current?.resize(el.clientWidth, el.clientHeight);
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const st = p.studioRef.current;
    if (!st || !p.spec.model) return;
    setLoading(true);
    st.load(p.spec.model)
      .then(({ widthCm }) => {
        if (!p.spec.parts.length) p.onSlots?.(st.slots(), widthCm);
        return st.apply(parts3d(p.spec), p.selection, p.materials, p.spec.groups);
      })
      .catch((e) => setError(`Could not open the 3D model: ${(e as Error).message}`))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.spec.model]);

  useEffect(() => {
    if (loading) return;
    p.studioRef.current?.apply(parts3d(p.spec), p.selection, p.materials, p.spec.groups);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.selection, p.materials, p.spec.parts, loading]);

  return (
    <div className="stage3d" ref={wrap}>
      <canvas
        ref={canvas}
        data-testid="canvas3d"
        onPointerDown={(e) => (down.current = [e.clientX, e.clientY])}
        onPointerUp={(e) => {
          const d = down.current;
          down.current = null;
          if (!d || Math.hypot(e.clientX - d[0], e.clientY - d[1]) > 4) return; // it was a drag (rotate)
          const hit = p.studioRef.current?.meshAt(e.clientX, e.clientY);
          const part = hit && p.spec.parts.find((x) => x.meshes?.includes(hit.mesh) || x.meshes?.includes(hit.material));
          if (part) p.onPickGroup(part.groupId);
        }}
      />
      {loading && <div className="loading">Loading 3D model…</div>}
      {error && <div className="stage-error">{error}</div>}
      <div className="hint3d">Drag to rotate · scroll to zoom · click a part to choose it</div>
    </div>
  );
}
