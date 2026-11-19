import * as T from "three";

export function mesh(parent: T.Object3D, geo: T.BufferGeometry, mat: T.Material, x = 0, y = 0, z = 0) {
  const part = new T.Mesh(geo, mat);
  part.position.set(x, y, z);
  part.castShadow = part.receiveShadow = true;
  parent.add(part);
  return part;
}

export function circle(radius: number, tube: number, arc = Math.PI * 2) {
  return new T.TorusGeometry(radius, tube, 8, 96, arc);
}

export function texture(width: number, height: number, draw: (ctx: CanvasRenderingContext2D) => void) {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  draw(canvas.getContext("2d")!);
  const map = new T.CanvasTexture(canvas);
  map.colorSpace = T.SRGBColorSpace;
  return map;
}

/** The chamber stays dark in both dashboard themes. Resolve swatches by physical lightness. */
export function pigments() {
  const css = getComputedStyle(document.documentElement);
  const swatch = (name: string) => css.getPropertyValue("--color-" + name).trim();
  const reversed = new T.Color(swatch("grey-900")).getHSL({ h: 0, s: 0, l: 0 }).l >
    new T.Color(swatch("grey-50")).getHSL({ h: 0, s: 0, l: 0 }).l;
  const steps = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900];
  const color = (family: string, step: number) => swatch(family + "-" + (reversed ? steps[9 - steps.indexOf(step)] : step));
  const metal = (c: string, metalness: number, roughness: number) => new T.MeshStandardMaterial({ color: c, metalness, roughness });
  const glow = (c: string, strength = 1) => new T.MeshStandardMaterial({ color: c, emissive: c, emissiveIntensity: strength, roughness: 0.34 });
  const ink = color("warning", 900);
  const score = texture(384, 384, (ctx) => {
    ctx.fillStyle = color("warning", 50);
    ctx.fillRect(0, 0, 384, 384);
    ctx.fillStyle = ctx.strokeStyle = ink;
    for (let row = 0; row < 4; row++) {
      const y = 55 + row * 76;
      for (let line = 0; line < 5; line++) ctx.fillRect(24, y + 6 * line, 336, 1.5);
      for (let i = 0; i < 11; i++) {
        const x = 37 + i * 30, note = y + 3 * ((i * 5 + row * 3) % 10);
        ctx.beginPath();
        ctx.ellipse(x, note, 5.6, 3.8, -0.3, 0, 2 * Math.PI);
        ctx.fill();
        ctx.fillRect(x + 4, note - 21, 1.7, 21);
      }
    }
  });
  const halo = texture(128, 128, (ctx) => {
    const gradient = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
    const light = color("grey", 50);
    gradient.addColorStop(0, light);
    gradient.addColorStop(0.2, light + "80");
    gradient.addColorStop(0.55, light + "20");
    gradient.addColorStop(1, light + "00");
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, 128, 128);
  });
  const fibers = texture(192, 192, (ctx) => {
    ctx.fillStyle = color("grey", 50);
    ctx.fillRect(0, 0, 192, 192);
    ctx.fillStyle = color("warning", 700);
    ctx.globalAlpha = 0.12;
    let seed = 17;
    const jitter = () => (seed = (16807 * seed) % 2147483647) / 2147483647;
    for (let i = 0; i < 3200; i++) ctx.fillRect(jitter() * 192, jitter() * 192, 0.3 + jitter() * 2.9, 0.6);
  });
  const paper = (c: string) => new T.MeshStandardMaterial({ color: c, map: fibers, roughness: 0.89, side: T.DoubleSide });
  const music = new T.MeshStandardMaterial({ map: score, roughness: 0.9, side: T.DoubleSide });
  const glass = (c: string) => new T.MeshPhysicalMaterial({
    color: c, roughness: 0.065, metalness: 0.08, transmission: 0.76, thickness: 0.45,
    ior: 1.46, clearcoat: 1, clearcoatRoughness: 0.1, envMapIntensity: 1.45,
  });
  return {
    color, metal, glow, halo, glass, music,
    deck: metal(color("grey", 700), 0.72, 0.38), basalt: metal(color("grey", 900), 0.32, 0.54),
    trim: metal(color("grey", 400), 0.84, 0.27), brass: metal(color("warning", 400), 0.76, 0.31),
    silver: metal(color("info", 200), 0.85, 0.25), patina: metal(color("info", 600), 0.76, 0.33),
    paper: paper(color("warning", 100)), shade: paper(color("warning", 200)), cover: paper(color("warning", 800)),
    sage: paper(color("success", 300)), clay: paper(color("danger", 300)),
  };
}
export type Paint = ReturnType<typeof pigments>;

/** Tall softboxes give bevels and clear glass readable reflections as the camera travels. */
export function studioEnvironment(p: Paint) {
  const map = texture(512, 256, (ctx) => {
    ctx.fillStyle = p.color("grey", 800);
    ctx.fillRect(0, 0, 512, 256);
    for (const [x, y, w, h, c] of [
      [74, 72, 54, 115, p.color("warning", 50)], [261, 40, 92, 66, p.color("grey", 100)],
      [405, 76, 22, 113, p.color("info", 200)],
    ] as const) {
      ctx.fillStyle = ctx.shadowColor = c;
      ctx.shadowBlur = 16;
      ctx.fillRect(x, y, w, h);
    }
  });
  map.mapping = T.EquirectangularReflectionMapping;
  return map;
}

export function halo(parent: T.Object3D, p: Paint, color: string, size: number, opacity: number) {
  const sprite = new T.Sprite(new T.SpriteMaterial({ map: p.halo, color, opacity, blending: T.AdditiveBlending, depthWrite: false }));
  sprite.scale.set(size, size, 1);
  parent.add(sprite);
  return sprite;
}
