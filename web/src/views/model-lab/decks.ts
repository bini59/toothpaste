import * as T from "three";
import { circle, halo, mesh, texture, type Paint } from "./materials";
import { providers, tools, type Sculpture, type Tool } from "./catalog";

export const spacing = 4.9;
export const entrances = {
  agy: new T.Vector3(-5.45, 0, 0.6), claude: new T.Vector3(0, 0, -5.45), codex: new T.Vector3(5.45, 0, 0.6),
};
export const accent = (p: Paint, tool: Tool) => p.color(({ agy: "info", claude: "warning", codex: "primary" })[tool], 300);
type Pieces = (tool: Tool, name: Sculpture) => T.Group;

function ring(parent: T.Object3D, radius: number, tube: number, height: number, mat: T.Material, gap = 0) {
  const geo = circle(radius, tube, Math.PI * 2 - gap);
  geo.rotateZ((gap - Math.PI) / 2);
  geo.rotateX(-Math.PI / 2);
  return mesh(parent, geo, mat, 0, height, 0);
}

/** A cutaway through the front of the annulus keeps the rims of the floors below in view. */
function deck(radius: number, inner: number, gap: number, p: Paint, light: T.Material) {
  const group = new T.Group();
  const start = (gap - Math.PI) / 2, end = start + 2 * Math.PI - gap;
  const shape = new T.Shape();
  shape.absarc(0, 0, radius, start, end, false);
  shape.lineTo(inner * Math.cos(end), inner * Math.sin(end));
  shape.absarc(0, 0, inner, end, start, true);
  shape.closePath();
  const geo = new T.ExtrudeGeometry(shape, { depth: 0.24, bevelSize: 0.035, bevelThickness: 0.035, bevelSegments: 2, curveSegments: 72 });
  geo.rotateX(-Math.PI / 2);
  mesh(group, geo, p.deck, 0, -0.25);
  ring(group, radius - 0.02, 0.034, -0.19, p.trim, gap);
  ring(group, inner, 0.030, -0.09, light, gap);
  ring(group, inner + 0.17, 0.014, 0.035, p.brass, gap);
  ring(group, radius - 0.40, 0.012, 0.035, p.trim, gap);
  const count = 112, ticks = new T.InstancedMesh(new T.BoxGeometry(1, 1, 1), p.trim, count);
  const tick = new T.Object3D();
  for (let i = 0; i < count; i++) {
    const a = start + (end - start) * (i + 0.5) / count, long = i % 4 === 0;
    tick.position.set((radius - 0.22) * Math.cos(a), 0.036, -(radius - 0.22) * Math.sin(a));
    tick.rotation.y = a;
    tick.scale.set(long ? 0.21 : 0.09, 0.011, long ? 0.026 : 0.016);
    tick.updateMatrix();
    ticks.setMatrixAt(i, tick.matrix);
  }
  group.add(ticks);
  return group;
}

function plinth(parent: T.Object3D, r: number, p: Paint, trim: T.Material) {
  mesh(parent, new T.CylinderGeometry(r * 0.95, r, 0.22, 72), p.basalt, 0, 0.12);
  mesh(parent, new T.CylinderGeometry(r * 0.89, r * 0.93, 0.10, 72), p.deck, 0, 0.28);
  ring(parent, r * 0.945, 0.022, 0.19, trim);
  ring(parent, r * 0.79, 0.012, 0.334, p.brass);
}

