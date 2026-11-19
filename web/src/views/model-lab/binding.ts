import * as T from "three";
import { bookFor } from "./catalog";
import { mesh, texture, type Paint } from "./materials";

export const coverWidth = 2.12,
  coverHeight = 3.22;
export const leafWidth = coverWidth - 0.09,
  leafHeight = coverHeight - 0.1;
const serif = "LabMincho, Georgia, serif";

function linen(ctx: CanvasRenderingContext2D, tone: string, weave: T.Texture) {
  const { width, height } = ctx.canvas;
  ctx.fillStyle = tone;
  ctx.fillRect(0, 0, width, height);
  if (!weave.image) return;
  ctx.save();
  ctx.globalCompositeOperation = "multiply";
  ctx.globalAlpha = 0.72;
  const tile = 244;
  for (let y = 0; y < height; y += tile)
    for (let x = 0; x < width; x += tile) ctx.drawImage(weave.image as HTMLImageElement, x, y, tile, tile);
  ctx.restore();
}

function inkPlate(
  c: CanvasRenderingContext2D,
  art: T.Texture | undefined,
  left: number,
  top: number,
  width: number,
  height: number,
  color?: string
) {
  if (!art?.image) return;
  const image = art.image as HTMLImageElement;
  const scale = Math.min(width / image.width, height / image.height);
  const w = image.width * scale,
    h = image.height * scale;
  if (!color) {
    c.drawImage(image, left + (width - w) / 2, top + (height - h) / 2, w, h);
    return;
  }
  const stamp = document.createElement("canvas");
  stamp.width = Math.ceil(w);
  stamp.height = Math.ceil(h);
  const ink = stamp.getContext("2d")!;
  ink.drawImage(image, 0, 0, w, h);
  ink.globalCompositeOperation = "source-in";
  ink.fillStyle = color;
  ink.fillRect(0, 0, stamp.width, stamp.height);
  c.drawImage(stamp, left + (width - w) / 2, top + (height - h) / 2);
}

function printedSurface(width: number, height: number, draw: (c: CanvasRenderingContext2D) => void) {
  const map = texture(width, height, draw);
  map.anisotropy = 4;
  return {
    map,
    refresh() {
      const c = map.image.getContext("2d")!;
      c.clearRect(0, 0, width, height);
      c.save();
      draw(c);
      c.restore();
      map.needsUpdate = true;
    },
  };
}

function spineGeometry(height: number, thickness: number) {
  const g = new T.PlaneGeometry(1, height, 24, 1),
    v = g.getAttribute("position"),
    uv = g.getAttribute("uv");
  for (let i = 0; i < v.count; i++) {
    const a = (uv.getX(i) - 0.5) * Math.PI;
    v.setXYZ(i, -0.12 * Math.cos(a), v.getY(i), (Math.sin(a) * thickness) / 2);
  }
  g.computeVertexNormals();
  return g;
}

export function leafRelief(u: number, v: number) {
  return (
    0.185 * Math.sin(Math.PI * u) ** 0.85 * (1.1 - u * 0.5) +
    0.032 * u ** 3 * Math.cos(v * Math.PI) -
    0.035 * Math.exp(-u * 14)
  );
}
function rightLeaf() {
  const g = new T.PlaneGeometry(leafWidth, leafHeight, 36, 8),
    v = g.getAttribute("position"),
    uv = g.getAttribute("uv");
  for (let i = 0; i < v.count; i++) v.setXYZ(i, uv.getX(i) * leafWidth, v.getY(i), leafRelief(uv.getX(i), uv.getY(i)));
  g.computeVertexNormals();
  return g;
}

