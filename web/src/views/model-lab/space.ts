import * as T from "three";
import { halo, highlight, label, mesh, pickable, ring, seeded, texture, world, type Paint } from "./materials";

const tau = 2 * Math.PI,
  radians = T.MathUtils.degToRad;
export function nightSky(p: Paint) {
  return texture(1200, 720, (ctx) => {
    const rand = seeded(3091),
      width = ctx.canvas.width,
      height = ctx.canvas.height;
    const field = Float32Array.from({ length: 128 * 128 }, rand);
    // Smooth noise at three scales gives the band of dust filaments instead of a flat stripe.
    const noise = (x: number, y: number) => {
      const ix = Math.floor(x),
        iy = Math.floor(y);
      const sx = x - ix,
        sy = y - iy,
        fx = sx * sx * (3 - 2 * sx),
        fy = sy * sy * (3 - 2 * sy);
      const a = (iy & 127) << 7,
        b = ((iy + 1) & 127) << 7,
        l = ix & 127,
        r = (ix + 1) & 127;
      return T.MathUtils.lerp(
        T.MathUtils.lerp(field[a + l], field[a + r], fx),
        T.MathUtils.lerp(field[b + l], field[b + r], fx),
        fy
      );
    };
    const rgb = (family: string, step: number) => new T.Color(p.color(family, step)).convertLinearToSRGB();
    const base = rgb("grey", 900),
      blue = rgb("info", 400),
      rose = rgb("secondary", 400);
    const pixels = ctx.createImageData(width, height);
    for (let y = 0; y < height; y++)
      for (let x = 0; x < width; x++) {
        const u = x / width,
          v = y / height;
        const warp = noise(u * 5 + 11, v * 4 + 8);
        const offset = v - (0.85 - u * 0.6) - 0.13 * (warp - 0.5);
        const band = Math.exp((-offset * offset) / 0.011);
        const dust =
          noise(u * 15 + warp * 3, v * 11 + 21) * 0.52 + noise(u * 51, v * 36) * 0.3 + noise(u * 160, v * 96) * 0.18;
        const veil = band * Math.max(0, dust - 0.24) ** 2 * 0.9;
        const tint = noise(u * 7 + 33, v * 4) * 0.9;
        const at = 4 * (y * width + x);
        pixels.data[at] = 255 * (base.r * 0.41 + veil * T.MathUtils.lerp(blue.r, rose.r, tint));
        pixels.data[at + 1] = 255 * (base.g * 0.49 + veil * T.MathUtils.lerp(blue.g, rose.g, tint));
        pixels.data[at + 2] = 255 * (base.b * 0.72 + veil * T.MathUtils.lerp(blue.b, rose.b, tint));
        pixels.data[at + 3] = 255;
      }
    ctx.putImageData(pixels, 0, 0);
    ctx.fillStyle = p.color("info", 100);
    for (let i = 0; i < 1450; i++) {
      ctx.globalAlpha = 0.14 + rand() ** 2 * 0.67;
      const x = rand() * width;
      const y = i < 710 ? rand() * height : (0.85 - (x / width) * 0.6 + (rand() + rand() - 1) * 0.2) * height;
      ctx.beginPath();
      ctx.arc(x, y, 0.24 + rand() ** 3 * 0.95, 0, tau);
      ctx.fill();
    }
  });
}

function star(parent: T.Object3D, p: Paint, color: string, radius: number, x: number, y: number, z: number) {
  const group = new T.Group();
  group.position.set(x, y, z);
  parent.add(group);
  const core = new T.MeshBasicMaterial({ color: p.color("grey", 50) });
  const center = mesh(group, new T.SphereGeometry(radius, 18, 12), core);
  for (const a of [0, Math.PI / 2]) {
    const ray = mesh(
      group,
      new T.OctahedronGeometry(radius * 0.35),
      new T.MeshBasicMaterial({ color, transparent: true, opacity: 0.76 })
    );
    ray.scale.set(0.6, 10, 0.4);
    ray.rotation.z = a - 0.15;
  }
  halo(group, p, color, radius * 27, 0.79);
  return center;
}

