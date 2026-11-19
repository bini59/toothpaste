import * as T from "three";
import { books, type Floor, type PrintName, type Visit } from "./catalog";
import { createExhibitSelector } from "./exhibitSelection";
import { bookcase, inkWindow } from "./books";
import { atomicModels, locationPlate } from "./exhibits";
import { pigments, type World } from "./materials";
import { earthApproach, milkyWay, nightSky, solarSystem } from "./space";

export type SceneVisit = (Visit & { floor: Floor; bookName?: string }) | null;
export type Gallery = ReturnType<typeof createGallery>;
export type Stage = { id: string; title: string; moving: boolean };
const stops = [
  ["galaxy", "우리은하"],
  ["solar", "태양계"],
  ["earth", "지구"],
  ["japan", "일본 · 교토"],
  ["city", "교토 · 가모강"],
  ["library", "도서관 입구"],
  ["aisle", "도서관 서가"],
  ["shelf", "문학 책장"],
  ["book", "펼친 책"],
  ["letter", "활자의 잉크"],
  ["atoms", "Gemini · 원자 모형"],
] as const;
const time = [0, 1.62, 3.54, 5.02, 6.91, 8.87, 10.69, 12.61, 15.54, 17.82, 21.12];
const shelf = 7,
  opened = 8,
  ink = 9,
  atoms = 10;
function stopFor(v: SceneVisit) {
  if (!v || (v.tool === "codex" && v.key === "Astra")) return 0;
  if (v.tool === "codex") return 1;
  if (v.tool === "claude") return v.book ? opened : shelf;
  return atoms;
}
const smooth = T.MathUtils.smoothstep,
  lerp = T.MathUtils.lerp;
const inkZoom = 16.5 / inkWindow.width;
const zoomOut = [7, 6.0, 4.5, 6.2, 4.0, 12.0, 4.5, 1, inkZoom, 95];
const incomingSize = [0.49, 0.97, 0.95, 0.95, 0.82, 0.91, 0.81, 1, 0.9, 2.7];

