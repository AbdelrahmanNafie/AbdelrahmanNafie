/**
 * WebGL2 compositor.
 *
 * Model (per pixel of a customisable part):
 *   lighting  L = smoothed photo luminance / group mean     (old pattern removed, shape & light kept)
 *   detail    D = (photo luminance / smoothed luminance)^k   (seams, creases, stitching)
 *   diffuse   = albedo(new material, physically scaled & oriented) * softclip(L * D)
 *   highlight = photographed highlight energy above the diffuse knee, re-tinted and
 *               modulated by the new material's gloss / relief / metalness
 * Output is composited over the untouched photo with an edge-aware soft alpha, so the
 * silhouette, the background and every non-customised pixel stay byte-identical.
 */
import type { Material, RenderTuning } from "../types";
import type { Mat3 } from "./mapping";

const VERT = `#version 300 es
in vec2 aPos;
out vec2 vUv;
void main() {
  vUv = vec2(aPos.x * 0.5 + 0.5, 0.5 - aPos.y * 0.5);
  gl_Position = vec4(aPos, 0.0, 1.0);
}`;

const FRAG_BASE = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D uOrig;
out vec4 o;
void main() { o = vec4(texture(uOrig, vUv).rgb, 1.0); }`;

const FRAG_PART = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D uOrig;
uniform sampler2D uPart;   // r = soft alpha, g = smoothed linear luminance, b = backdrop luminance on silhouette edges, a = edge weight
uniform sampler2D uTex;    // material albedo (sRGB)
uniform vec2 uImgSize;
uniform mat3 uMap;
uniform float uMeanShade;
uniform float uTexMeanLum;
uniform int uKind;         // 0 fabric 1 leather 2 wood 3 metal 4 stone 5 chrome 6 glass
uniform float uSpec, uGloss, uSheen, uRelief, uMetallic, uOpacity, uAniso;
uniform float uContrast, uHighlights, uDetail, uExposure;
out vec4 o;

const vec3 LUM = vec3(0.2126, 0.7152, 0.0722);
vec3 toLin(vec3 c) { return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(0.04045, c)); }
vec3 toSrgb(vec3 c) { c = max(c, 0.0); return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(0.0031308, c)); }

void main() {
  vec4 pd = texture(uPart, vUv);
  float alpha = pd.r;
  if (alpha < 0.002) discard;
  vec2 px = vUv * uImgSize;
  vec3 orig = toLin(texture(uOrig, vUv).rgb);
  float Y = dot(orig, LUM);
  float S = max(pd.g, 1e-4);

  float light = pow(S / max(uMeanShade, 1e-4), uContrast);
  float detail = pow(clamp(Y / S, 0.15, 4.0), uDetail);
  float lit = light * detail;

  // diffuse / highlight split: soft knee depends on gloss
  float knee = mix(1.75, 1.3, uGloss);
  float diff = lit < knee ? lit : knee + (lit - knee) * 0.12;
  float hiAbs = max(lit - knee, 0.0) * uMeanShade;

  vec3 t = uMap * vec3(px, 1.0);
  vec2 tuv = t.xy / t.z;
  vec3 A = toLin(texture(uTex, tuv).rgb);
  float aL = max(dot(A, LUM), 1e-4);
  float relief = mix(1.0, aL / max(uTexMeanLum, 1e-4), uRelief);
  vec3 specTint = mix(vec3(1.0), A / aL * 0.9, uMetallic);

  vec3 col;
  if (uKind == 5) {
    // chrome: high-contrast remap of the photographed lighting, cool neutral tint
    float c = pow(clamp(lit * 0.55, 0.0, 1.6), 1.7);
    col = mix(vec3(0.025, 0.027, 0.03), vec3(0.93, 0.95, 0.98), clamp(c, 0.0, 1.0)) + hiAbs * 3.0 * uHighlights;
  } else if (uKind == 6) {
    // glass in a white studio: mostly the backdrop, tinted, plus edges and highlights
    vec3 back = vec3(0.92) * (A / max(aL, 1e-3)) * 0.95;
    vec3 body = A * diff * 0.55;
    col = mix(back, body, uOpacity) * mix(1.0, detail, 0.6) + hiAbs * uHighlights * 1.5;
  } else {
    if (uKind == 0) {
      // fabric scatters light: compress the lighting range, lift the lit side slightly
      diff = pow(diff, 1.0 - 0.3 * uSheen) * (1.0 + 0.06 * uSheen);
    }
    float glossShape = mix(0.6, 1.4, uGloss);
    float aniso = mix(1.0, aL / max(uTexMeanLum, 1e-4) * aL / max(uTexMeanLum, 1e-4), uAniso);
    col = A * diff * uExposure + hiAbs * uHighlights * uSpec * glossShape * relief * aniso * specTint;
    if (uKind == 3) {
      // metals: reflectance rises with lighting (cheap fresnel-ish lift on lit faces)
      col += A * max(light - 1.0, 0.0) * 0.25 * uMetallic;
    }
  }
  // Silhouette edge pixels are composited over the local backdrop instead of the old finish.
  // b = backdrop luminance * w, a = edge weight w (both interpolate linearly, so b/a stays exact).
  // Emulate  w * mix(bg, col, alpha) + (1-w) * [alpha-blend over the photo]  with one blend equation.
  vec3 c = toSrgb(col);
  float w = clamp(pd.a, 0.0, 1.0);
  if (w > 0.001) {
    vec3 E = toSrgb(mix(vec3(pd.b / w), col, alpha));
    float a2 = 1.0 - (1.0 - w) * (1.0 - alpha);
    o = vec4((w * E + (1.0 - w) * alpha * c) / a2, a2);
  } else {
    o = vec4(c, alpha);
  }
}`;