export function constellation(p: Paint) {
  const w = world(0.65, 0.28);
  const coords = [
    [-5.1, -1.15, -0.8],
    [-3.1, 1.3, -0.3],
    [-0.82, -0.62, 0.35],
    [0.65, 0.28, 0],
    [2.65, 1.87, -0.4],
    [5.0, 1.05, -0.9],
    [-1.8, -2.32, -1.2],
  ];
  const path = new T.LineBasicMaterial({ color: p.color("info", 300), transparent: true, opacity: 0.25 });
  for (const track of [
    [0, 1, 2, 3, 4, 5],
    [0, 6, 2],
  ]) {
    const pts = track.map((i) => new T.Vector3(...coords[i]));
    w.root.add(new T.Line(new T.BufferGeometry().setFromPoints(pts), path));
  }
  coords.forEach(([x, y, z], i) => {
    const sun = i === 3;
    const center = star(
      w.root,
      p,
      p.color(sun ? "warning" : i % 2 ? "info" : "primary", sun ? 300 : 200),
      sun ? 0.15 : 0.052 + (i % 3) * 0.026,
      x,
      y,
      z
    );
    const hit = mesh(
      center.parent!,
      new T.SphereGeometry(sun ? 0.54 : 0.28, 12, 8),
      new T.MeshBasicMaterial({ visible: false })
    );
    pickable(w, hit, "@solar");
    if (sun) {
      ring(w.root, 0.54, p.color("warning", 300), 0.6).position.set(x, y, z);
      label(w.root, p, "태양계", x, y - 0.95, z, 0.59);
    }
  });
  return w;
}

function globe(p: Paint, earthMap: T.Texture, radius: number) {
  const earth = new T.Group();
  mesh(earth, new T.SphereGeometry(radius, 80, 48), new T.MeshPhongMaterial({ map: earthMap, shininess: 5 }));
  const air = new T.ShaderMaterial({
    uniforms: { tint: { value: new T.Color(p.color("info", 300)) } },
    vertexShader:
      "varying vec3 n; varying vec3 v; void main(){vec4 mv=modelViewMatrix*vec4(position,1.); n=normalize(normalMatrix*normal); v=normalize(-mv.xyz); gl_Position=projectionMatrix*mv;}",
    fragmentShader:
      "varying vec3 n; varying vec3 v; uniform vec3 tint; void main(){ float a=pow(1.-clamp(dot(normalize(n),normalize(v)),0.,1.),3.4); gl_FragColor=vec4(tint,a*0.62); }",
    transparent: true,
    depthWrite: false,
    blending: T.AdditiveBlending,
  });
  mesh(earth, new T.SphereGeometry(radius * 1.014, 64, 40), air);
  earth.rotation.set(radians(23), radians(-214), 0);
  return earth;
}

function planetTexture(p: Paint, family: string, banded: boolean, seed: number) {
  const rand = seeded(seed);
  return texture(512, 256, (ctx) => {
    const base = ctx.createLinearGradient(0, 0, 0, 256);
    base.addColorStop(0, p.color(family, 400));
    base.addColorStop(0.53, p.color(family, 200));
    base.addColorStop(1, p.color(family, 600));
    ctx.fillStyle = base;
    ctx.fillRect(0, 0, 512, 256);
    for (let i = 0; i < (banded ? 135 : 3400); i++) {
      const x = rand() * 512,
        y = rand() * 256;
      ctx.fillStyle = p.color(family, i % 3 ? 600 : 100);
      ctx.globalAlpha = 0.1 + rand() * 0.2;
      if (banded) {
        ctx.beginPath();
        ctx.moveTo(-2, y);
        ctx.bezierCurveTo(120, y + 6, 360, y - 6, 514, y);
        ctx.lineTo(514, y + rand() * 7);
        ctx.bezierCurveTo(360, y - 5, 120, y + 8, -2, y + 3);
        ctx.fill();
      } else {
        ctx.beginPath();
        ctx.ellipse(x, y, rand() * 6 + 0.5, rand() * 3 + 0.6, 0, 0, tau);
        ctx.fill();
      }
    }
  });
}

