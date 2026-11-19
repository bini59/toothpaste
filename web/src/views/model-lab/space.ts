import * as T from "three";
import { highlight, label, mesh, pickable, ring, seeded, texture, world, type Paint } from "./materials";

const tau = 2 * Math.PI,
  radians = T.MathUtils.degToRad;
export function nightSky(p: Paint) {
  return texture(1024, 640, (ctx) => {
    const rand = seeded(45),
      base = new T.Color(p.color("grey", 900)).multiplyScalar(0.23).getStyle();
    ctx.fillStyle = base;
    ctx.fillRect(0, 0, 1024, 640);
    ctx.fillStyle = p.color("grey", 200);
    for (let i = 0; i < 82; i++) {
      ctx.globalAlpha = 0.1 + rand() * 0.3;
      ctx.beginPath();
      ctx.arc(rand() * 1024, rand() * 640, 0.28 + rand() * 0.55, 0, tau);
      ctx.fill();
    }
  });
}

export function milkyWay(p: Paint, image: T.Texture) {
  const w = world(2.6, -0.45, 0.04);
  const disk = mesh(w.root, new T.PlaneGeometry(15.4, 10.27), new T.MeshBasicMaterial({ map: image }));
  pickable(w, disk, "Astra");
  const sun = mesh(
    w.root,
    new T.SphereGeometry(0.055, 16, 10),
    new T.MeshBasicMaterial({ color: p.color("warning", 100) }),
    w.portal.x,
    w.portal.y,
    w.portal.z
  );
  const reach = mesh(
    sun,
    new T.SphereGeometry(0.41, 16, 10),
    new T.MeshBasicMaterial({ visible: false }),
    0,
    0,
    0.08
  );
  pickable(w, reach, "@solar");
  const locator = ring(w.root, 0.18, p.color("grey", 200), 0.75);
  locator.position.copy(w.portal);
  locator.userData.annotation = true;
  label(w.root, p, "태양계", w.portal.x, w.portal.y - 0.47, 0.08, 0.43);
  return w;
}

function globe(p: Paint, earthMap: T.Texture, radius: number) {
  const earth = new T.Group();
  mesh(earth, new T.SphereGeometry(radius, 80, 48), new T.MeshPhongMaterial({ map: earthMap, shininess: 2 }));
  earth.rotation.set(radians(23), radians(-214), 0);
  return earth;
}

// Pack the nine surface maps into a single request. Inset by half a texel to keep neighbouring map edges out.
function planetSphere(radius: number, tile: number) {
  const sphere = new T.SphereGeometry(radius, 52, 32),
    uv = sphere.getAttribute("uv");
  for (let i = 0; i < uv.count; i++) {
    const u = T.MathUtils.clamp(uv.getX(i), 0.001, 0.999),
      v = T.MathUtils.clamp(uv.getY(i), 0.002, 0.998);
    uv.setXY(i, ((tile % 3) + u) / 3, (2 - Math.floor(tile / 3) + v) / 3);
  }
  return sphere;
}
export function solarSystem(p: Paint, earthMap: T.Texture, atlas: T.Texture) {
  const w = world(),
    sunX = -5.15;
  const sun = mesh(
    w.root,
    planetSphere(1.33, 8),
    new T.MeshBasicMaterial({ map: atlas, color: p.color("warning", 100) }),
    sunX,
    0.13
  );
  pickable(w, sun, "Sol");
  const tag = (key: string, pos: T.Vector3, offsetY: number, offsetX = 0) => {
    const y = pos.y + offsetY;
    label(w.root, p, key, pos.x + offsetX, y, pos.z, 0.43);
    const mark = new T.Line(
      new T.BufferGeometry().setFromPoints([new T.Vector3(-0.18, 0, 0), new T.Vector3(0.18, 0, 0)]),
      new T.LineBasicMaterial({ color: p.color("warning", 200) })
    );
    mark.position.set(pos.x + offsetX, y - 0.24, pos.z);
    w.root.add(mark);
    highlight(w, key, mark);
  };
  tag("Sol", sun.position, -1.7);
  // Look six degrees down on the ecliptic. Short arcs behind the limbs convey their order in depth.
  const plane = new T.Group();
  w.root.add(plane);
  plane.position.set(sunX, 0.13, 0);
  plane.rotation.set(-radians(84), 0, radians(3), "ZXY");
  const planets = [
    { r: 1.86, a: -0.3, size: 0.095 },
    { r: 2.65, a: 0.31, size: 0.22 },
    { r: 4.05, a: -0.38, size: 0.38 },
    { r: 5.52, a: 0.27, size: 0.19 },
    { r: 6.78, a: -0.1, size: 0.65 },
    { r: 9.32, a: 0.14, size: 0.48 },
    { r: 10.95, a: 0.13, size: 0.26 },
    { r: 11.29, a: -0.025, size: 0.24 },
  ];
  planets.forEach(({ r, a, size }, i) => {
    ring(plane, r, p.color("grey", 400), i === 2 ? 0.36 : 0.19);
    const pos = new T.Vector3(r * Math.cos(a), r * Math.sin(a), 0).applyEuler(plane.rotation).add(plane.position);
    const body =
      i === 2
        ? globe(p, earthMap, size)
        : new T.Mesh(planetSphere(size, i), new T.MeshStandardMaterial({ map: atlas, roughness: 0.96 }));
    body.position.copy(pos);
    w.root.add(body);
    if (i === 2) {
      w.portal.copy(pos);
      pickable(w, body, "Terra");
      tag("Terra", pos, -0.8);
      const lunarPos = pos.clone().add(new T.Vector3(0.65, 0.61, 0.09));
      const moon = mesh(
        w.root,
        planetSphere(0.16, 2),
        new T.MeshStandardMaterial({ map: atlas, roughness: 0.98 })
      );
      moon.position.copy(lunarPos);
      pickable(w, moon, "Luna");
      tag("Luna", lunarPos, 0.49);
      const hit = mesh(moon, new T.SphereGeometry(0.28, 16, 12), new T.MeshBasicMaterial({ visible: false }));
      pickable(w, hit, "Luna");
    }
    if (i === 5)
      for (const [inner, outer, tone] of [
        [0.63, 0.79, 300],
        [0.81, 0.99, 400],
      ]) {
        const belt = mesh(
          body,
          new T.RingGeometry(inner, outer, 80),
          new T.MeshBasicMaterial({ color: p.tone("warning", tone, "grey", 400, 0.6), side: T.DoubleSide })
        );
        belt.rotation.set(-1.27, 0, 0.17, "ZXY");
      }
  });
  return w;
}

export function earthApproach(p: Paint, earthMap: T.Texture) {
  const w = world(),
    r = 2.95,
    earth = globe(p, earthMap, r);
  w.root.add(earth);
  const lat = radians(35.012),
    lon = radians(135.768);
  const pin = new T.Vector3(Math.cos(lat) * Math.cos(lon), Math.sin(lat), -Math.cos(lat) * Math.sin(lon))
    .multiplyScalar(r * 1.012)
    .applyEuler(earth.rotation);
  w.portal.copy(pin);
  const dot = mesh(
    w.root,
    new T.SphereGeometry(0.034, 12, 8),
    new T.MeshBasicMaterial({ color: p.color("warning", 100) })
  );
  dot.position.copy(pin);
  return w;
}