export interface PartDraw {
  partTex: WebGLTexture;
  meanShade: number;
  material: Material;
  map: Mat3;
  tuning: RenderTuning;
}

const KIND: Record<string, number> = { fabric: 0, leather: 1, wood: 2, metal: 3, stone: 4, chrome: 5, glass: 6 };

export class Renderer {
  readonly gl: WebGL2RenderingContext;
  private base: WebGLProgram;
  private part: WebGLProgram;
  private vao: WebGLVertexArrayObject;
  private origTex: WebGLTexture | null = null;
  private texCache = new Map<string, { tex: WebGLTexture; ready: boolean }>();
  private aniso: number = 1;
  private anisoExt: EXT_texture_filter_anisotropic | null;
  imgW = 1;
  imgH = 1;
  onTextureReady?: () => void;

  constructor(readonly canvas: HTMLCanvasElement) {
    const gl = canvas.getContext("webgl2", { premultipliedAlpha: false, antialias: false, preserveDrawingBuffer: true });
    if (!gl) throw new Error("WebGL2 is not available in this browser.");
    this.gl = gl;
    this.anisoExt = gl.getExtension("EXT_texture_filter_anisotropic");
    if (this.anisoExt) this.aniso = Math.min(8, gl.getParameter(this.anisoExt.MAX_TEXTURE_MAX_ANISOTROPY_EXT));
    this.base = this.program(VERT, FRAG_BASE);
    this.part = this.program(VERT, FRAG_PART);
    this.vao = gl.createVertexArray()!;
    gl.bindVertexArray(this.vao);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    for (const p of [this.base, this.part]) {
      const loc = gl.getAttribLocation(p, "aPos");
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    }
  }

