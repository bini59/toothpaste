import * as T from "three";
import { halo, highlight, label, mesh, pickable, ring, seeded, texture, world, type Paint } from "./materials";
import { artworks, type PrintName } from "./catalog";

export function japanPrints(p: Paint, region: T.Texture, prints: Record<PrintName, T.Texture>) {
  const w = world();
  // The satellite crop stays visible through the middle of the four prints.
  mesh(
    w.root,
    new T.PlaneGeometry(24, 16),
    new T.MeshBasicMaterial({ map: region, color: p.color("info", 200) }),
    0,
    0,
    -1.6
  );
  const mount = new T.MeshStandardMaterial({ color: p.color("warning", 100), roughness: 0.85 });
  const ink = new T.MeshStandardMaterial({ color: p.color("grey", 900), roughness: 0.8 });
  (Object.keys(artworks) as PrintName[]).forEach((name, i) => {
    const side = i % 2 ? 1 : -1,
      frame = new T.Group();
    frame.position.set(side * 4.1, i < 2 ? 1.73 : -1.32, 0.12);
    frame.rotation.y = -side * 0.1;
    w.root.add(frame);
    mesh(frame, new T.BoxGeometry(2.7, 2.75, 0.12), ink, 0, -0.04, -0.07);
    mesh(frame, new T.BoxGeometry(2.6, 2.64, 0.08), mount);
    const picture = mesh(
      frame,
      new T.PlaneGeometry(2.45, 2.45),
      new T.MeshBasicMaterial({ map: prints[name] }),
      0,
      0.02,
      0.045
    );
    pickable(w, picture, name);
    label(frame, p, name, 0, -1.51, 0.16, 0.48);
    const points = [
      [-1.37, 1.38],
      [1.37, 1.38],
      [1.37, -1.4],
      [-1.37, -1.4],
    ].map(([x, y]) => new T.Vector3(x, y, 0.14));
    const border = new T.LineLoop(
      new T.BufferGeometry().setFromPoints(points),
      new T.LineBasicMaterial({ color: p.color("warning", 200) })
    );
    frame.add(border);
    highlight(w, name, border);
    if (name === "Haiku") w.portal.copy(frame.position).add(new T.Vector3(0, 0.08, 0.06));
  });
  return w;
}

export function haikuDetail(map: T.Texture) {
  const w = world(0, 0.22, 0);
  // The same print as the selectable Haiku frame, so the dew stays in place through the last zoom.
  mesh(w.root, new T.PlaneGeometry(16, 16), new T.MeshBasicMaterial({ map }));
  return w;
}

export function microscopicField(p: Paint) {
  return texture(960, 600, (ctx) => {
    const random = seeded(197),
      w = ctx.canvas.width,
      h = ctx.canvas.height;
    const deep = new T.Color(p.color("info", 900))
      .lerp(new T.Color(p.color("grey", 900)), 0.5)
      .multiplyScalar(0.47)
      .getStyle();
    const wash = ctx.createRadialGradient(w * 0.46, h * 0.47, 0, w * 0.5, h * 0.47, w * 0.6);
    wash.addColorStop(0, p.color("info", 900));
    wash.addColorStop(1, deep);
    ctx.fillStyle = wash;
    ctx.fillRect(0, 0, w, h);
    const tint = p.color("info", 200);
    for (let i = 0; i < 210; i++) {
      const x = random() * w,
        y = random() * h,
        r = 0.6 + random() ** 4 * 16;
      const glow = ctx.createRadialGradient(x, y, 0, x, y, r);
      glow.addColorStop(0, tint + "24");
      glow.addColorStop(1, tint + "00");
      ctx.fillStyle = glow;
      ctx.fillRect(x - r, y - r, r * 2, r * 2);
    }
  });
}

