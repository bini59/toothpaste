import * as T from "three";
import { highlight, label, mesh, pickable, ring, world, type Paint } from "./materials";

/** Positions are fractions measured on the source photograph, from its upper left. */
export function locationPlate(
  map: T.Texture,
  targetX: number,
  targetY: number,
  width = 18.9,
  relief?: "street" | "aisle"
) {
  const height = (width * 2) / 3,
    w = world((targetX - 0.5) * width, (0.5 - targetY) * height);
  const panel = new T.PlaneGeometry(width, height, relief ? 50 : 1, relief ? 32 : 1);
  if (relief) {
    const pos = panel.getAttribute("position"),
      uv = panel.getAttribute("uv");
    w.projectPlate = (distance) => {
      for (let i = 0; i < pos.count; i++) {
        const u = uv.getX(i),
          v = uv.getY(i);
        // Reproject each original image ray onto the nearby shelves / foreground roofs.
        const side = Math.abs(u - targetX),
          bottom = Math.max(0, 1 - targetY - v);
        const depth =
          relief === "aisle"
            ? 2.9 * T.MathUtils.smoothstep(Math.max(side * 1.75, bottom * 1.45), 0.2, 0.84)
            : 1.65 * T.MathUtils.smoothstep(bottom + side * 0.39, 0.13, 0.61);
        const rayScale = 1 - depth / distance;
        pos.setXYZ(i, (u - 0.5) * width * rayScale, (v - 0.5) * height * rayScale, depth);
      }
      pos.needsUpdate = true;
      panel.computeBoundingSphere();
    };
  }
  mesh(w.root, panel, new T.MeshBasicMaterial({ map }));
  return w;
}

export function atomicModels(p: Paint) {
  const w = world();
  const graphite = p.tone("grey", 800, "warning", 900, 0.13);
  const core = new T.MeshStandardMaterial({ color: p.tone("grey", 800, "warning", 900, 0.12), roughness: 1 });
  const dot = new T.MeshBasicMaterial({ color: graphite });
  const ball = new T.SphereGeometry(1, 32, 20);
  const elements = [
    { name: "Hydrogen", symbol: "H", shells: [1] },
    { name: "Neon", symbol: "Ne", shells: [2, 8] },
    { name: "Argon", symbol: "Ar", shells: [2, 8, 8] },
  ];
  for (const [i, { name, symbol, shells }] of elements.entries()) {
    const x = (i - 1) * 4.0,
      atom = new T.Group();
    atom.position.set(x, 0.2, 0);
    w.root.add(atom);
    mesh(atom, ball, core).scale.setScalar(0.2 + i * 0.052);
    for (const [n, count] of shells.entries()) {
      const r = 0.69 + n * 0.365,
        shell = new T.Group();
      shell.rotation.set(0.23, -0.1, 0.035);
      atom.add(shell);
      ring(shell, r, graphite, 0.7);
      for (let j = 0; j < count; j++) {
        const theta = 2 * Math.PI * (j / count + 0.19 + 0.113 * n);
        mesh(shell, ball, dot, r * Math.cos(theta), r * Math.sin(theta)).scale.setScalar(0.054);
      }
    }
    const hit = mesh(atom, new T.SphereGeometry(0.92 + i * 0.32, 20, 12), new T.MeshBasicMaterial({ visible: false }));
    pickable(w, hit, name);
    label(w.root, p, symbol, x, -1.7, 0, 0.78, 512, true);
    label(w.root, p, name, x, -2.31, 0, 0.48, 512, true);
    const underline = mesh(w.root, new T.PlaneGeometry(0.49, 0.034), dot, x, -2.72, 0);
    highlight(w, name, underline);
  }
  return w;
}
