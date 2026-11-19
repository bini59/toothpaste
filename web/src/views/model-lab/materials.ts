import * as T from "three";

export function mesh(parent: T.Object3D, geometry: T.BufferGeometry, material: T.Material, x = 0, y = 0, z = 0) {
  const part = new T.Mesh(geometry, material);
  part.position.set(x, y, z);
  parent.add(part);
  return part;
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

/** Keep the night sky dark even when a dashboard theme reverses its token scales. */
export function pigments() {
  const css = getComputedStyle(document.documentElement);
  const swatch = (name: string) => css.getPropertyValue("--color-" + name).trim();
  const lightness = (name: string) => new T.Color(swatch(name)).getHSL({ h: 0, s: 0, l: 0 }).l;
  const reversed = lightness("grey-900") > lightness("grey-50");
  const steps = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900];
  const color = (family: string, step: number) =>
    swatch(family + "-" + (reversed ? steps[9 - steps.indexOf(step)] : step));
  const haze = texture(128, 128, (ctx) => {
    const white = color("grey", 50),
      g = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
    for (const [t, alpha] of [
      [0, "ff"],
      [0.09, "df"],
      [0.25, "68"],
      [0.57, "19"],
      [1, "00"],
    ] as const)
      g.addColorStop(t, white + alpha);
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, 128, 128);
  });
  return { color, haze };
}
export type Paint = ReturnType<typeof pigments>;

export function halo(parent: T.Object3D, p: Paint, color: string, size: number, opacity: number) {
  const sprite = new T.Sprite(
    new T.SpriteMaterial({ map: p.haze, color, opacity, blending: T.AdditiveBlending, depthWrite: false })
  );
  sprite.scale.set(size, size, 1);
  parent.add(sprite);
  return sprite;
}

export function seeded(seed: number) {
  return () => ((seed = (Math.imul(seed, 1664525) + 1013904223) | 0) >>> 0) / 4294967296;
}

export function label(
  parent: T.Object3D, p: Paint, text: string, x: number, y: number, z: number, height = 0.48, width = 512
) {
  const map = texture(width, 96, (ctx) => {
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.font = "600 62px system-ui, sans-serif";
    ctx.shadowColor = p.color("grey", 900);
    ctx.shadowBlur = 12;
    ctx.fillStyle = p.color("grey", 50);
  });
  function setText(value: string) {
    const ctx = map.image.getContext("2d")!;
    ctx.clearRect(0, 0, width, 96);
    ctx.fillText(value, width / 2, 48, width - 16);
    map.needsUpdate = true;
  }
  setText(text);
  const sprite = new T.Sprite(new T.SpriteMaterial({ map, depthTest: false, depthWrite: false, transparent: true }));
  sprite.position.set(x, y, z);
  sprite.scale.set((height * width) / 96, height, 1);
  sprite.renderOrder = 6;
  parent.add(sprite);
  return setText;
}

export function ring(parent: T.Object3D, radius: number, color: string, opacity: number) {
  const points = Array.from({ length: 128 }, (_, i) => {
    const a = (i * Math.PI) / 64;
    return new T.Vector3(radius * Math.cos(a), radius * Math.sin(a), 0);
  });
  const line = new T.LineLoop(
    new T.BufferGeometry().setFromPoints(points),
    new T.LineBasicMaterial({ color, transparent: true, opacity, depthWrite: false })
  );
  parent.add(line);
  return line;
}

export type World = {
  root: T.Group;
  portal: T.Vector3;
  /** Only these objects participate in picking. Label and decorative sprites have broad transparent bounds. */
  hits: T.Object3D[];
  marks: Map<string, T.Object3D>;
};
export function world(x = 0, y = 0, z = 0): World {
  return { root: new T.Group(), portal: new T.Vector3(x, y, z), hits: [], marks: new Map() };
}
export function pickable(w: World, object: T.Object3D, key: string) {
  object.userData.choice = key;
  w.hits.push(object);
}
export function highlight(w: World, key: string, mark: T.Object3D) {
  mark.visible = false;
  w.marks.set(key, mark);
}
