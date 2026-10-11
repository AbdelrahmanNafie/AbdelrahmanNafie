/**
 * 3D studio: renders a real 3D product model (glTF/GLB) with physically based materials
 * built from the Mobica swatch scans, in a white photo studio. Two quality levels:
 *  - interactive: rasterised PBR (instant, rotatable)
 *  - studio render: progressive GPU path tracing (three-gpu-pathtracer) for export — the same
 *    light transport a desktop renderer uses: soft shadows, inter-reflections, true fabric sheen.
 */
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { WebGLPathTracer } from "three-gpu-pathtracer";
import { HorizontalBlurShader } from "three/examples/jsm/shaders/HorizontalBlurShader.js";
import { VerticalBlurShader } from "three/examples/jsm/shaders/VerticalBlurShader.js";
import type { Material, MaterialGroup } from "../types";

export interface Part3D {
  id: string;
  name: string;
  groupId: string;
  /** mesh names (or glTF material names) this part covers */
  meshes: string[];
}

/** Physical surface parameters per Mobica category / concept finish. */
function surfaceFor(m: Material) {
  const k = m.render.kind;
  if (k === "fabric") return { roughness: 0.92, metalness: 0, sheen: 0.9, sheenRoughness: 0.55, clearcoat: 0, normal: 1.4 };
  if (k === "leather") return { roughness: m.category === "leather-natural" ? 0.5 : 0.42, metalness: 0, sheen: 0, sheenRoughness: 0, clearcoat: 0.15, normal: 0.3 };
  if (k === "wood") return { roughness: 0.42, metalness: 0, sheen: 0, sheenRoughness: 0, clearcoat: 0.25, normal: 0.25 };
  if (k === "stone") return { roughness: 0.12, metalness: 0, sheen: 0, sheenRoughness: 0, clearcoat: 0.7, normal: 0 };
  if (k === "chrome") return { roughness: 0.06, metalness: 1, sheen: 0, sheenRoughness: 0, clearcoat: 0, normal: 0 };
  if (k === "glass") return { roughness: m.render.opacity && m.render.opacity > 0.4 ? 0.35 : 0.02, metalness: 0, sheen: 0, sheenRoughness: 0, clearcoat: 0, normal: 0 };
  // metal: powder coat (paint), metallic for 9006 / 8519-1 / brushed nickel
  const metallic = m.render.metallic ?? 0;
  return { roughness: m.render.aniso ? 0.32 : metallic > 0 ? 0.38 : 0.45, metalness: metallic, sheen: 0, sheenRoughness: 0, clearcoat: metallic > 0.5 ? 0 : 0.2, normal: 0.15 };
}

/** A soft white photo studio as an equirectangular HDR: bright ceiling softbox, two side softboxes, white cyclorama. */
function studioEnvironment(): THREE.DataTexture {
  const W = 512;
  const H = 256;
  const data = new Float32Array(W * H * 4);
  const box = (u: number, v: number, cu: number, cv: number, su: number, sv: number) => {
    const du = Math.min(Math.abs(u - cu), 1 - Math.abs(u - cu)) / su;
    const dv = (v - cv) / sv;
    const d = Math.max(Math.abs(du), Math.abs(dv));
    return d < 1 ? 1 : Math.exp(-(d - 1) * 8);
  };
  for (let y = 0; y < H; y++)
    for (let x = 0; x < W; x++) {
      const u = x / W;
      const v = y / H; // 0 = top
      let L = 0.55 + 0.25 * (1 - v); // cyclorama, brighter above
      if (v > 0.55) L *= 0.85; // floor bounce
      L += 4.5 * box(u, v, 0.5, 0.08, 0.5, 0.08); // overhead softbox
      L += 2.6 * box(u, v, 0.38, 0.3, 0.12, 0.16); // key softbox front-left
      L += 1.3 * box(u, v, 0.68, 0.32, 0.1, 0.14); // fill softbox right
      const i = (y * W + x) * 4;
      data[i] = L;
      data[i + 1] = L;
      data[i + 2] = L;
      data[i + 3] = 1;
    }
  const t = new THREE.DataTexture(data, W, H, THREE.RGBAFormat, THREE.FloatType);
  t.mapping = THREE.EquirectangularReflectionMapping;
  t.colorSpace = THREE.LinearSRGBColorSpace;
  t.magFilter = THREE.LinearFilter;
  t.minFilter = THREE.LinearFilter;
  t.needsUpdate = true;
  return t;
}

