import * as T from "three";
import { circle, halo, mesh, texture, type Paint } from "./materials";
import type { Sculpture, Tool } from "./catalog";

type XY = [number, number];
type XYZ = [number, number, number];
const outline = (points: XY[]) => new T.Shape(points.map(([x, y]) => new T.Vector2(x, y)));
const extrude = (shape: T.Shape, depth: number, bevel: number) =>
  new T.ExtrudeGeometry(shape, { depth, bevelEnabled: true, bevelSize: bevel, bevelThickness: bevel, bevelSegments: 3, curveSegments: 24 });

function tile(p: Paint, name: Sculpture) {
  const group = new T.Group();
  const plate = new T.Group();
  group.add(plate);
  plate.rotation.set(0.05, -0.26, -0.07);
  const shape = new T.Shape();
  shape.moveTo(-0.92, -0.98);
  shape.lineTo(0.92, -0.98); shape.quadraticCurveTo(1.03, -0.98, 1.03, -0.87);
  shape.lineTo(1.03, 0.87); shape.quadraticCurveTo(1.03, 0.98, 0.92, 0.98);
  shape.lineTo(-0.92, 0.98); shape.quadraticCurveTo(-1.03, 0.98, -1.03, 0.87);
  shape.lineTo(-1.03, -0.87); shape.quadraticCurveTo(-1.03, -0.98, -0.92, -0.98);

  // The symbol is cut through the face into a darker backing, with separate counters in the A and e.
  const cut = (points: XY[]) => shape.holes.push(outline(points));
  const island = (points: XY[]) => mesh(plate, extrude(outline(points), 0.075, 0.008), p.silver, 0, 0, 0.12);
  mesh(plate, extrude(shape, 0.22, 0.065), p.patina, 0, 0, -0.17);
  if (name === "Hydrogen") {
    cut([[-0.36,-0.38],[-0.36,0.43],[-0.18,0.43],[-0.18,0.12],[0.18,0.12],[0.18,0.43],[0.36,0.43],[0.36,-0.38],[0.18,-0.38],[0.18,-0.04],[-0.18,-0.04],[-0.18,-0.38]]);
  } else if (name === "Neon") {
    cut([[-0.71,-0.36],[-0.71,0.44],[-0.55,0.44],[-0.21,-0.08],[-0.21,0.44],[-0.05,0.44],[-0.05,-0.36],[-0.21,-0.36],[-0.55,0.16],[-0.55,-0.36]]);
    const e = new T.Shape();
    e.moveTo(0.67, -0.10); e.bezierCurveTo(0.71, 0.4, 0.09, 0.38, 0.08, -0.04);
    e.bezierCurveTo(0.05, -0.4, 0.46, -0.49, 0.66, -0.27); e.lineTo(0.57, -0.19);
    e.bezierCurveTo(0.40, -0.31, 0.25, -0.26, 0.24, -0.1); e.closePath();
    shape.holes.push(e);
    island([[0.24,0.01],[0.50,0.01],[0.48,0.09],[0.43,0.14],[0.35,0.15],[0.28,0.11]]);
  } else if (name === "Argon") {
    cut([[-0.70,-0.36],[-0.40,0.44],[-0.21,0.44],[0.10,-0.36],[-0.09,-0.36],[-0.15,-0.18],[-0.47,-0.18],[-0.53,-0.36]]);
    island([[-0.42,-0.03],[-0.20,-0.03],[-0.31,0.27]]);
    cut([[0.18,-0.36],[0.18,0.21],[0.34,0.21],[0.34,0.12],[0.44,0.21],[0.60,0.23],[0.60,0.07],[0.48,0.07],[0.38,0.00],[0.35,-0.10],[0.35,-0.36]]);
  } else {
    cut([[-0.38,0],[0,0.42],[0.38,0],[0,-0.42]]);
  }
  mesh(plate, extrude(shape, 0.075, 0.012), p.silver, 0, 0, 0.12);
  const mark = texture(512, 512, (ctx) => {
    ctx.fillStyle = ctx.strokeStyle = p.color("info", 900);
    ctx.font = "30px monospace";
    ctx.fillText(({ Hydrogen: "01", Neon: "10", Argon: "18" } as Record<string, string>)[name] ?? "·", 42, 76);
    ctx.font = "bold 25px sans-serif";
    ctx.fillText(name === "Other" ? "AGY" : name.toUpperCase(), 42, 453);
    ctx.lineWidth = 2;
    ctx.strokeRect(21, 20, 470, 472);
    for (let i = 0; i < 7; i++) ctx.fillRect(408 + i * 8, 48, 2, i % 2 ? 10 : 18);
  });
  mesh(plate, new T.PlaneGeometry(2, 1.92), new T.MeshBasicMaterial({ map: mark, transparent: true, depthWrite: false }), 0, 0, 0.213).castShadow = false;
  const nucleus = mesh(group, new T.SphereGeometry(0.30, 32, 20), p.brass, 0.81, 0.64, -0.56);
  const shells = name === "Argon" ? 3 : name === "Neon" ? 2 : 0;
  for (let i = 0; i < shells; i++) {
    const r = 1.27 + i * 0.21;
    const orbit = mesh(group, circle(r, 0.029), i % 2 ? p.silver : p.brass, 0.17, 0.12, -0.71 - i * 0.20);
    orbit.rotation.set(0.30 + i * 0.37, -0.50 + i * 0.28, 0.28 + i * 0.6);
    const a = 0.55 + i * 1.8;
    mesh(orbit, new T.SphereGeometry(0.093, 16, 12), p.patina, r * Math.cos(a), r * Math.sin(a), 0);
  }
  if (!shells) nucleus.position.set(0.97, 0.85, -0.18);
  return group;
}