function solarSurface(p: Paint) {
  // Granulation is evaluated in object space, so the texture stays on the Sun as the camera moves.
  return new T.ShaderMaterial({
    uniforms: {
      amber: { value: new T.Color(p.color("warning", 500)) },
      gold: { value: new T.Color(p.color("warning", 300)) },
      light: { value: new T.Color(p.color("warning", 50)) },
    },
    vertexShader: [
      "varying vec3 point; varying vec3 outward; varying vec3 viewDir;",
      "void main(){point=normal; vec4 view=modelViewMatrix*vec4(position,1.);",
      "outward=normalMatrix*normal; viewDir=-view.xyz; gl_Position=projectionMatrix*view;}",
    ].join("\n"),
    fragmentShader: [
      "uniform vec3 amber,gold,light; varying vec3 point,outward,viewDir;",
      "float hash(vec3 p){p=fract(p*.1031);p+=dot(p,p.yzx+33.33);return fract((p.x+p.y)*p.z);}",
      "float noise3(vec3 p){vec3 i=floor(p),u=fract(p);u=u*u*(3.-2.*u);",
      "return mix(mix(mix(hash(i),hash(i+vec3(1,0,0)),u.x),mix(hash(i+vec3(0,1,0)),hash(i+vec3(1,1,0)),u.x),u.y),",
      "mix(mix(hash(i+vec3(0,0,1)),hash(i+vec3(1,0,1)),u.x),mix(hash(i+vec3(0,1,1)),hash(i+1.),u.x),u.y),u.z);}",
      "void main(){vec3 n=normalize(point);float curl=noise3(n*4.);",
      "float grain=noise3(n*20.+curl*3.)*.55+noise3(n*70.)*.3+noise3(n*130.)*.15;",
      "float limb=max(0.,dot(normalize(outward),normalize(viewDir)));",
      "vec3 c=mix(amber,gold,smoothstep(.20,.64,grain)); c=mix(c,light,smoothstep(.48,.86,grain)*.9);",
      "gl_FragColor=vec4(c*(.74+.38*pow(limb,.44)),1.);",
      "#include <colorspace_fragment>",
      "}",
    ].join("\n"),
  });
}

