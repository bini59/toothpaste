import * as T from "three";
import { accent, chamber, entrances, floorDeck, lobby, spacing } from "./decks";
import { pigments, studioEnvironment } from "./materials";
import { sculpture } from "./sculptures";
import type { Floor, Sculpture, Tool } from "./catalog";

export type SceneVisit = { tool: Tool; floors: Floor[]; index: number } | null;
export type Gallery = ReturnType<typeof createGallery>;
const mix = T.MathUtils.lerp;
const ease = (t: number) => t * t * (3 - 2 * t);

export function createGallery(canvas: HTMLCanvasElement) {
  const renderer = new T.WebGLRenderer({ canvas, antialias: true, powerPreference: "low-power" });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.6));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = T.PCFShadowMap;
  renderer.toneMapping = T.ACESFilmicToneMapping;
  renderer.transmissionResolutionScale = 0.5;
  const p = pigments(), scene = new T.Scene(), camera = new T.PerspectiveCamera(38, 1, 0.08, 80);
  const dark = new T.Color(p.color("grey", 900)).lerp(new T.Color(p.color("info", 900)), 0.24);
  scene.background = dark;
  const fog = new T.Fog(dark, 11, 35);
  scene.fog = fog;
  scene.environment = studioEnvironment(p);
  scene.environmentIntensity = 0.72;
  const sky = new T.HemisphereLight(p.color("info", 100), p.color("warning", 800), 1.1);
  const key = new T.SpotLight(p.color("warning", 100), 330, 29, 0.87, 0.65, 2);
  key.castShadow = true;
  key.shadow.camera.near = 0.3;
  key.shadow.radius = 2.5;
  key.shadow.mapSize.set(1024, 1024);
  key.shadow.normalBias = 0.014;
  const rim = new T.DirectionalLight(p.color("info", 200), 1.6);
  const focus = new T.PointLight(p.color("grey", 100), 46, 12, 2);
  scene.add(sky, key, key.target, rim, rim.target, focus);
  const library = new Map<string, T.Group>();
  const piece = (tool: Tool, name: Sculpture) => {
    const id = tool + "/" + name;
    if (!library.has(id)) library.set(id, sculpture(tool, name, p));
    return library.get(id)!.clone();
  };
  const { hall, gates } = lobby(p, piece);
  const wall = chamber(p);
  scene.add(hall, wall);
  const trim = p.glow(p.color("info", 400), 0.6);
  // Reuse five physical decks around the camera, even when the account lists many extra models.
  const decks = Array.from({ length: 5 }, () => {
    const d = floorDeck(p, trim);
    scene.add(d.level);
    return { ...d, model: "" };
  });
  let tour: SceneVisit = null, pending: SceneVisit = null;
  let depth = -1, from = -1, to = -1, start = 0, duration = 0, frame = 0, orbit = 0;
  const motion = matchMedia("(prefers-reduced-motion: reduce)");
  const proportion = (i: number) => Math.min(1, Math.max(0, i / Math.max(1, (tour?.floors.length ?? 4) - 1)));
  const modelHeight = (i: number) => 1.65 + 0.46 * proportion(i);
  const roomPose = () => {
    const distance = Math.max(16.4, 26.8 / camera.aspect);
    return { eye: new T.Vector3(0, 0.52 * distance, 0.89 * distance), aim: new T.Vector3(0, -0.65, 0) };
  };
  function floorPose(i: number) {
    const t = proportion(i), d = (9.25 - 2.95 * t) * Math.max(1, 1.18 / camera.aspect);
    const a = orbit + 0.24 * Math.cos(t * 3.8 + (tour?.tool === "agy" ? 0.4 : 1.3));
    const aim = new T.Vector3(0, -(i + 1) * spacing + modelHeight(i) - 0.1, 0);
    return { aim, eye: aim.clone().add(new T.Vector3(d * Math.sin(a), d * (0.31 + 0.05 * t), d * Math.cos(a))) };
  }
  function pose() {
    if (depth >= 0) return floorPose(depth);
    const room = roomPose();
    if (!tour || depth === -1) return room;
    const gate = entrances[tour.tool], floor = floorPose(0);
    // Pass the chosen arch at eye level, then cross the inner lip before dropping inside the ring.
    const path = (points: T.Vector3[]) => new T.CatmullRomCurve3(points).getPoint(depth + 1);
    return {
      eye: path([room.eye, new T.Vector3(gate.x * 0.92, 2.9, gate.z + 5.8),
        new T.Vector3(gate.x * 0.39, 0.4, 2.95), floor.eye]),
      aim: path([room.aim, gate.clone().add(new T.Vector3(0, 1.4, 0)),
        new T.Vector3(0, -1.9, -0.6), floor.aim]),
    };
  }
  function render() {
    const { eye, aim } = pose();
    camera.position.copy(eye);
    camera.lookAt(aim);
    const framing = Math.max(0, eye.distanceTo(aim) - 18);
    fog.near = 11 + framing; fog.far = 35 + framing;
    const nearest = Math.max(0, Math.round(depth));
    // Keep each deck in the same slot on adjacent frames.
    for (let i = nearest - 2; i <= nearest + 2; i++) {
      const d = decks[((i % 5) + 5) % 5];
      d.level.visible = i >= 0 && i < (tour?.floors.length ?? 3);
      if (!d.level.visible) continue;
      const t = proportion(i);
      d.level.position.y = -(i + 1) * spacing;
      d.balcony.scale.set(1 - 0.57 * t, 1, 1 - 0.57 * t);
      const exhibit = tour?.floors[i].sculpture;
      const id = tour && exhibit ? tour.tool + "/" + exhibit : "";
      if (id !== d.model) {
        d.exhibit.clear();
        if (tour && exhibit) d.exhibit.add(piece(tour.tool, exhibit));
        d.model = id;
      }
      d.exhibit.position.y = modelHeight(i);
      d.exhibit.scale.setScalar(1 + 0.54 * t);
    }
    hall.visible = depth < 1.4;
    wall.position.y = Math.min(0, aim.y + 36);
    key.position.copy(aim).add(new T.Vector3(-4.8, 6.0, 5.1));
    key.target.position.copy(aim);
    key.angle = mix(1.16, 0.72, Math.min(1, depth + 1));
    rim.position.copy(aim).add(new T.Vector3(4.2, 3.5, -4.3));
    rim.target.position.copy(aim);
    focus.position.copy(aim).add(new T.Vector3(2.5, 2.6, 2.0));
    renderer.render(scene, camera);
  }
  function tick(time: number) {
    const t = Math.min(1, (time - start) / duration);
    depth = mix(from, to, ease(t));
    if (t === 1) { settle(); return; }
    render();
    frame = requestAnimationFrame(tick);
  }
  function settle() {
    cancelAnimationFrame(frame); frame = 0;
    depth = to;
    if (depth === -1) {
      tour = null;
      orbit = 0;
      focus.color.set(p.color("grey", 100));
      trim.color.set(p.color("info", 400));
      trim.emissive.copy(trim.color);
    }
    render();
    if (pending) visit(pending);
  }
  const onMotion = () => { if (motion.matches) visit(pending ?? (to === -1 ? null : tour), true); };
  motion.addEventListener("change", onMotion);
  const resize = new ResizeObserver(() => {
    const { width, height } = canvas.getBoundingClientRect();
    if (width < 1 || height < 1) return;
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    render();
  });
  resize.observe(canvas);

  function visit(next: SceneVisit, immediate = false) {
    // While returning, a visitor can already choose a different gate. Finish the ascent before entering it.
    if (tour && next && tour.tool !== next.tool && depth > -1 && !immediate && !motion.matches) {
      pending = next;
      if (to === -1) return;
      next = null;
    } else pending = null;
    const target = next?.index ?? -1;
    const same = tour?.tool === next?.tool && to === target;
    if (next) {
      tour = next;
      focus.color.set(accent(p, next.tool));
      trim.color.copy(focus.color); trim.emissive.copy(focus.color);
    }
    if (same && !immediate) { render(); return; }
    cancelAnimationFrame(frame);
    from = depth; to = target;
    if (immediate || motion.matches || from === to) { settle(); return; }
    duration = Math.min(1900, 740 + Math.abs(to - from) * 150 + (Math.min(to, from) < 0 ? 950 : 0));
    start = performance.now();
    frame = requestAnimationFrame(tick);
  }

  const ray = new T.Raycaster();
  return {
    visit,
    turn(pixels: number) {
      orbit = T.MathUtils.clamp(orbit - pixels * 0.008, -0.98, 0.98);
      render();
    },
    entrance(x: number, y: number): Tool | null {
      if (to !== -1 || !hall.visible) return null;
      const r = canvas.getBoundingClientRect();
      ray.setFromCamera(new T.Vector2((x - r.left) * 2 / r.width - 1, 1 - (y - r.top) * 2 / r.height), camera);
      let hit: T.Object3D | null = ray.intersectObjects(gates, true)[0]?.object ?? null;
      while (hit && !hit.userData.tool) hit = hit.parent;
      return hit?.userData.tool ?? null;
    },
    dispose() {
      cancelAnimationFrame(frame);
      resize.disconnect();
      motion.removeEventListener("change", onMotion);
      const geometries = new Set<T.BufferGeometry>(), materials = new Set<T.Material>(), textures = new Set<T.Texture>([p.halo, scene.environment!]);
      for (const root of [scene, ...library.values()]) root.traverse((obj) => {
        if (obj instanceof T.InstancedMesh) obj.dispose();
        if (obj instanceof T.Mesh || obj instanceof T.Line || obj instanceof T.Sprite) {
          if (!(obj instanceof T.Sprite)) geometries.add(obj.geometry);
          for (const mat of Array.isArray(obj.material) ? obj.material : [obj.material]) materials.add(mat);
        }
      });
      for (const item of Object.values(p)) if (item instanceof T.Material) materials.add(item);
      for (const m of materials) {
        for (const item of Object.values(m)) if (item instanceof T.Texture) textures.add(item);
        m.dispose();
      }
      for (const g of geometries) g.dispose();
      for (const t of textures) t.dispose();
      key.shadow.dispose();
      renderer.dispose();
    },
  };
}