function sheet(parent: T.Object3D, points: XYZ[], indices: number[], material: T.Material) {
  const geo = new T.BufferGeometry();
  geo.setAttribute("position", new T.Float32BufferAttribute(points.flat(), 3));
  geo.setAttribute("uv", new T.Float32BufferAttribute(points.flatMap(([x, y]) => [x * 0.45 + 0.5, y * 0.45 + 0.5]), 2));
  geo.setIndex(indices);
  geo.computeVertexNormals();
  return mesh(parent, geo, material);
}

/** A continuous bent sheet between the spine and the fore edge. UVs follow the page so the score bends with it. */
function page(parent: T.Object3D, side: number, height: number, lift: number, material: T.Material) {
  const points: XYZ[] = [], uv: number[] = [], indices: number[] = [];
  for (let i = 0; i <= 12; i++) {
    const u = i / 12, x = side * u * 1.23;
    const y = height + Math.sin(u * Math.PI) * lift + u * (0.12 + lift * 0.12);
    points.push([x, y, 0.83], [x, y, -0.83]);
    uv.push(side > 0 ? u : 1 - u, 0, side > 0 ? u : 1 - u, 1);
    if (i) {
      const k = i * 2;
      indices.push(k - 2, k, k - 1, k, k + 1, k - 1);
    }
  }
  const part = sheet(parent, points, indices, material);
  part.geometry.setAttribute("uv", new T.Float32BufferAttribute(uv, 2));
}