export function createGallery(canvas: HTMLCanvasElement, onStage: (s: Stage) => void) {
  const renderer = new T.WebGLRenderer({ canvas, antialias: true, powerPreference: "low-power" });
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 1.5));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = T.PCFShadowMap;
  renderer.shadowMap.autoUpdate = false;
  const camera = new T.PerspectiveCamera(38, 1, 0.08, 200),
    scene = new T.Scene(),
    p = pigments();
  const sky = nightSky(p),
    paper = new T.Color(p.tone("warning", 50, "grey", 200, 0.22));
  scene.add(new T.HemisphereLight(p.color("grey", 50), p.color("grey", 700), 1.42));
  const sun = new T.DirectionalLight(p.color("warning", 100), 2.05);
  sun.position.set(-5, 7, 4);
  sun.castShadow = true;
  sun.shadow.mapSize.set(1024, 1024);
  Object.assign(sun.shadow.camera, { left: -10, right: 10, top: 9, bottom: -9, near: 0.1, far: 33 });
  sun.shadow.camera.updateProjectionMatrix();
  sun.shadow.bias = -0.00032;
  sun.shadow.normalBias = 0.016;
  sun.shadow.radius = 3;
  const bounce = new T.DirectionalLight(p.color("grey", 50), 0.8);
  bounce.position.set(4, 1, 7);
  bounce.visible = false;
  scene.add(sun, bounce);
  let frame = 0,
    visible = true,
    disposed = false,
    dirty = true,
    last = 0,
    clock = 0,
    goal = 0,
    yaw = 0;
  let destination: SceneVisit = null,
    volume = "Fable",
    wantedBook = "Fable",
    wantedName = "Fable",
    stageSignature = "";
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const manager = new T.LoadingManager();
  const loader = new T.TextureLoader(manager);
  const load = (name: string) => {
    const t = loader.load("/model-lab/" + name + ".webp", redraw);
    t.colorSpace = T.SRGBColorSpace;
    t.anisotropy = Math.min(6, renderer.capabilities.getMaxAnisotropy());
    return t;
  };
  const earthMap = load("earth"),
    room = load("reading-room");
  const art = Object.fromEntries(Object.keys(books).map((k) => [k, load(k.toLowerCase() + "-engraving")])) as Record<
    PrintName,
    T.Texture
  >;
  const library = bookcase(p, load("walnut"), load("weave"), load("bindings"), art);
  manager.onLoad = () => {
    if (!disposed) {
      library.refresh();
      redraw();
    }
  };
  const initialMacro = library.pose("Fable", 0);
  const macro = locationPlate(initialMacro, 0.5, 0.5, 16.5);
  macro.root.children[0].scale.y = (inkWindow.height / inkWindow.width) * 1.5;
  const macroMaterial = (macro.root.children[0] as T.Mesh<T.PlaneGeometry, T.MeshBasicMaterial>).material;
  const worlds = [
    milkyWay(p, load("galaxy")),
    solarSystem(p, earthMap, load("planets")),
    earthApproach(p, earthMap),
    locationPlate(load("japan"), (135.768 - 122) / 32, (46 - 35.012) / 17),
    locationPlate(load("kyoto"), 0.499, 0.607, 18.9, "street"),
    locationPlate(load("library"), 0.501, 0.703),
    locationPlate(room, 0.502, 0.51, 18.9, "aisle"),
    library.world,
    library.world,
    macro,
    atomicModels(p),
  ];
  const extraPlanet = createExhibitSelector(worlds[1], p),
    extraAtom = createExhibitSelector(worlds[atoms], p, true);
  const unique = [...new Set(worlds)];
  for (const w of unique) {
    w.root.visible = false;
    scene.add(w.root);
  }
  const targets = [new T.WebGLRenderTarget(), new T.WebGLRenderTarget()];
  const composite = new T.Scene(),
    screen = new T.Camera(),
    quad = new T.PlaneGeometry(2, 2);
  const blend = new T.ShaderMaterial({
    uniforms: {
      outer: { value: targets[0].texture },
      inner: { value: targets[1].texture },
      amount: { value: 0 },
      focus: { value: new T.Vector2() },
      aspect: { value: 1 },
      door: { value: new T.Vector2() },
      doorway: { value: false },
    },
    vertexShader: "varying vec2 coord;void main(){coord=uv;gl_Position=vec4(position.xy,0.,1.);}",
    fragmentShader: [
      "varying vec2 coord;uniform sampler2D outer,inner;uniform float amount,aspect;uniform vec2 focus,door;uniform bool doorway;",
      "void main(){float t=amount;vec2 inside=coord;float lift=1.;",
      "if(doorway){vec2 dd=abs(coord-focus)-door;float rim=max(dd.x*aspect,dd.y);t=(1.-smoothstep(-.006,.006,rim))*amount;",
      "float fit=clamp(max(door.x,door.y)*2.,.08,1.);inside=clamp((coord-focus)/fit+.5,0.,1.);lift=mix(.32,1.,fit);}",
      "gl_FragColor=mix(texture2D(outer,coord),texture2D(inner,inside)*vec4(lift,lift,lift,1.),t);",
      "#include <colorspace_fragment>",
      "}",
    ].join("\n"),
    depthTest: false,
    depthWrite: false,
  });
  composite.add(new T.Mesh(quad, blend));
  const origin = new T.Vector3(),
    offset = new T.Vector3(),
    focus = new T.Vector3(),
    entry = new T.Vector3();
  const notes = new Map<World, T.Object3D[]>();
  for (const w of unique) {
    const arr: T.Object3D[] = [];
    w.root.traverse((o) => {
      if (o.userData.annotation) arr.push(o);
    });
    notes.set(w, arr);
  }
  function paint(
    w: World,
    scale: number,
    position: T.Vector3,
    angle: number,
    target: T.WebGLRenderTarget | null,
    annotate: boolean
  ) {
    scene.background = w === worlds[atoms] ? paper : sky;
    w.root.visible = true;
    w.root.scale.setScalar(scale);
    w.root.position.copy(position);
    w.root.rotation.y = angle;
    for (const o of notes.get(w)!)
      o.visible = annotate && (!o.userData.modelKey || o.userData.modelKey === destination?.key);
    const reading = w === library.world;
    if (reading) library.lightDirection(sun.position, angle);
    else sun.position.set(-5, 7, 4);
    bounce.visible = reading;
    sun.shadow.intensity = reading ? 1 - smooth(scale, 1.15, 2.4) : 0;
    renderer.shadowMap.needsUpdate = reading && sun.shadow.intensity > 0;
    renderer.setRenderTarget(target);
    renderer.render(scene, camera);
    w.root.visible = false;
  }
  for (const w of unique) for (const [key, mark] of w.marks) mark.userData.modelKey = key;
  function poseBook(progress: number, approach = 0) {
    const w = library.world;
    w.root.position.set(0, 0, 0);
    w.root.scale.setScalar(1);
    w.root.rotation.y = 0;
    w.root.updateMatrixWorld(true);
    macroMaterial.map = library.pose(volume, progress, approach);
  }
  function segment() {
    let i = 0;
    while (i < time.length - 1 && clock >= time[i + 1] - 0.00001) i++;
    return { i, t: i === atoms ? 0 : T.MathUtils.clamp((clock - time[i]) / (time[i + 1] - time[i]), 0, 1) };
  }
  function render() {
    const { i, t } = segment();
    if (i >= shelf - 1 && i <= ink)
      poseBook(i < shelf ? 0 : i === shelf ? t : 1, i === opened ? t : i > opened ? 1 : 0);
    const spot = t > 0.54 ? Math.min(i + 1, atoms) : i,
      moving = clock !== goal || goal !== time[stopFor(destination)];
    const sig = spot + "/" + moving;
    if (sig !== stageSignature) {
      stageSignature = sig;
      onStage({ id: stops[spot][0], title: stops[spot][1], moving });
    }
    if (t < 0.0001 || i === shelf || i === atoms) {
      paint(worlds[i], 1, origin, yaw, null, true);
      return;
    }
    const outer = worlds[i],
      inner = worlds[i + 1],
      pan = smooth(t, 0.01, 0.77);
    const travel = 0.18 * t + 0.82 * smooth(t, 0.035, 0.99);
    const z = zoomOut[i] ** (i === ink ? smooth(t, 0, 0.65) : travel);
    focus.copy(outer.portal).multiplyScalar(1 - pan);
    offset.copy(focus).addScaledVector(outer.portal, -z);
    const track = (i === 4 ? -0.043 : i === 6 ? 0.039 : 0) * Math.sin(Math.PI * t);
    paint(outer, z, offset, yaw * (1 - pan) + track, targets[0], t < 0.12);
    let size = incomingSize[i] ** (1 - travel);
    entry.set(0, 0, 0);
    // Cross each threshold on its subject: a globe, a marked block, the open door, or one nucleus.
    if (i === 0) entry.set(-6.56, 0.13, 0);
    if (i === 2 || i === 4) entry.copy(inner.portal);
    if (i === 9) entry.set(-4.0, 0.2, 0);
    offset
      .copy(focus)
      .addScaledVector(entry, -size)
      .multiplyScalar(1 - smooth(t, 0.44, 1.0));
    if (i === opened) {
      // The right page is 720 units across; the macro plate reproduces the same 150-unit crop.
      size = z / inkZoom;
      offset.copy(focus);
    } else if (i === ink) {
      // Go inside the chosen ink stroke before pulling away from the matte hydrogen nucleus.
      size = 43 ** (1 - smooth(t, 0.59, 1));
      offset.copy(entry).multiplyScalar(smooth(t, 0.7, 1) - size);
    }
    paint(inner, size, offset, 0, targets[1], t > 0.91);
    const f = focus.project(camera);
    blend.uniforms.focus.value.set(0.5 + f.x * 0.5, 0.5 + f.y * 0.5);
    blend.uniforms.doorway.value = i === 5;
    blend.uniforms.amount.value = smooth(t, i === 1 ? 0.43 : 0.65, i === 1 ? 0.82 : 0.89);
    if (i === 5) {
      // Project the actual opening in the facade. Its jambs occlude the reading room until they pass the camera.
      const corner = entry.copy(outer.portal).add(new T.Vector3(0.93, 0.98, 0));
      outer.root.localToWorld(corner).project(camera);
      blend.uniforms.door.value.set(Math.abs(corner.x - f.x) * 0.5, Math.abs(corner.y - f.y) * 0.5);
      blend.uniforms.amount.value = smooth(t, 0.02, 0.38);
    } else if (i === opened || i === ink) {
      blend.uniforms.amount.value = i === opened ? smooth(t, 0.9, 0.985) : smooth(t, 0.54, 0.68);
    }
    renderer.setRenderTarget(null);
    renderer.render(composite, screen);
  }
  function route(immediate = false) {
    const end = time[stopFor(destination)];
    if (immediate) {
      clock = goal = end;
    }
    if (wantedBook !== volume) {
      if (!immediate && clock > time[shelf] + 0.00001) {
        goal = time[shelf];
        return;
      }
      library.ensure(wantedBook, wantedName);
      volume = wantedBook;
    }
    goal = end;
  }
  function tick(now: number) {
    frame = 0;
    if (disposed || !visible || document.hidden) return;
    const dt = last ? Math.max(0, (now - last) / 1000) : 0;
    last = now;
    const moving = clock !== goal;
    if (moving) {
      const distance = goal - clock;
      clock += Math.sign(distance) * Math.min(Math.abs(distance), dt);
      if (Math.abs(clock - goal) < 0.00001) {
        clock = goal;
        route();
      }
    }
    if (dirty || moving) {
      dirty = false;
      render();
    }
    if (clock !== goal) schedule();
    else last = 0;
  }
  function schedule() {
    if (!disposed && visible && !document.hidden && !frame) frame = requestAnimationFrame(tick);
  }
  function redraw() {
    dirty = true;
    schedule();
  }
  function visit(v: SceneVisit, instant = false) {
    destination = v;
    if (v?.tool === "claude" || v?.tool === "agy") {
      wantedBook = v.tool === "claude" ? v.key : (v.book ?? "Fable");
      wantedName = v.tool === "claude" ? v.floor.name : (v.bookName ?? wantedBook.replace(/^model\//, ""));
    }
    extraPlanet(v?.tool === "codex" ? v.floor : undefined);
    extraAtom(v?.tool === "agy" ? v.floor : undefined);
    yaw = 0;
    route(instant || reduced.matches);
    redraw();
  }
  const skip = () => {
    route(true);
    redraw();
  };
  const onMotion = () => {
    if (reduced.matches) skip();
  };
  const onVisibility = () => {
    last = 0;
    redraw();
  };
  reduced.addEventListener("change", onMotion);
  document.addEventListener("visibilitychange", onVisibility);
  const observer = new IntersectionObserver(([e]) => {
    visible = e.isIntersecting;
    last = 0;
    if (visible) redraw();
  });
  observer.observe(canvas);
  const resize = new ResizeObserver(() => {
    const { width, height } = canvas.getBoundingClientRect();
    if (!width || !height) return;
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.position.set(0, 0, Math.max(3.95, 6.76 / camera.aspect) / Math.tan(T.MathUtils.degToRad(19)));
    for (const w of unique) w.projectPlate?.(camera.position.z);
    camera.lookAt(0, 0, 0);
    camera.updateMatrixWorld();
    camera.updateProjectionMatrix();
    blend.uniforms.aspect.value = camera.aspect;
    const size = renderer.getDrawingBufferSize(new T.Vector2());
    targets.forEach((t) => t.setSize(size.x, size.y));
    redraw();
  });
  resize.observe(canvas);
  const ray = new T.Raycaster();
  return {
    visit,
    skip,
    turn(dx: number) {
      if (clock === goal) {
        yaw = T.MathUtils.clamp(yaw - dx * 0.003, -0.19, 0.19);
        redraw();
      }
    },
    pick(x: number, y: number): string | null {
      if (clock !== goal) return null;
      const idx = stopFor(destination),
        w = worlds[idx],
        r = canvas.getBoundingClientRect();
      if (idx === shelf || idx === opened) poseBook(idx === shelf ? 0 : 1);
      w.root.position.set(0, 0, 0);
      w.root.scale.setScalar(1);
      w.root.rotation.y = yaw;
      w.root.updateMatrixWorld(true);
      ray.setFromCamera(new T.Vector2((2 * (x - r.left)) / r.width - 1, 1 - (2 * (y - r.top)) / r.height), camera);
      for (const hit of ray.intersectObjects(w.hits, true)) {
        let key: string | null = null,
          shown = true;
        for (let o: T.Object3D | null = hit.object; o && o !== w.root; o = o.parent) {
          shown = shown && o.visible;
          key = key ?? o.userData.choice ?? null;
        }
        if (shown && key) return key;
      }
      return null;
    },
    dispose() {
      disposed = true;
      cancelAnimationFrame(frame);
      resize.disconnect();
      observer.disconnect();
      reduced.removeEventListener("change", onMotion);
      document.removeEventListener("visibilitychange", onVisibility);
      const geometry = new Set<T.BufferGeometry>(),
        mats = new Set<T.Material>(),
        maps = new Set<T.Texture>([sky]);
      scene.traverse((o) => {
        if (o instanceof T.InstancedMesh) o.dispose();
        if (o instanceof T.Mesh || o instanceof T.Line || o instanceof T.Sprite) {
          if (!(o instanceof T.Sprite)) geometry.add(o.geometry);
          for (const m of Array.isArray(o.material) ? o.material : [o.material]) mats.add(m);
        }
      });
      for (const m of mats) {
        for (const v of Object.values(m)) if (v instanceof T.Texture) maps.add(v);
        m.dispose();
      }
      geometry.forEach((g) => g.dispose());
      maps.forEach((m) => m.dispose());
      library.dispose();
      targets.forEach((t) => t.dispose());
      quad.dispose();
      blend.dispose();
      sun.shadow.dispose();
      renderer.dispose();
    },
  };
}