export function atomicModels(p: Paint) {
  const w = world();
  const nuclei = [p.color("warning", 400), p.color("info", 300)].map(
    (color) => new T.MeshStandardMaterial({ color, metalness: 0.25, roughness: 0.34 })
  );
  const nucleonSphere = new T.SphereGeometry(1, 22, 16),
    electronSphere = new T.SphereGeometry(1, 16, 12);
  const electron = new T.MeshBasicMaterial({ color: p.color("info", 100) });
  const orbiting: { group: T.Group; speed: number }[] = [];
  const samples = [
    { name: "Hydrogen", symbol: "H", protons: 1, neutrons: 0, shells: [1] },
    { name: "Neon", symbol: "Ne", protons: 10, neutrons: 10, shells: [2, 8] },
    { name: "Argon", symbol: "Ar", protons: 18, neutrons: 22, shells: [2, 8, 8] },
  ];
  samples.forEach(({ name, symbol, protons, neutrons, shells }, i) => {
    const atom = new T.Group(),
      x = (i - 1) * 4.05;
    atom.position.set(x, 0.15, 0);
    w.root.add(atom);
    const count = protons + neutrons,
      poses = [[], []] as [T.Matrix4[], T.Matrix4[]];
    for (let k = 0; k < count; k++) {
      const v = new T.Vector3(),
        size = count === 1 ? 0.29 : 0.145;
      if (count > 1) {
        const y = 1 - 2 * ((k + 0.5) / count),
          angle = k * 2.399963;
        const r = (i === 1 ? 0.35 : 0.49) * Math.sqrt(Math.max(0, 1 - y * y));
        v.set(r * Math.cos(angle), y * (i === 1 ? 0.35 : 0.49), r * Math.sin(angle));
      }
      const proton = Math.floor(((k + 1) * protons) / count) > Math.floor((k * protons) / count);
      poses[proton ? 0 : 1].push(new T.Matrix4().makeScale(size, size, size).setPosition(v));
    }
    poses.forEach((positions, n) => {
      if (!positions.length) return;
      const cluster = new T.InstancedMesh(nucleonSphere, nuclei[n], positions.length);
      positions.forEach((m, j) => cluster.setMatrixAt(j, m));
      atom.add(cluster);
    });
    halo(atom, p, p.color(i === 2 ? "primary" : "info", 400), 3.9 + i * 0.45, 0.24);
    shells.forEach((population, s) => {
      const radius = 0.97 + s * 0.37,
        plane = new T.Group();
      plane.rotation.set(0.91 + s * 0.07, 0.15 + s * 0.1, -0.36 + s * 1.16, "ZXY");
      atom.add(plane);
      ring(plane, radius, p.color(s === 1 ? "primary" : "info", 200), 0.85);
      const carrier = new T.Group();
      plane.add(carrier);
      for (let e = 0; e < population; e++) {
        const a = (2 * Math.PI * (e + 0.14 + s * 0.25)) / population;
        const bead = mesh(carrier, electronSphere, electron, radius * Math.cos(a), radius * Math.sin(a));
        bead.scale.setScalar(s === 0 ? 0.075 : 0.057);
      }
      orbiting.push({ group: carrier, speed: (s % 2 ? -1 : 1) * (0.27 + 0.08 * s) });
    });
    // A solid pick volume also covers the empty space between electron shells.
    const pick = mesh(
      atom,
      new T.SphereGeometry(0.99 + 0.37 * (shells.length - 1), 24, 16),
      new T.MeshBasicMaterial({ visible: false })
    );
    pickable(w, pick, name);
    label(w.root, p, symbol + " · " + name, x, -2.0, 0, 0.57);
    const mark = ring(w.root, 0.92, p.color("info", 200), 0.86);
    mark.scale.y = 0.15;
    mark.position.set(x, -2.52, 0);
    highlight(w, name, mark);
  });
  return {
    world: w,
    animate: (seconds: number) => {
      for (const { group, speed } of orbiting) group.rotation.z = seconds * speed;
    },
  };
}