function paperwork(p: Paint, name: Sculpture) {
  const book = new T.Group();
  if (name === "Haiku") {
    // One folded crane, built from flat facets sharing the ridge between its wings.
    const nose: XYZ = [0.83, 0.47, 0.07], tail: XYZ = [-1.22, 0.52, 0.16];
    const breast: XYZ = [0.37, -0.47, 0.19], back: XYZ = [-0.38, 0.02, -0.02];
    for (const side of [-1, 1]) {
      const wing: XYZ = [-0.35, side > 0 ? 0.95 : 1.14, side * 1.11];
      const fold: XYZ = [-0.59, 0.28, side * 0.42];
      sheet(book, [tail, back, fold, wing, breast, nose, [1.02, 0.29, 0.09]], [0,1,2, 2,1,3, 3,1,4, 1,5,4, 5,6,4], p.paper);
    }
    book.rotation.y = -0.54;
  } else if (name === "Sonnet" || name === "Other") {
    // Concertina: alternating creases, with the outer folds opening wider like a fan.
    const folds = 10;
    for (let i = 0; i < folds; i++) {
      const edge = (j: number): XYZ[] => {
        const a = -1.24 + 2.48 * j / folds, z = j % 2 ? -0.29 : 0.30;
        return [[0.29 * Math.sin(a), -0.76 + 0.26 * Math.cos(a), z * 0.22],
                [1.47 * Math.sin(a), -0.49 + 1.73 * Math.cos(a), z]];
      };
      sheet(book, [...edge(i), ...edge(i + 1)], [0,2,1, 1,2,3], i % 2 ? p.shade : p.paper);
    }
    book.rotation.set(-0.17, -0.22, -0.09);
  } else {
    const fable = name === "Fable";
    for (const side of [-1, 1]) {
      const cover = mesh(book, new T.BoxGeometry(1.28, 0.08, 1.78), p.cover, side * 0.62, -0.43, 0);
      cover.rotation.z = side * 0.1;
      for (let j = 0; j < 6; j++)
        page(book, side, -0.41 + j * 0.046, (fable ? 0.10 : 0.34) + j * 0.035, !fable && j === 5 ? p.music : j % 2 ? p.paper : p.shade);
    }
    if (fable) {
      const cutout = (points: XY[], x: number, z: number, mat: T.Material, turn: number) => {
        const shape = outline(points);
        const part = mesh(book, extrude(shape, 0.024, 0.006), mat, x, -0.12, z);
        part.rotation.y = turn;
        return part;
      };
      // Two folded landscape spreads, a crenellated tower and a pair of trees rising from the book.
      cutout([[-1,0],[-0.51,0.91],[-0.27,0.56],[0.09,1.37],[0.57,0.68],[0.80,0.91],[1.10,0]], 0, -0.45, p.shade, -0.08);
      const tower = cutout([[-0.31,0],[-0.31,0.82],[-0.40,0.82],[-0.13,1.16],[0.13,0.82],[0.07,0.82],[0.07,0.48],[0.22,0.48],[0.22,0.70],[0.35,0.70],[0.35,0.51],[0.48,0.51],[0.48,0]], 0.10, -0.10, p.clay, -0.20);
      // A side flap holds the tower upright, with a doorway and windows printed on the front.
      const arch = new T.Shape();
      arch.moveTo(-0.075, 0); arch.lineTo(-0.075, 0.15);
      arch.absarc(0, 0.15, 0.075, Math.PI, 0, true); arch.lineTo(0.075, 0); arch.closePath();
      mesh(tower, new T.ShapeGeometry(arch), p.cover, -0.10, 0.05, 0.035);
      for (const x of [-0.21, -0.05]) mesh(tower, new T.PlaneGeometry(0.055, 0.12), p.cover, x, 0.67, 0.036);
      sheet(tower, [[0.07,0.82,0],[0.25,0.75,-0.22],[0.25,0,-0.22],[0.07,0,0]], [0,1,2, 0,2,3], p.shade);
      sheet(tower, [[-0.13,1.16,0],[0.13,0.82,0],[0.29,0.75,-0.23],[0.02,1.08,-0.23]], [0,1,2, 0,2,3], p.cover);
      for (const [x, z, size] of [[-0.65,0.22,0.87],[0.72,0.05,0.65],[-0.38,0.48,0.53]]) {
        const tree: XY[] = [[-0.06,0],[-0.06,0.19],[-0.31,0.19],[-0.13,0.46],[-0.24,0.46],[0,0.93],[0.24,0.46],[0.13,0.46],[0.31,0.19],[0.06,0.19],[0.06,0]];
        const fir = cutout(tree.map(([a,b]) => [a*size,b*size]), x, z, p.sage, 0.23);
        sheet(fir, [[0,0.93*size,0.04],[0.15*size,0.25*size,0.03],[0,0.19*size,0.19*size]], [0,1,2], p.sage);
      }
    }
    book.rotation.set(fable ? 0.22 : 0.56, -0.24, -0.06);
  }
  return book;
}