export function solarSystem(p: Paint, earthMap: T.Texture) {
  const w = world(),
    sunX = -0.58;
  const sun = mesh(w.root, new T.SphereGeometry(0.77, 64, 40), solarSurface(p), sunX);
  halo(sun, p, p.color("warning", 400), 4.9, 0.88);
  halo(sun, p, p.color("warning", 300), 2.45, 0.63);
  pickable(w, sun, "Sol");
  label(w.root, p, "Sol", sunX, -1.12, 0);
  const solarMark = ring(w.root, 0.93, p.color("warning", 200), 0.82);
  solarMark.position.x = sunX;
  highlight(w, "Sol", solarMark);
  const plane = new T.Group();
  w.root.add(plane);
  plane.position.x = sunX;
  plane.rotation.set(-1.03, 0, 0.16, "ZXY");
  const orbits = [0.98, 1.46, 2.35, 2.96, 3.82, 4.75, 5.51, 6.05];
  const angles = [-1.25, 2.4, 0.64, -2.16, 2.72, -0.32, 1.3, 3.15];
  const sizes = [0.085, 0.15, 0.38, 0.16, 0.52, 0.39, 0.22, 0.21];
  const colors = ["grey", "warning", "info", "danger", "warning", "warning", "info", "primary"];
  orbits.forEach((r, i) => {
    ring(plane, r, p.color("info", 300), i === 2 ? 0.49 : 0.21);
    const pos = new T.Vector3(r * Math.cos(angles[i]), r * Math.sin(angles[i]), 0).applyEuler(plane.rotation);
    pos.x += sunX;
    let body: T.Object3D;
    if (i === 2) {
      body = globe(p, earthMap, sizes[i]);
      w.root.add(body);
    } else {
      body = mesh(
        w.root,
        new T.SphereGeometry(sizes[i], 40, 24),
        new T.MeshStandardMaterial({ map: planetTexture(p, colors[i], i >= 4, 54 + i), roughness: 0.86 })
      );
    }
    body.position.copy(pos);
    if (i === 2) {
      w.portal.copy(pos);
      pickable(w, body, "Terra");
      label(w.root, p, "Terra", pos.x, pos.y - 0.7, pos.z);
      const mark = ring(w.root, 0.5, p.color("info", 100), 0.84);
      mark.position.copy(pos);
      highlight(w, "Terra", mark);
      const moon = mesh(
        w.root,
        new T.SphereGeometry(0.15, 28, 20),
        new T.MeshStandardMaterial({ map: planetTexture(p, "grey", false, 112) }),
        pos.x + 0.74,
        pos.y + 0.48,
        pos.z + 0.12
      );
      pickable(w, moon, "Luna");
      label(w.root, p, "Luna", moon.position.x + 0.44, moon.position.y + 0.29, moon.position.z, 0.43);
      const lunarMark = ring(w.root, 0.25, p.color("grey", 100), 0.9);
      lunarMark.position.copy(moon.position);
      highlight(w, "Luna", lunarMark);
    }
    if (i === 5) {
      for (const [inner, outer, tone] of [
        [0.51, 0.64, 300],
        [0.66, 0.78, 500],
        [0.79, 0.83, 200],
      ]) {
        const ringPart = mesh(
          body,
          new T.RingGeometry(inner, outer, 72),
          new T.MeshBasicMaterial({
            color: p.color("warning", tone),
            side: T.DoubleSide,
            transparent: true,
            opacity: 0.76,
          })
        );
        ringPart.rotation.set(-1.18, 0, 0.26, "ZXY");
      }
    }
  });
  const cluster = new T.Group();
  cluster.position.set(-3.85, 2.05, -0.4);
  w.root.add(cluster);
  for (const [x, y, s] of [
    [-0.46, 0.14, 0.045],
    [0, -0.12, 0.078],
    [0.42, 0.24, 0.056],
  ])
    star(cluster, p, p.color("info", 200), s, x, y, 0);
  pickable(w, cluster, "Astra");
  label(w.root, p, "Astra", -3.85, 1.48, -0.35);
  const astralMark = ring(w.root, 0.64, p.color("info", 200), 0.72);
  astralMark.position.copy(cluster.position);
  highlight(w, "Astra", astralMark);
  return w;
}

export function earthApproach(p: Paint, earthMap: T.Texture) {
  const w = world(),
    radius = 2.94,
    earth = globe(p, earthMap, radius);
  w.root.add(earth);
  const lon = radians(138),
    lat = radians(36);
  const pin = new T.Vector3(Math.cos(lat) * Math.cos(lon), Math.sin(lat), -Math.cos(lat) * Math.sin(lon))
    .multiplyScalar(radius * 1.02)
    .applyEuler(earth.rotation);
  w.portal.copy(pin);
  const target = ring(w.root, 0.12, p.color("warning", 100), 1);
  target.position.copy(pin);
  mesh(
    w.root,
    new T.SphereGeometry(0.034, 12, 8),
    new T.MeshBasicMaterial({ color: p.color("warning", 100) }),
    pin.x,
    pin.y,
    pin.z
  );
  label(w.root, p, "일본", pin.x + 0.4, pin.y + 0.28, pin.z, 0.35);
  return w;
}