/** Normal map from a swatch scan: luminance as height, Sobel gradient → tangent-space normal. */
function normalMapFrom(img: HTMLImageElement): THREE.CanvasTexture {
  const s = Math.min(512, img.naturalWidth);
  const c = document.createElement("canvas");
  c.width = s;
  c.height = s;
  const ctx = c.getContext("2d", { willReadFrequently: true })!;
  ctx.filter = "blur(0.6px)";
  ctx.drawImage(img, 0, 0, s, s);
  const src = ctx.getImageData(0, 0, s, s).data;
  const hgt = new Float32Array(s * s);
  let mean = 0;
  for (let i = 0; i < s * s; i++) mean += hgt[i] = (0.2126 * src[i * 4] + 0.7152 * src[i * 4 + 1] + 0.0722 * src[i * 4 + 2]) / 255;
  mean /= s * s;
  const out = ctx.createImageData(s, s);
  const at = (x: number, y: number) => hgt[((y + s) % s) * s + ((x + s) % s)];
  const k = 2.2 / Math.max(0.08, mean);
  for (let y = 0; y < s; y++)
    for (let x = 0; x < s; x++) {
      const dx = (at(x + 1, y - 1) + 2 * at(x + 1, y) + at(x + 1, y + 1) - at(x - 1, y - 1) - 2 * at(x - 1, y) - at(x - 1, y + 1)) * k;
      const dy = (at(x - 1, y + 1) + 2 * at(x, y + 1) + at(x + 1, y + 1) - at(x - 1, y - 1) - 2 * at(x, y - 1) - at(x + 1, y - 1)) * k;
      const n = new THREE.Vector3(-dx, dy, 1).normalize();
      const i = (y * s + x) * 4;
      out.data[i] = (n.x * 0.5 + 0.5) * 255;
      out.data[i + 1] = (n.y * 0.5 + 0.5) * 255;
      out.data[i + 2] = (n.z * 0.5 + 0.5) * 255;
      out.data[i + 3] = 255;
    }
  ctx.putImageData(out, 0, 0);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.NoColorSpace;
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  return t;
}

/** Real-world centimetres covered by one unit of UV space on this mesh (so textures get their true size). */
function cmPerUv(mesh: THREE.Mesh): number {
  const g = mesh.geometry as THREE.BufferGeometry;
  const pos = g.attributes.position as THREE.BufferAttribute;
  const uv = g.attributes.uv as THREE.BufferAttribute;
  if (!pos || !uv) return 100;
  const idx = g.index;
  const n = idx ? idx.count : pos.count;
  mesh.updateWorldMatrix(true, false);
  const s = new THREE.Vector3();
  mesh.matrixWorld.decompose(new THREE.Vector3(), new THREE.Quaternion(), s);
  const scale = (Math.abs(s.x) + Math.abs(s.y) + Math.abs(s.z)) / 3;
  const a = new THREE.Vector3(), b = new THREE.Vector3(), c = new THREE.Vector3();
  const ua = new THREE.Vector2(), ub = new THREE.Vector2(), uc = new THREE.Vector2();
  let world = 0, uvA = 0;
  const step = Math.max(3, Math.floor(n / 3 / 4000) * 3);
  for (let i = 0; i + 2 < n; i += step) {
    const i0 = idx ? idx.getX(i) : i, i1 = idx ? idx.getX(i + 1) : i + 1, i2 = idx ? idx.getX(i + 2) : i + 2;
    a.fromBufferAttribute(pos, i0); b.fromBufferAttribute(pos, i1); c.fromBufferAttribute(pos, i2);
    ua.fromBufferAttribute(uv, i0); ub.fromBufferAttribute(uv, i1); uc.fromBufferAttribute(uv, i2);
    world += b.clone().sub(a).cross(c.clone().sub(a)).length() / 2;
    uvA += Math.abs((ub.x - ua.x) * (uc.y - ua.y) - (uc.x - ua.x) * (ub.y - ua.y)) / 2;
  }
  if (uvA < 1e-9) return 100;
  return Math.sqrt(world / uvA) * scale * 100; // glTF units are metres
}