function turningLeaf() {
  const segments = 32,
    g = new T.PlaneGeometry(leafWidth, leafHeight, segments, 6),
    v = g.getAttribute("position"),
    uv = g.getAttribute("uv");
  const x = new Float32Array(segments + 1),
    z = new Float32Array(segments + 1),
    step = leafWidth / segments;
  let previous = -1;
  const set = (progress: number) => {
    if (progress === previous) return;
    previous = progress;
    const bell = Math.sin(progress * Math.PI),
      swing = Math.PI * progress;
    for (let j = 1; j <= segments; j++) {
      const u = (j - 0.5) / segments,
        angle = swing + bell * (0.72 - 1.32 * u);
      x[j] = x[j - 1] + Math.cos(angle) * step;
      z[j] = z[j - 1] + Math.sin(angle) * step;
    }
    for (let j = 0; j < v.count; j++) {
      const u = uv.getX(j),
        t = uv.getY(j),
        col = Math.round(u * segments);
      const curl = leafRelief(u, t) * (0.9 + 0.15 * Math.cos(swing));
      v.setXYZ(
        j,
        x[col],
        (t - 0.5) * leafHeight + 0.055 * bell * u * (1 - t),
        z[col] + curl + bell * 0.11 * (t - 0.5) * u
      );
    }
    v.needsUpdate = true;
    g.computeVertexNormals();
    g.computeBoundingSphere();
  };
  set(0);
  return { geometry: g, set };
}