  private program(vs: string, fs: string) {
    const gl = this.gl;
    const mk = (type: number, src: string) => {
      const s = gl.createShader(type)!;
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) ?? "shader error");
      return s;
    };
    const p = gl.createProgram()!;
    gl.attachShader(p, mk(gl.VERTEX_SHADER, vs));
    gl.attachShader(p, mk(gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p) ?? "link error");
    return p;
  }

  setImage(img: TexImageSource, w: number, h: number) {
    const gl = this.gl;
    if (this.origTex) gl.deleteTexture(this.origTex);
    const t = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE, img);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    this.origTex = t;
    this.imgW = w;
    this.imgH = h;
  }

  /** Upload (or replace) a part's alpha + lighting + edge backdrop as an RGBA16F texture. */
  partTexture(alpha: Float32Array, shade: Float32Array, edgeBg: Float32Array, w: number, h: number, existing?: WebGLTexture): WebGLTexture {
    const gl = this.gl;
    const data = new Float32Array(w * h * 4);
    for (let i = 0; i < w * h; i++) {
      data[i * 4] = alpha[i];
      data[i * 4 + 1] = shade[i];
      data[i * 4 + 2] = edgeBg[i];
      data[i * 4 + 3] = edgeBg[i] > 0 ? 1 : 0;
    }
    const t = existing ?? gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA16F, w, h, 0, gl.RGBA, gl.FLOAT, data);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return t;
  }

  deleteTexture(t: WebGLTexture) {
    this.gl.deleteTexture(t);
  }

  /** Material textures are cached; a flat average-colour texture is used until the image arrives. */
  materialTexture(m: Material): { tex: WebGLTexture; ready: boolean } {
    const hit = this.texCache.get(m.texture);
    if (hit) return hit;
    const gl = this.gl;
    const tex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, tex);
    const hex = m.avgColor.replace("#", "");
    const px = new Uint8Array([parseInt(hex.slice(0, 2), 16), parseInt(hex.slice(2, 4), 16), parseInt(hex.slice(4, 6), 16), 255]);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, px);
    const entry = { tex, ready: false };
    this.texCache.set(m.texture, entry);
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => {
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE, img);
      gl.generateMipmap(gl.TEXTURE_2D);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.REPEAT);
      if (this.anisoExt) gl.texParameterf(gl.TEXTURE_2D, this.anisoExt.TEXTURE_MAX_ANISOTROPY_EXT, this.aniso);
      entry.ready = true;
      this.onTextureReady?.();
    };
    img.src = m.texture;
    return entry;
  }

  /** Resolves once every listed material texture is uploaded (used before exports). */
  async ensureTextures(ms: Material[]) {
    ms.forEach((m) => this.materialTexture(m));
    for (let i = 0; i < 200; i++) {
      if (ms.every((m) => this.texCache.get(m.texture)?.ready)) return;
      await new Promise((r) => setTimeout(r, 25));
    }
  }

  /** Draw to the bound framebuffer (null = canvas) at the given size. */
  draw(parts: PartDraw[], width: number, height: number, framebuffer: WebGLFramebuffer | null = null) {
    const gl = this.gl;
    if (!this.origTex) return;
    gl.bindFramebuffer(gl.FRAMEBUFFER, framebuffer);
    gl.viewport(0, 0, width, height);
    gl.bindVertexArray(this.vao);
    gl.disable(gl.BLEND);
    gl.useProgram(this.base);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.origTex);
    gl.uniform1i(gl.getUniformLocation(this.base, "uOrig"), 0);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);

    gl.enable(gl.BLEND);
    gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    const p = this.part;
    gl.useProgram(p);
    const u = (n: string) => gl.getUniformLocation(p, n);
    gl.uniform1i(u("uOrig"), 0);
    gl.uniform1i(u("uPart"), 1);
    gl.uniform1i(u("uTex"), 2);
    gl.uniform2f(u("uImgSize"), this.imgW, this.imgH);
    for (const d of parts) {
      const r = d.material.render;
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, d.partTex);
      gl.activeTexture(gl.TEXTURE2);
      gl.bindTexture(gl.TEXTURE_2D, this.materialTexture(d.material).tex);
      gl.uniformMatrix3fv(u("uMap"), true, d.map);
      gl.uniform1f(u("uMeanShade"), d.meanShade);
      const [lr, lg, lb] = d.material.avgLinear;
      gl.uniform1f(u("uTexMeanLum"), 0.2126 * lr + 0.7152 * lg + 0.0722 * lb);
      gl.uniform1i(u("uKind"), KIND[r.kind] ?? 2);
      gl.uniform1f(u("uSpec"), r.spec);
      gl.uniform1f(u("uGloss"), r.gloss);
      gl.uniform1f(u("uSheen"), r.sheen);
      gl.uniform1f(u("uRelief"), r.relief);
      gl.uniform1f(u("uMetallic"), r.metallic ?? 0);
      gl.uniform1f(u("uOpacity"), r.opacity ?? 1);
      gl.uniform1f(u("uAniso"), r.aniso ?? 0);
      gl.uniform1f(u("uContrast"), d.tuning.contrast);
      gl.uniform1f(u("uHighlights"), d.tuning.highlights);
      gl.uniform1f(u("uDetail"), r.kind === "chrome" ? Math.max(d.tuning.detail, 0.8) : d.tuning.detail);
      gl.uniform1f(u("uExposure"), d.tuning.exposure);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }
    gl.disable(gl.BLEND);
  }

  /** Off-screen render at any resolution; returns top-down RGBA pixels. */
  renderToPixels(parts: PartDraw[], width: number, height: number): Uint8ClampedArray {
    const gl = this.gl;
    const fb = gl.createFramebuffer()!;
    const tex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, width, height, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    this.draw(parts, width, height, fb);
    const raw = new Uint8Array(width * height * 4);
    gl.readPixels(0, 0, width, height, gl.RGBA, gl.UNSIGNED_BYTE, raw);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.deleteFramebuffer(fb);
    gl.deleteTexture(tex);
    // readPixels returns rows bottom-up (row 0 = clip-space bottom = image bottom): flip to top-down
    const out = new Uint8ClampedArray(raw.length);
    const row = width * 4;
    for (let y = 0; y < height; y++) out.set(raw.subarray((height - 1 - y) * row, (height - y) * row), y * row);
    return out;
  }
}