export class Studio3D {
  readonly renderer: THREE.WebGLRenderer;
  readonly scene = new THREE.Scene();
  readonly camera = new THREE.PerspectiveCamera(30, 1, 0.05, 100);
  readonly controls: OrbitControls;
  private model: THREE.Object3D | null = null;
  private meshes: THREE.Mesh[] = [];
  private original = new Map<THREE.Mesh, THREE.Material | THREE.Material[]>();
  private texCache = new Map<string, Promise<{ map: THREE.Texture; normal: THREE.Texture }>>();
  private floor: THREE.Mesh;
  private key: THREE.DirectionalLight;
  private contact: ContactShadow;
  private raf = 0;
  private dirty = true;
  onChange?: () => void;

  constructor(readonly canvas: HTMLCanvasElement) {
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true, alpha: false });
    this.renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    // Khronos PBR Neutral: tone mapping designed for e-commerce product colour fidelity
    this.renderer.toneMapping = THREE.NeutralToneMapping;
    this.renderer.toneMappingExposure = 1.0;

    this.scene.background = new THREE.Color(0xffffff);
    const env = studioEnvironment();
    const pmrem = new THREE.PMREMGenerator(this.renderer);
    this.scene.environment = pmrem.fromEquirectangular(env).texture;
    this.scene.userData.equirect = env;

    this.key = new THREE.DirectionalLight(0xffffff, 1.6);
    this.key.position.set(-2.5, 5, 3.5);
    this.scene.add(this.key);

    // studio floor: only used by the path tracer (rasterised view uses the contact shadow)
    // a white studio sweep: slightly self-lit so it photographs white while still catching soft shadows
    this.floor = new THREE.Mesh(
      new THREE.PlaneGeometry(200, 200),
      new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 1, emissive: 0xffffff, emissiveIntensity: 0.38 }),
    );
    this.floor.rotation.x = -Math.PI / 2;
    this.floor.visible = false;
    this.scene.add(this.floor);
    this.contact = new ContactShadow(this.renderer);
    this.scene.add(this.contact.group);

    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true;
    this.controls.maxPolarAngle = Math.PI / 2 - 0.05;
    this.controls.addEventListener("change", () => this.invalidate());
    const loop = () => {
      this.raf = requestAnimationFrame(loop);
      this.controls.update();
      if (this.dirty) {
        this.dirty = false;
        this.renderer.render(this.scene, this.camera);
      }
    };
    loop();
  }

  private updateShadow() {
    if (this.model) this.contact.update(this.scene, this.model);
  }

  private fitRadius = 1;
  private fitDistance() {
    const vfov = (this.camera.fov * Math.PI) / 180;
    const hfov = 2 * Math.atan(Math.tan(vfov / 2) * Math.max(0.5, this.camera.aspect));
    return (this.fitRadius / Math.sin(Math.min(vfov, hfov) / 2)) * 0.88;
  }

  invalidate() {
    this.dirty = true;
  }

  resize(w: number, h: number) {
    if (!w || !h) return;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    if (this.model) {
      const dir = this.camera.position.clone().sub(this.controls.target).normalize();
      this.camera.position.copy(this.controls.target).addScaledVector(dir, this.fitDistance());
    }
    this.invalidate();
  }

  async load(url: string) {
    const gltf = await new GLTFLoader().loadAsync(url);
    if (this.model) this.scene.remove(this.model);
    this.model = gltf.scene;
    this.meshes = [];
    this.original.clear();
    this.model.traverse((o) => {
      const m = o as THREE.Mesh;
      if (m.isMesh) {
        m.castShadow = true;
        m.receiveShadow = true;
        this.meshes.push(m);
        this.original.set(m, m.material);
      }
      if ((o as THREE.Light).isLight) o.visible = false; // we light the studio ourselves
    });
    // sit on the floor, centred
    const box = new THREE.Box3().setFromObject(this.model);
    const c = box.getCenter(new THREE.Vector3());
    this.model.position.sub(new THREE.Vector3(c.x, box.min.y, c.z));
    this.scene.add(this.model);
    const size = box.getSize(new THREE.Vector3());
    const r = size.length() / 2;
    this.fitRadius = r;
    this.controls.target.set(0, size.y * 0.45, 0);
    // classic 3/4 catalogue view, distance chosen so the bounding sphere fills ~85% of the frame
    const dir = new THREE.Vector3(0.55, 0.32, 1).normalize();
    this.camera.position.copy(this.controls.target).addScaledVector(dir, this.fitDistance());
    this.camera.near = r / 50;
    this.camera.far = r * 40;
    this.camera.updateProjectionMatrix();
    this.contact.resize(Math.max(size.x, size.z) * 1.8);
    this.updateShadow();
    this.invalidate();
    return { widthCm: Math.round(Math.max(size.x, size.z) * 100) };
  }

  /** Names of meshes / glTF materials, used to build parts for an uploaded model. */
  slots(): { mesh: string; material: string }[] {
    return this.meshes.map((m) => {
      const o = this.original.get(m)!;
      return { mesh: m.name, material: (Array.isArray(o) ? o[0] : o).name };
    });
  }

  meshAt(clientX: number, clientY: number): { mesh: string; material: string } | null {
    const r = this.canvas.getBoundingClientRect();
    const ray = new THREE.Raycaster();
    ray.setFromCamera(new THREE.Vector2(((clientX - r.left) / r.width) * 2 - 1, -((clientY - r.top) / r.height) * 2 + 1), this.camera);
    const hit = ray.intersectObjects(this.meshes, false)[0];
    if (!hit) return null;
    const m = hit.object as THREE.Mesh;
    const orig = this.original.get(m)!;
    return { mesh: m.name, material: (Array.isArray(orig) ? orig[0] : orig).name };
  }

  private textures(m: Material) {
    let p = this.texCache.get(m.id);
    if (!p) {
      p = new Promise((res, rej) => {
        const img = new Image();
        img.crossOrigin = "anonymous";
        img.onload = () => {
          const map = new THREE.Texture(img);
          map.colorSpace = THREE.SRGBColorSpace;
          map.wrapS = map.wrapT = THREE.RepeatWrapping;
          map.anisotropy = this.renderer.capabilities.getMaxAnisotropy();
          map.needsUpdate = true;
          res({ map, normal: normalMapFrom(img) });
        };
        img.onerror = rej;
        img.src = m.texture;
      });
      this.texCache.set(m.id, p);
    }
    return p;
  }

  /** Apply the selection: parts without a material keep the model's original material. */
  async apply(parts: Part3D[], selection: Record<string, string | undefined>, materials: Map<string, Material>, groups: MaterialGroup[]) {
    void groups;
    for (const part of parts) {
      const mid = selection[part.groupId];
      const mat = mid ? materials.get(mid) : undefined;
      const targets = this.meshes.filter((x) => {
        const o = this.original.get(x)!;
        return part.meshes.includes(x.name) || part.meshes.includes((Array.isArray(o) ? o[0] : o).name);
      });
      for (const mesh of targets) {
        if (!mat) {
          mesh.material = this.original.get(mesh)!;
          continue;
        }
        const { map, normal } = await this.textures(mat);
        const s = surfaceFor(mat);
        const rep = cmPerUv(mesh) / Math.max(1, mat.tileCm);
        const mp = map.clone();
        const nm = normal.clone();
        mp.repeat.set(rep, rep);
        nm.repeat.set(rep, rep);
        mp.needsUpdate = nm.needsUpdate = true;
        const isGlass = mat.render.kind === "glass";
        const pm = new THREE.MeshPhysicalMaterial({
          name: mat.id,
          map: mp,
          normalMap: s.normal ? nm : null,
          normalScale: new THREE.Vector2(s.normal, s.normal),
          roughness: s.roughness,
          metalness: s.metalness,
          clearcoat: s.clearcoat,
          clearcoatRoughness: 0.35,
          sheen: s.sheen,
          sheenRoughness: s.sheenRoughness,
          sheenColor: s.sheen ? new THREE.Color(mat.avgColor).lerp(new THREE.Color(0xffffff), 0.35) : new THREE.Color(0),
          transmission: isGlass ? 1 : 0,
          thickness: isGlass ? 0.01 : 0,
          ior: isGlass ? 1.5 : 1.5,
          envMapIntensity: 1,
        });
        if (mat.render.aniso) {
          pm.anisotropy = 0.8;
        }
        mesh.material = pm;
      }
    }
    this.updateShadow();
    this.invalidate();
    this.onChange?.();
  }

  /**
   * Final-quality still: progressive path tracing at the requested size, white studio, soft
   * contact shadow. Returns a canvas with the finished image.
   */
  async renderStill(width: number, height: number, samples: number, onProgress?: (f: number) => void): Promise<HTMLCanvasElement> {
    const prevSize = this.renderer.getSize(new THREE.Vector2());
    const prevPR = this.renderer.getPixelRatio();
    cancelAnimationFrame(this.raf);
    this.renderer.setPixelRatio(1);
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    // the path tracer needs a real white floor (shadow-only materials do not exist in path tracing)
    this.floor.visible = true;
    this.contact.group.visible = false;
    const envPrev = this.scene.environment;
    this.scene.environment = this.scene.userData.equirect;
    const out = document.createElement("canvas");
    out.width = width;
    out.height = height;
    try {
      const tracer = new WebGLPathTracer(this.renderer);
      tracer.bounces = 6;
      tracer.filterGlossyFactor = 0.4;
      tracer.tiles.set(1, 1);
      tracer.renderDelay = 0;
      tracer.fadeDuration = 0;
      tracer.minSamples = 1;
      tracer.dynamicLowRes = false;
      tracer.setScene(this.scene, this.camera);
      // count real samples (shader compilation is asynchronous), with a wall-clock safety limit
      const t0 = performance.now();
      while (tracer.samples < samples && performance.now() - t0 < 600000) {
        tracer.renderSample();
        onProgress?.(Math.min(0.97, tracer.samples / samples));
        await new Promise((r) => setTimeout(r, 0));
      }
      tracer.renderSample();
      const ctx = out.getContext("2d")!;
      ctx.drawImage(this.renderer.domElement, 0, 0);
      denoise(ctx, width, height);
      onProgress?.(1);
      tracer.dispose();
    } finally {
      this.floor.visible = false;
      this.contact.group.visible = true;
      this.scene.environment = envPrev;
      this.renderer.setPixelRatio(prevPR);
      this.renderer.setSize(prevSize.x, prevSize.y, false);
      this.camera.aspect = prevSize.x / prevSize.y;
      this.camera.updateProjectionMatrix();
      this.invalidate();
      const loop = () => {
        this.raf = requestAnimationFrame(loop);
        this.controls.update();
        if (this.dirty) {
          this.dirty = false;
          this.renderer.render(this.scene, this.camera);
        }
      };
      loop();
    }
    return out;
  }

  /** High-quality still from the rasteriser: rendered at 2x and downsampled (crisp, instant). */
  snapshot(width: number, height: number): HTMLCanvasElement {
    const prevSize = this.renderer.getSize(new THREE.Vector2());
    const prevPR = this.renderer.getPixelRatio();
    const ss = Math.min(2, 4096 / Math.max(width, height));
    this.renderer.setPixelRatio(1);
    this.renderer.setSize(Math.round(width * ss), Math.round(height * ss), false);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.renderer.render(this.scene, this.camera);
    const out = document.createElement("canvas");
    out.width = width;
    out.height = height;
    const ctx = out.getContext("2d")!;
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(this.renderer.domElement, 0, 0, width, height);
    this.renderer.setPixelRatio(prevPR);
    this.renderer.setSize(prevSize.x, prevSize.y, false);
    this.camera.aspect = prevSize.x / prevSize.y;
    this.camera.updateProjectionMatrix();
    this.invalidate();
    return out;
  }

  dispose() {
    cancelAnimationFrame(this.raf);
    this.controls.dispose();
    this.renderer.dispose();
  }
}