export function hardback(
  p: Paint,
  key: string,
  name: string,
  tone: string,
  thickness: number,
  weave: T.Texture,
  bindings: T.Texture,
  art: T.Texture | undefined,
  rightPage: T.Texture,
  glyph: T.Vector2
) {
  const root = new T.Group(),
    hinge = new T.Group();
  const genre = bookFor(key)?.genre ?? "단행본";
  const pale = key === "Haiku";
  const ink = pale ? p.color("grey", 800) : p.tone("warning", 200, "grey", 100, 0.44);
  const cover = printedSurface(630, 960, (c) => {
    linen(c, tone, weave);
    c.fillStyle = ink;
    c.textAlign = "left";
    c.font = "80px " + serif;
    c.fillText(name, 65, 170, 515);
    c.font = "26px " + serif;
    c.fillText(genre, 69, 229, 495);
    inkPlate(c, art, 70, 280, 500, 520, ink);
    c.fillStyle = ink;
    if (key.startsWith("model/")) {
      c.font = "22px monospace";
      c.fillText(key.slice(6), 70, 800, 495);
    }
    const crease = c.createLinearGradient(0, 0, 52, 0);
    crease.addColorStop(0, p.color("grey", 900) + "65");
    crease.addColorStop(0.43, p.color("grey", 50) + "24");
    crease.addColorStop(1, p.color("grey", 900) + "00");
    c.fillStyle = crease;
    c.fillRect(0, 0, 52, 960);
  });
  const spine = printedSurface(232, 920, (c) => {
    linen(c, tone, weave);
    const source = bindings.image as HTMLImageElement | undefined;
    if (source) {
      const which = key === "Opus" ? 1 : key === "Sonnet" ? 2 : key === "Haiku" ? 4 : 0;
      const sw = source.width / 6;
      c.save();
      c.globalCompositeOperation = "soft-light";
      c.globalAlpha = 0.68;
      c.drawImage(source, sw * which + 3, 2, sw - 6, source.height - 4, 0, 0, 232, 920);
      c.restore();
    }
    c.fillStyle = ink;
    c.textAlign = "center";
    c.save();
    c.translate(114, 385);
    c.rotate(-Math.PI / 2);
    c.font = "120px " + serif;
    c.fillText(name, 0, 36, 510);
    c.restore();
    c.fillRect(53, 688, 126, 2);
    c.font = "30px " + serif;
    c.fillText(genre, 116, 749, 170);
    if (key.startsWith("model/")) {
      c.font = "20px monospace";
      c.fillText(key.slice(6), 116, 810, 190);
    }
    for (const y of [49, 57, 860]) c.fillRect(24, y, 184, 2);
  });
  const plate = printedSurface(660, 960, (c) => {
    c.save();
    c.translate(660, 0);
    c.scale(-1, 1);
    const paper = p.tone("warning", 50, "grey", 200, 0.25);
    c.fillStyle = paper;
    c.fillRect(0, 0, 660, 960);
    inkPlate(c, art, 48, 88, 560, 700);
    c.fillStyle = p.color("grey", 700);
    c.textAlign = "center";
    c.font = "italic 30px " + serif;
    c.fillText(name, 332, 833, 535);
    c.restore();
  });
  const cloth = new T.MeshStandardMaterial({ color: tone, bumpMap: weave, bumpScale: 0.014, roughness: 0.85 });
  const print = (map: T.Texture, side: T.Side = T.FrontSide) =>
    new T.MeshStandardMaterial({ map, side, roughness: 0.92 });
  const edge = texture(128, 224, (c) => {
    c.fillStyle = p.tone("warning", 100, "grey", 200, 0.5);
    c.fillRect(0, 0, 128, 224);
    for (let i = 0; i < 224; i += 2) {
      c.fillStyle = p.color("warning", 900) + (i % 8 === 0 ? "55" : "1d");
      c.fillRect(0, i, 128, 0.85);
    }
  });
  const edgeMat = new T.MeshStandardMaterial({ map: edge, roughness: 0.96 });
  const half = thickness / 2;
  mesh(root, new T.BoxGeometry(coverWidth, coverHeight, 0.062), cloth, coverWidth / 2, 0, -half);
  mesh(root, new T.BoxGeometry(leafWidth, leafHeight, thickness - 0.12), edgeMat, coverWidth / 2, 0, -0.011);
  const spinePrint = print(spine.map, T.DoubleSide);
  spinePrint.bumpMap = weave;
  spinePrint.bumpScale = 0.012;
  mesh(root, spineGeometry(coverHeight, thickness + 0.06), spinePrint);
  const right = mesh(root, rightLeaf(), print(rightPage), 0.053, 0, half - 0.034);
  const target = new T.Vector3(glyph.x * leafWidth, (glyph.y - 0.5) * leafHeight, leafRelief(glyph.x, glyph.y));
  const hit = mesh(right, new T.PlaneGeometry(0.38, 0.3), new T.MeshBasicMaterial({ visible: false }));
  hit.position.copy(target);
  hit.position.z += 0.025;
  hit.userData.choice = "@ink";
  root.add(hinge);
  hinge.position.set(0.014, 0, half + 0.047);
  mesh(hinge, new T.BoxGeometry(coverWidth, coverHeight, 0.06), cloth, coverWidth / 2);
  const jacketPrint = print(cover.map);
  jacketPrint.bumpMap = weave;
  jacketPrint.bumpScale = 0.012;
  mesh(hinge, new T.PlaneGeometry(coverWidth, coverHeight), jacketPrint, coverWidth / 2, 0, 0.035);
  const lining = new T.MeshStandardMaterial({
    color: p.tone("warning", 100, "grey", 300, 0.4),
    roughness: 1,
    side: T.BackSide,
  });
  mesh(hinge, new T.PlaneGeometry(coverWidth - 0.06, coverHeight - 0.07), lining, coverWidth / 2, 0, -0.032);
  const turning = turningLeaf(),
    leaf = new T.Group();
  root.add(leaf);
  leaf.position.set(0.045, 0, half + 0.017);
  const recto = new T.MeshStandardMaterial({ color: p.tone("warning", 50, "grey", 200, 0.26), roughness: 1 });
  mesh(leaf, turning.geometry, recto);
  mesh(leaf, turning.geometry, print(plate.map, T.BackSide));
  root.traverse((o) => {
    if (o instanceof T.Mesh && o !== hit) {
      o.castShadow = true;
      o.receiveShadow = true;
    }
  });
  // The front board clears the text block before the flyleaf follows under its own bend.
  function open(progress: number) {
    const board = T.MathUtils.smoothstep(progress, 0.05, 0.94),
      paper = T.MathUtils.smoothstep(progress, 0.27, 1);
    hinge.rotation.y = -board * (Math.PI - 0.028);
    turning.set(paper);
    leaf.visible = progress > 0.01;
    right.visible = progress > 0.02;
    // Once fully open, the illustrated verso sits just above the board at the left of the gutter.
    leaf.position.z = half + 0.017 + 0.1 * paper;
    hit.visible = progress > 0.98;
  }
  open(0);
  return {
    root,
    right,
    target,
    open,
    refresh() {
      cover.refresh();
      spine.refresh();
      plate.refresh();
    },
  };
}
