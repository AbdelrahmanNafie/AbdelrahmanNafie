import type { Mapping } from "../types";

/** Row-major 3x3. */
export type Mat3 = [number, number, number, number, number, number, number, number, number];

export const mul3 = (a: Mat3, b: Mat3): Mat3 => {
  const r = new Array(9).fill(0) as Mat3;
  for (let i = 0; i < 3; i++)
    for (let j = 0; j < 3; j++) r[i * 3 + j] = a[i * 3] * b[j] + a[i * 3 + 1] * b[3 + j] + a[i * 3 + 2] * b[6 + j];
  return r;
};

/** Solve A x = b (n x n) with partial pivoting. */
function solve(A: number[][], b: number[]): number[] {
  const n = b.length;
  const M = A.map((row, i) => [...row, b[i]]);
  for (let c = 0; c < n; c++) {
    let p = c;
    for (let r = c + 1; r < n; r++) if (Math.abs(M[r][c]) > Math.abs(M[p][c])) p = r;
    [M[c], M[p]] = [M[p], M[c]];
    const d = M[c][c] || 1e-12;
    for (let r = 0; r < n; r++) {
      if (r === c) continue;
      const f = M[r][c] / d;
      for (let k = c; k <= n; k++) M[r][k] -= f * M[c][k];
    }
  }
  return M.map((row, i) => row[n] / (row[i] || 1e-12));
}

/** Homography mapping src[i] -> dst[i] (4 point correspondences). */
export function homography(src: [number, number][], dst: [number, number][]): Mat3 {
  const A: number[][] = [];
  const b: number[] = [];
  for (let i = 0; i < 4; i++) {
    const [x, y] = src[i];
    const [u, v] = dst[i];
    A.push([x, y, 1, 0, 0, 0, -u * x, -u * y]);
    b.push(u);
    A.push([0, 0, 0, x, y, 1, -v * x, -v * y]);
    b.push(v);
  }
  const h = solve(A, b);
  return [h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7], 1];
}

/**
 * Image pixel -> texture tile coordinates (1.0 = one physical tile).
 * The returned matrix is projective; the shader divides by w.
 */
export function mappingMatrix(m: Mapping, pxPerCm: number, tileCm: number): Mat3 {
  const tile = Math.max(0.01, tileCm * (m.scale || 1));
  const a = (-m.angle * Math.PI) / 180;
  const c = Math.cos(a);
  const s = Math.sin(a);
  const [ox, oy] = m.offset ?? [0, 0];
  // rotate + scale + offset in tile space
  const RS: Mat3 = [c / tile, -s / tile, ox, s / tile, c / tile, oy, 0, 0, 1];
  if (m.mode === "planar") {
    const toCm: Mat3 = [1 / pxPerCm, 0, 0, 0, 1 / pxPerCm, 0, 0, 0, 1];
    return mul3(RS, toCm);
  }
  const [W, H] = m.sizeCm;
  const Hm = homography(m.quad, [
    [0, 0],
    [W, 0],
    [W, H],
    [0, H],
  ]);
  return mul3(RS, Hm);
}