/**
 * Soft contact shadow (as used by product viewers): render the model's depth from below into a
 * texture, blur it, and lay it on the floor. Much closer to a studio photo than a hard shadow map.
 */
class ContactShadow {
  readonly group = new THREE.Group();
  private rt: THREE.WebGLRenderTarget;
  private rtBlur: THREE.WebGLRenderTarget;
  private cam: THREE.OrthographicCamera;
  private plane: THREE.Mesh;
  private blurPlane: THREE.Mesh;
  private depthMat: THREE.MeshDepthMaterial;
  private hBlur: THREE.ShaderMaterial;
  private vBlur: THREE.ShaderMaterial;
  private size = 2;

  constructor(private renderer: THREE.WebGLRenderer, res = 512) {
    this.rt = new THREE.WebGLRenderTarget(res, res);
    this.rt.texture.generateMipmaps = false;
    this.rtBlur = new THREE.WebGLRenderTarget(res, res);
    this.rtBlur.texture.generateMipmaps = false;
    const geo = new THREE.PlaneGeometry(1, 1).rotateX(Math.PI / 2);
    this.plane = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ map: this.rt.texture, opacity: 0.32, transparent: true, depthWrite: false }));
    this.plane.renderOrder = 1;
    this.plane.scale.y = -1;
    this.group.add(this.plane);
    this.blurPlane = new THREE.Mesh(geo);
    this.blurPlane.visible = false;
    this.group.add(this.blurPlane);
    this.group.position.y = 0.001;
    this.cam = new THREE.OrthographicCamera(-0.5, 0.5, 0.5, -0.5, 0, 0.5);
    this.cam.rotation.x = Math.PI / 2;
    this.group.add(this.cam);
    this.depthMat = new THREE.MeshDepthMaterial();
    this.depthMat.userData.darkness = { value: 1.1 };
    this.depthMat.onBeforeCompile = (shader) => {
      shader.uniforms.darkness = this.depthMat.userData.darkness;
      shader.fragmentShader = `uniform float darkness;\n${shader.fragmentShader.replace(
        "gl_FragColor = vec4( vec3( 1.0 - fragCoordZ ), opacity );",
        "gl_FragColor = vec4( vec3( 0.0 ), ( 1.0 - fragCoordZ ) * darkness );",
      )}`;
    };
    this.depthMat.depthTest = false;
    this.depthMat.depthWrite = false;
    this.hBlur = new THREE.ShaderMaterial(HorizontalBlurShader);
    this.hBlur.depthTest = false;
    this.vBlur = new THREE.ShaderMaterial(VerticalBlurShader);
    this.vBlur.depthTest = false;
  }

  resize(size: number) {
    this.size = size;
    this.plane.scale.set(size, -1, size);
    this.blurPlane.scale.set(size, 1, size);
    this.cam.left = this.cam.bottom = -size / 2;
    this.cam.right = this.cam.top = size / 2;
    this.cam.far = size * 0.22; // only the lower part of the product darkens the floor
    this.cam.updateProjectionMatrix();
  }

  private blur(amount: number) {
    const r = this.renderer;
    this.blurPlane.visible = true;
    this.blurPlane.material = this.hBlur;
    this.hBlur.uniforms.tDiffuse.value = this.rt.texture;
    this.hBlur.uniforms.h.value = amount / 256;
    r.setRenderTarget(this.rtBlur);
    r.render(this.blurPlane, this.cam);
    this.blurPlane.material = this.vBlur;
    this.vBlur.uniforms.tDiffuse.value = this.rtBlur.texture;
    this.vBlur.uniforms.v.value = amount / 256;
    r.setRenderTarget(this.rt);
    r.render(this.blurPlane, this.cam);
    this.blurPlane.visible = false;
  }

  update(scene: THREE.Scene, _model: THREE.Object3D) {
    void _model;
    const r = this.renderer;
    const bg = scene.background;
    scene.background = null;
    scene.overrideMaterial = this.depthMat;
    this.plane.visible = false;
    const clear = r.getClearAlpha();
    r.setClearAlpha(0);
    r.setRenderTarget(this.rt);
    r.clear();
    r.render(scene, this.cam);
    scene.overrideMaterial = null;
    this.blur(2.2);
    this.blur(1.0);
    this.blur(0.4);
    r.setRenderTarget(null);
    r.setClearAlpha(clear);
    scene.background = bg;
    this.plane.visible = true;
    void this.size;
  }
}