export function lobby(p: Paint, piece: Pieces) {
  const hall = deck(7.66, 4.45, 0.75, p, p.brass);
  const gates: T.Group[] = [];
  const sample: Record<Tool, Sculpture> = { agy: "Neon", claude: "Sonnet", codex: "Terra" };
  for (const tool of tools) {
    const gate = new T.Group(), light = p.glow(accent(p, tool), 0.67);
    gate.userData.tool = tool;
    gate.position.copy(entrances[tool]);
    gate.rotation.y = -gate.position.x * 0.038;
    hall.add(gate);
    gates.push(gate);
    plinth(gate, 1.67, p, light);
    const frame = new T.Shape();
    frame.moveTo(-1.52, 0); frame.lineTo(-1.52, 1.75);
    frame.absarc(0, 1.75, 1.52, Math.PI, 0, true);
    frame.lineTo(1.52, 0); frame.lineTo(1.34, 0); frame.lineTo(1.34, 1.75);
    frame.absarc(0, 1.75, 1.34, 0, Math.PI, false);
    frame.lineTo(-1.34, 0); frame.closePath();
    mesh(gate, new T.ExtrudeGeometry(frame, { depth: 0.29, bevelThickness: 0.025, bevelSize: 0.025, bevelSegments: 2, curveSegments: 32 }), p.trim, 0, 0.10, -0.76);
    mesh(gate, circle(1.41, 0.030, Math.PI), light, 0, 1.85, -0.435);
    for (const side of [-1, 1]) mesh(gate, new T.BoxGeometry(0.049, 1.63, 0.035), light, side * 1.41, 1.02, -0.43);
    const wash = halo(gate, p, accent(p, tool), 4.8, 0.21);
    wash.position.set(0, 1.6, -0.95);
    const object = piece(tool, sample[tool]);
    object.position.y = 1.52;
    object.scale.setScalar(0.90);
    gate.add(object);
    const sign = texture(320, 88, (ctx) => {
      ctx.fillStyle = p.color("grey", 900); ctx.fillRect(0, 0, 320, 88);
      ctx.fillStyle = accent(p, tool); ctx.fillRect(0, 0, 320, 4);
      ctx.font = "600 49px sans-serif"; ctx.textAlign = "center";
      ctx.fillStyle = p.color("grey", 50); ctx.fillText(providers[tool].name, 160, 64);
    });
    const label = mesh(gate, new T.PlaneGeometry(1.7, 0.47), new T.MeshBasicMaterial({ map: sign }), 0, 0.37, 1.54);
    label.rotation.x = -0.25;
  }
  return { hall, gates };
}

export function floorDeck(p: Paint, light: T.Material) {
  const radius = 6.6, level = new T.Group(), balcony = deck(radius, radius * 0.71, 1.50, p, light);
  level.add(balcony);
  // A bridge from the perimeter to the specimen leaves open sightlines into the shaft.
  mesh(balcony, new T.BoxGeometry(1.20, 0.17, radius), p.deck, 0, -0.07, -radius / 2);
  plinth(level, 1.68, p, light);
  const exhibit = new T.Group();
  level.add(exhibit);
  return { level, balcony, exhibit };
}

/** Back wall of the drum. Its vertical ribs and narrow light slots give the camera travel a stationary reference. */
export function chamber(p: Paint) {
  const wall = new T.Group();
  const material = p.metal(p.color("grey", 800), 0.36, 0.59);
  material.side = T.BackSide;
  mesh(wall, new T.CylinderGeometry(16, 16, 86, 96, 1, true), material, 0, -26);
  const rib = new T.BoxGeometry(0.16, 86, 0.33), strip = new T.BoxGeometry(0.034, 86, 0.045);
  const lamp = p.glow(p.color("info", 700), 0.30);
  for (let i = 0; i < 42; i++) {
    const a = i * 2 * Math.PI / 42;
    if (Math.cos(a) > 0.35) continue;
    const bar = mesh(wall, rib, p.basalt, 15.65 * Math.sin(a), -26, 15.65 * Math.cos(a));
    bar.rotation.y = a;
    if (i % 3 === 0) mesh(wall, strip, lamp, 15.42 * Math.sin(a), -26, 15.42 * Math.cos(a));
  }
  for (let i = 0; i < 15; i++) ring(wall, 15.52, 0.045, 5 - i * spacing, p.trim, 2.6);
  wall.traverse((part) => { part.castShadow = part.receiveShadow = false; });
  return wall;
}
