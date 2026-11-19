import * as T from "three";
import { artworks, type Floor, type PrintName, type Tool } from "./catalog";
import { createExhibitSelector } from "./exhibitSelection";
import { atomicModels, haikuDetail, japanPrints, microscopicField } from "./exhibits";
import { pigments, type World } from "./materials";
import { constellation, earthApproach, nightSky, solarSystem } from "./space";

export type SceneVisit = { tool: Tool; floor: Floor } | null;
export type Gallery = ReturnType<typeof createGallery>;
const depthFor = (v: SceneVisit) => (!v ? 0 : { codex: 1, claude: 3, agy: 5 }[v.tool]);
const magnification = [6.5, 6, 5.5, 5.2, 7];
const arrivalScale = [0.22, 0.14, 0.57, 0.18, 1.65];
const ease = (t: number) => t * t * (3 - 2 * t);
const mix = T.MathUtils.lerp;

export function createGallery(canvas: HTMLCanvasElement) {
  const renderer = new T.WebGLRenderer({ canvas, antialias: true, powerPreference: "low-power" });
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 1.5));
  const scene = new T.Scene(),
    camera = new T.PerspectiveCamera(38, 1, 0.08, 175);
  const p = pigments(),
    background = nightSky(p);
  scene.background = background;
  scene.add(new T.HemisphereLight(p.color("grey", 50), p.color("info", 800), 1.25));
  const sun = new T.DirectionalLight(p.color("warning", 100), 2.1);
  sun.position.set(-4, 5, 6);
  scene.add(sun);
  const rim = new T.DirectionalLight(p.color("info", 200), 1.05);
  rim.position.set(5, 2, -4);
  scene.add(rim);

  let frame = 0,
    disposed = false,
    visible = true,
    dirty = true,
    lastPaint = 0;
  let depth = 0,
    from = 0,
    to = 0,
    started = 0,
    duration = 0,
    yaw = 0,
    destination: SceneVisit = null;
  const motion = matchMedia("(prefers-reduced-motion: reduce)");
  function redraw() {
    dirty = true;
    schedule();
  }
  const loader = new T.TextureLoader();
  const load = (url: string) => {
    const map = loader.load(url, redraw);
    map.colorSpace = T.SRGBColorSpace;
    map.anisotropy = Math.min(4, renderer.capabilities.getMaxAnisotropy());
    return map;
  };
  const earthMap = load("/model-lab/earth.webp"),
    region = load("/model-lab/japan.webp");
  const prints = Object.fromEntries(Object.entries(artworks).map(([key, art]) => [key, load(art.image)])) as Record<
    PrintName,
    T.Texture
  >;
  const atoms = atomicModels(p),
    micro = microscopicField(p);
  const worlds = [
    constellation(p),
    solarSystem(p, earthMap),
    earthApproach(p, earthMap),
    japanPrints(p, region, prints),
    haikuDetail(prints.Haiku),
    atoms.world,
  ];
  const selectors = worlds.map((w) => createExhibitSelector(w, p));
  for (const w of worlds) {
    w.root.visible = false;
    scene.add(w.root);
  }

  // Each scale uses local units. Two render targets let the camera approach an opaque object
  // (the Earth, a print, the dewdrop) and reveal its interior without near-plane clipping or depth fights.
  const targets = [new T.WebGLRenderTarget(), new T.WebGLRenderTarget()];
  const composite = new T.Scene(),
    screen = new T.Camera();
  const blend = new T.ShaderMaterial({
    uniforms: { outer: { value: targets[0].texture }, inner: { value: targets[1].texture }, amount: { value: 0 } },
    vertexShader: "varying vec2 coord; void main(){coord=uv; gl_Position=vec4(position.xy,0.,1.);}",
    fragmentShader: [
      "varying vec2 coord; uniform sampler2D outer; uniform sampler2D inner; uniform float amount;",
      "void main(){ gl_FragColor=mix(texture2D(outer,coord),texture2D(inner,coord),amount);",
      "#include <colorspace_fragment>",
      "}",
    ].join("\n"),
    depthTest: false,
    depthWrite: false,
  });
  const quad = new T.PlaneGeometry(2, 2);
  composite.add(new T.Mesh(quad, blend));
  const center = new T.Vector3();
  function paint(w: World, scale: number, offset: T.Vector3, angle: number, buffer: T.WebGLRenderTarget | null) {
    scene.background = w === atoms.world ? micro : background;
    w.root.visible = true;
    w.root.scale.setScalar(scale);
    w.root.position.copy(offset);
    w.root.rotation.y = angle;
    renderer.setRenderTarget(buffer);
    renderer.render(scene, camera);
    w.root.visible = false;
  }
  const origin = new T.Vector3();
  function render(now: number) {
    const i = Math.floor(depth),
      t = depth - i;
    atoms.animate(motion.matches ? 0 : now / 1000);
    if (t < 0.0001 || i === worlds.length - 1) {
      paint(worlds[i], 1, origin, yaw, null);
      return;
    }
    const u = ease(t),
      outer = worlds[i],
      inner = worlds[i + 1],
      zoom = magnification[i] ** u;
    // At the boundary the camera is centred on the actual star, Earth, Japan, Haiku print or droplet.
    center.copy(outer.portal).multiplyScalar(-u * zoom);
    paint(outer, zoom, center, yaw * (1 - u), targets[0]);
    const arrival = arrivalScale[i];
    center.copy(outer.portal).multiplyScalar((1 - u) * 0.7);
    if (i === 4) center.x += 4.05 * (1 - u) * arrival;
    paint(inner, arrival ** (1 - u), center, 0, targets[1]);
    blend.uniforms.amount.value = T.MathUtils.smoothstep(u, 0.21, 0.83);
    renderer.setRenderTarget(null);
    renderer.render(composite, screen);
  }
  function tick(now: number) {
    frame = 0;
    if (!visible || document.hidden || disposed) return;
    const moving = depth !== to;
    if (moving) {
      // A queued frame can carry a timestamp just before the input event that started the trip.
      const progress = T.MathUtils.clamp((now - started) / duration, 0, 1);
      depth = progress === 1 ? to : mix(from, to, progress);
    }
    const animating = depth === 5 && !motion.matches;
    if (dirty || moving || (animating && now - lastPaint >= 40)) {
      dirty = false;
      lastPaint = now;
      render(now);
    }
    if (depth !== to || animating) schedule();
  }
  function schedule() {
    if (!disposed && visible && !document.hidden && !frame) frame = requestAnimationFrame(tick);
  }
  function visit(next: SceneVisit, immediate = false) {
    destination = next;
    const target = depthFor(next);
    selectors.forEach((select, i) => select(i === target ? next?.floor : undefined));
    if (immediate || motion.matches) {
      depth = to = target;
      yaw = 0;
    } else if (to !== target) {
      from = depth;
      to = target;
      started = performance.now();
      duration = Math.max(180, Math.abs(to - from) * 1250);
      yaw = 0;
    }
    redraw();
  }
  const onMotion = () => visit(destination, motion.matches);
  motion.addEventListener("change", onMotion);
  document.addEventListener("visibilitychange", redraw);
  const observer = new IntersectionObserver(([entry]) => {
    visible = entry.isIntersecting;
    if (visible) redraw();
  });
  observer.observe(canvas);
  const resize = new ResizeObserver(() => {
    const { width, height } = canvas.getBoundingClientRect();
    if (!width || !height) return;
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.position.set(0, 0, Math.max(4.05, 6.89 / camera.aspect) / Math.tan(T.MathUtils.degToRad(19)));
    camera.lookAt(0, 0, 0);
    camera.updateProjectionMatrix();
    const size = renderer.getDrawingBufferSize(new T.Vector2());
    for (const target of targets) target.setSize(size.x, size.y);
    redraw();
  });
  resize.observe(canvas);
  const ray = new T.Raycaster();
  return {
    visit,
    turn(pixels: number) {
      yaw = T.MathUtils.clamp(yaw - pixels * 0.004, -0.36, 0.36);
      redraw();
    },
    pick(x: number, y: number): string | null {
      if (depth !== to) return null;
      const w = worlds[to],
        r = canvas.getBoundingClientRect();
      // Picking also works if a pointer event follows a layout/visit update before the next frame.
      w.root.position.set(0, 0, 0);
      w.root.scale.setScalar(1);
      w.root.rotation.y = yaw;
      w.root.updateMatrixWorld(true);
      ray.setFromCamera(new T.Vector2(((x - r.left) * 2) / r.width - 1, 1 - ((y - r.top) * 2) / r.height), camera);
      const hits = ray.intersectObjects(w.hits.filter((object) => object.visible), true);
      let obj: T.Object3D | null = hits.find((hit) => hit.object instanceof T.Mesh)?.object ?? null;
      while (obj && !obj.userData.choice) obj = obj.parent;
      return obj?.userData.choice ?? null;
    },
    dispose() {
      disposed = true;
      cancelAnimationFrame(frame);
      resize.disconnect();
      observer.disconnect();
      motion.removeEventListener("change", onMotion);
      document.removeEventListener("visibilitychange", redraw);
      const geometry = new Set<T.BufferGeometry>(),
        materials = new Set<T.Material>(),
        maps = new Set<T.Texture>([background, micro, p.haze]);
      scene.traverse((obj) => {
        if (obj instanceof T.InstancedMesh) obj.dispose();
        if (obj instanceof T.Mesh || obj instanceof T.Line || obj instanceof T.Sprite) {
          if (!(obj instanceof T.Sprite)) geometry.add(obj.geometry);
          for (const m of Array.isArray(obj.material) ? obj.material : [obj.material]) materials.add(m);
        }
      });
      for (const m of materials) {
        for (const value of Object.values(m)) if (value instanceof T.Texture) maps.add(value);
        m.dispose();
      }
      for (const g of geometry) g.dispose();
      for (const map of maps) map.dispose();
      for (const target of targets) target.dispose();
      quad.dispose();
      blend.dispose();
      renderer.dispose();
    },
  };
}