/** Edge-preserving (bilateral) denoise of the path-traced still: removes residual sampling grain. */
function denoise(ctx: CanvasRenderingContext2D, w: number, h: number) {
  const id = ctx.getImageData(0, 0, w, h);
  const src = id.data;
  const out = new Uint8ClampedArray(src.length);
  // 1) firefly removal: a pixel much brighter than all its neighbours takes their mean
  for (let y = 1; y < h - 1; y++)
    for (let x = 1; x < w - 1; x++) {
      const i = (y * w + x) * 4;
      const l = src[i] + src[i + 1] + src[i + 2];
      let mx = 0, sr0 = 0, sg0 = 0, sb0 = 0;
      for (const [dx, dy] of [[-1, 0], [1, 0], [0, -1], [0, 1], [-1, -1], [1, 1], [-1, 1], [1, -1]]) {
        const j = ((y + dy) * w + x + dx) * 4;
        mx = Math.max(mx, src[j] + src[j + 1] + src[j + 2]);
        sr0 += src[j]; sg0 += src[j + 1]; sb0 += src[j + 2];
      }
      if (l > mx + 90) {
        src[i] = sr0 / 8; src[i + 1] = sg0 / 8; src[i + 2] = sb0 / 8;
      }
    }
  // 2) light bilateral pass (keeps weave and grain)
  const R = 1;
  const sr = 2 * 12 * 12; // range sigma ~12/255
  const ss = 2 * 1.6 * 1.6;
  for (let y = 0; y < h; y++)
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      const r0 = src[i], g0 = src[i + 1], b0 = src[i + 2];
      let r = 0, g = 0, b = 0, ws = 0;
      for (let dy = -R; dy <= R; dy++) {
        const yy = Math.min(h - 1, Math.max(0, y + dy));
        for (let dx = -R; dx <= R; dx++) {
          const xx = Math.min(w - 1, Math.max(0, x + dx));
          const j = (yy * w + xx) * 4;
          const dr = src[j] - r0, dg = src[j + 1] - g0, db = src[j + 2] - b0;
          const wgt = Math.exp(-(dx * dx + dy * dy) / ss - (dr * dr + dg * dg + db * db) / 3 / sr);
          r += src[j] * wgt; g += src[j + 1] * wgt; b += src[j + 2] * wgt; ws += wgt;
        }
      }
      out[i] = r / ws; out[i + 1] = g / ws; out[i + 2] = b / ws; out[i + 3] = 255;
    }
  id.data.set(out);
  ctx.putImageData(id, 0, 0);
}