function celestial(p: Paint, name: Sculpture) {
  const group = new T.Group();
  const blue = p.color("info", 200), gold = p.color("warning", 300), lilac = p.color("primary", 300);
  if (name === "Luna") {
    const crescent = new T.Shape();
    crescent.moveTo(0.52, 1.12);
    crescent.bezierCurveTo(-1.46, 1.07, -1.43, -1.25, 0.48, -1.09);
    crescent.bezierCurveTo(-0.51, -0.68, -0.57, 0.59, 0.52, 1.12);
    mesh(group, extrude(crescent, 0.13, 0.088), p.glass(blue));
    const inner = mesh(group, extrude(crescent, 0.012, 0.022), p.glow(p.color("info", 400), 0.5), -0.10, 0, 0.06);
    inner.scale.set(0.80, 0.87, 1);
    group.rotation.set(0.08, -0.24, -0.34);
  } else if (name === "Terra") {
    const radius = 0.97, bands = 15, axis = new T.Vector3(0, 0, 1);
    for (let i = 0; i < bands; i++) {
      const geo = new T.SphereGeometry(radius, 64, 8, 0, Math.PI * 2, Math.PI * i / bands, Math.PI / bands);
      const pos = geo.attributes.position, v = new T.Vector3();
      for (let j = 0; j < pos.count; j++) {
        v.fromBufferAttribute(pos, j);
        v.applyAxisAngle(axis, 0.30 * Math.sin(v.z * 4) + 0.25 * Math.cos(v.x * 4));
        pos.setXYZ(j, v.x, v.y, v.z);
      }
      geo.computeVertexNormals();
      const c = [p.color("info", 500), blue, p.color("success", 400), p.color("primary", 400), p.color("info", 300)][i % 5];
      mesh(group, geo, p.metal(c, 0.23, 0.29));
    }
    mesh(group, new T.SphereGeometry(1.055, 64, 40), p.glass(blue));
    const ring = mesh(group, circle(1.30, 0.019), p.brass);
    ring.rotation.set(1.02, 0.39, -0.28);
    group.rotation.z = -0.20;
  } else if (name === "Sol") {
    mesh(group, new T.SphereGeometry(0.61, 40, 28), p.glow(gold, 2.2));
    mesh(group, new T.SphereGeometry(1.04, 56, 32), p.glass(p.color("warning", 200)));
    for (let i = 0; i < 5; i++) {
      const plasma = mesh(group, circle(0.78 + i * 0.032, 0.018), p.glow(gold, 1.4));
      plasma.rotation.set(i * 0.7, i * 0.97, 0.23);
    }
    halo(group, p, gold, 3.7, 0.37);
  } else {
    const points: XYZ[] = [[-0.94,0.34,0.14],[-0.46,-0.59,0.26],[0.03,0.04,0.03],[0.41,0.81,-0.19],[1.02,0.39,0.06],[0.69,-0.65,-0.24],[-0.58,0.96,-0.35]];
    const colors = [blue, lilac, gold], cores = colors.map((c) => p.glow(c, 1.9));
    const shells = colors.map((c) => {
      const shell = p.glass(c);
      shell.transparent = true; shell.opacity = 0.53; shell.depthWrite = false; shell.thickness = 0.16;
      return shell;
    });
    for (const [i, xyz] of points.entries()) {
      const star = new T.Group();
      group.add(star);
      star.position.set(...xyz);
      const r = [0.22,0.15,0.36,0.24,0.14,0.22,0.09][i];
      mesh(star, new T.IcosahedronGeometry(r, 1), shells[i % 3]);
      mesh(star, new T.OctahedronGeometry(r * 0.65), cores[i % 3]);
      for (const roll of [0, Math.PI / 2]) {
        const ray = mesh(star, new T.OctahedronGeometry(r * 0.16), cores[i % 3]);
        ray.scale.y = 10.5;
        ray.rotation.z = roll + i * 0.13;
      }
      halo(star, p, colors[i % 3], r * 8.4, 0.50);
    }
    const thread = new T.LineBasicMaterial({ color: lilac, transparent: true, opacity: 0.46 });
    for (const chain of [[0,1,2,3,4],[2,5,4],[0,6,3]]) {
      group.add(new T.Line(new T.BufferGeometry().setFromPoints(chain.map((i) => new T.Vector3(...points[i]))), thread));
    }
  }
  return group;
}

export function sculpture(tool: Tool, name: Sculpture, p: Paint) {
  return tool === "agy" ? tile(p, name) : tool === "claude" ? paperwork(p, name) : celestial(p, name);
}
