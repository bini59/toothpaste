import * as T from "three";
import { bookFor, books, type PrintName } from "./catalog";
import { hardback, coverHeight, coverWidth, leafWidth, leafHeight } from "./binding";
import { mesh, pickable, seeded, texture, world, type Paint } from "./materials";

const pageW = 720,
  pageH = 1024;
const macroW = 150,
  macroH = 100,
  readingScale = 1.92;
export const inkWindow = {
  width: (readingScale * leafWidth * macroW) / pageW,
  height: (readingScale * leafHeight * macroH) / pageH,
};
const smooth = T.MathUtils.smoothstep,
  mix = T.MathUtils.lerp;

/** Both magnifications are impressions of the same typeset page. Locate a stroke inside the chosen character. */
function printedPage(p: Paint, key: string) {
  const book = bookFor(key),
    lines: readonly string[] = book?.lines ?? ["작은 책 한 권을", "조용히 펼쳐 읽는다."];
  const glyph = book?.glyph ?? "책",
    text = lines[0],
    title = book?.title ?? "서가의 기록";
  const ink = p.tone("grey", 900, "warning", 900, 0.1),
    paper = p.tone("warning", 50, "grey", 200, 0.23);
  const font = '48px LabMincho, Georgia, "Noto Serif CJK KR", serif';
  const left = 84,
    baseline = 418;
  function inkPage(c: CanvasRenderingContext2D) {
    c.fillStyle = paper;
    c.fillRect(-pageW, -pageH, pageW * 3, pageH * 3);
    const shade = c.createLinearGradient(0, 0, 84, 0);
    shade.addColorStop(0, p.color("warning", 900) + "42");
    shade.addColorStop(0.34, p.color("grey", 700) + "0f");
    shade.addColorStop(1, p.color("warning", 900) + "00");
    c.fillStyle = shade;
    c.fillRect(0, 0, 84, pageH);
    c.fillStyle = ink;
    c.textAlign = "left";
    c.font = "23px LabMincho, Georgia, serif";
    c.fillText(book?.genre ?? "단행본", left, 112);
    c.font = '54px LabMincho, Georgia, "Noto Serif CJK KR", serif';
    c.fillText(title, left, 246, 560);
    if (key.startsWith("model/")) {
      c.font = "23px monospace";
      c.fillText(key.slice(6), left, 660, 545);
    }
    c.fillRect(left, 291, 48, 2);
    c.font = font;
    c.fillText(text, left, baseline);
    const start = left + c.measureText(text.slice(0, text.indexOf(glyph))).width;
    c.save();
    c.globalAlpha = 0.42;
    c.fillRect(start + 2, baseline + 7, c.measureText(glyph).width - 4, 1.2);
    c.restore();
    c.font = '33px LabMincho, Georgia, "Noto Serif CJK KR", serif';
    lines.slice(1).forEach((line, i) => c.fillText(line, left, baseline + 84 * (i + 1), pageW - left - 42));
    if (key === "Opus") {
      c.strokeStyle = ink;
      c.lineWidth = 2;
      for (let i = 0; i < 5; i++) {
        const y = 747 + i * 11;
        c.beginPath();
        c.moveTo(left, y);
        c.lineTo(589, y);
        c.stroke();
      }
      [0, 2, 3, 1, 4, 3, 2, 0].forEach((pitch, i) => {
        const x = left + 35 + i * 59,
          y = 785 - pitch * 5.5;
        c.beginPath();
        c.ellipse(x, y, 8.2, 5.3, -0.35, 0, Math.PI * 2);
        c.fill();
        c.fillRect(x + 6, y - 34, 2.5, 34);
        if (i === 3 || i === 7) c.fillRect(x + 23, 747, 2, 44);
      });
    }
    c.font = "italic 22px LabMincho, Georgia, serif";
    c.fillText("2", pageW - 86, pageH - 64);
  }
  let gx = 0,
    gy = 0;
  const page = texture(pageW, pageH, (c) => {
    inkPage(c);
    c.font = font;
    const before = text.slice(0, text.indexOf(glyph)),
      charLeft = Math.floor(left + c.measureText(before).width),
      charW = Math.ceil(c.measureText(glyph).width);
    const top = baseline - 53;
    const pixels = c.getImageData(charLeft, top, charW, 64).data;
    let best = Infinity;
    for (let y = 3; y < 60; y++)
      for (let x = 2; x < charW - 2; x++) {
        if (pixels[(y * charW + x) * 4] > 95) continue;
        let radius = 0;
        for (let d = 1; d < 6 && x >= d && x + d < charW && y >= d && y + d < 64; d++) {
          if (
            [
              [d, 0],
              [-d, 0],
              [0, d],
              [0, -d],
              [d, d],
              [-d, d],
              [d, -d],
              [-d, -d],
            ].some(([dx, dy]) => pixels[((y + dy) * charW + x + dx) * 4] > 105)
          )
            break;
          radius = d;
        }
        const score = (x - charW / 2) ** 2 + (y - 32) ** 2 - radius * 1200;
        if (score < best) {
          best = score;
          gx = charLeft + x;
          gy = top + y;
        }
      }
    if (!Number.isFinite(best)) {
      gx = charLeft + charW / 2;
      gy = baseline - 18;
    }
  });
  // Put the magnified letter on the same uncoated paper and keep its neighbours at their exact offsets.
  const macro = texture(1440, 960, (c) => {
    c.scale(1440 / macroW, 960 / macroH);
    c.translate(macroW / 2 - gx, macroH / 2 - gy);
    inkPage(c);
    c.setTransform(1, 0, 0, 1, 0, 0);
    const grain = seeded(239);
    for (let i = 0; i < 20000; i++) {
      c.fillStyle = i % 3 === 0 ? p.color("grey", 900) + "0a" : p.color("warning", 50) + "1a";
      c.fillRect(grain() * 1440, grain() * 960, grain() * 2.4 + 0.3, grain() * 1.7 + 0.3);
    }
  });
  return { page, macro, uv: new T.Vector2(gx / pageW, 1 - gy / pageH) };
}

type BoundBook = ReturnType<typeof hardback>;
type Edition = {
  book: BoundBook;
  page: ReturnType<typeof printedPage>;
  slot: number;
  height: number;
  thickness: number;
};
const slotX = [-4.35, -1.53, 1.42, 4.34];
const heightScale = [1.12, 1.24, 1.07, 0.94];
const widths = [0.81, 0.88, 0.75, 0.6];

function shelving(walnut: T.Texture, bindings: T.Texture) {
  const rig = new T.Group();
  const timber = new T.MeshStandardMaterial({ map: walnut, bumpMap: walnut, bumpScale: 0.022, roughness: 0.65 });
  const wood = (width: number, height: number, depth: number, x: number, y: number, z: number) => {
    const part = mesh(rig, new T.BoxGeometry(width, height, depth), timber, x, y, z);
    part.receiveShadow = true;
    part.castShadow = true;
    return part;
  };
  // A continuous reading ledge projects from the case; the camera can follow a volume down onto it.
  wood(31, 0.22, 14, 0, -1.99, 3.9);
  wood(28, 12, 0.14, 0, 1.18, -2.65);
  wood(21, 0.25, 2.8, 0, 3.2, -1.14);
  for (const x of [-7.2, 7.2]) wood(0.23, 10, 2.7, x, 0.8, -1.17);
  const random = seeded(243);
  const material = new T.MeshStandardMaterial({ map: bindings, bumpMap: bindings, bumpScale: 0.012, roughness: 0.94 });
  const batches: T.Matrix4[][] = Array.from({ length: 6 }, () => []);
  const placement = new T.Object3D();
  function fill(left: number, right: number, base: number) {
    let x = left,
      last = -1;
    while (x < right - 0.14) {
      const width = Math.min(right - x, 0.21 + random() * 0.35);
      const height = (2.63 + random() * 0.89) * (base > 0 ? 0.84 : 1),
        front = -0.4 + random() * 0.23;
      const design = (last + 1 + Math.floor(random() * 5)) % 6;
      last = design;
      placement.position.set(x + width / 2, base + height / 2, front - 0.74);
      placement.scale.set(width * 0.98, height, 1.5);
      placement.rotation.z = (random() - 0.5) * 0.037;
      placement.updateMatrix();
      batches[design].push(placement.matrix.clone());
      x += width + 0.018;
    }
  }
  let left = -8.5;
  slotX.forEach((x, slot) => {
    const half = (widths[slot] * heightScale[slot]) / 2;
    fill(left, x - half - 0.22, -1.87);
    left = x + half + 0.26;
  });
  fill(left, 8.7, -1.87);
  fill(-8.5, 8.7, 3.34);
  // Each atlas column belongs to a physical binding. Batch ordinary volumes by their worn leather or cloth.
  batches.forEach((matrices, design) => {
    const g = new T.BoxGeometry(1, 1, 1, 10, 1, 1),
      uv = g.getAttribute("uv"),
      pos = g.getAttribute("position"),
      n = g.getAttribute("normal");
    for (let i = 0; i < pos.count; i++) {
      const u = uv.getX(i),
        v = uv.getY(i),
        x = pos.getX(i);
      const px = n.getZ(i) > 0.5 ? 0.016 + 0.968 * u : x > 0 ? 0.949 + 0.035 * u : 0.016 + 0.035 * u;
      const py = Math.abs(n.getY(i)) > 0.6 ? (n.getY(i) > 0 ? 0.97 : 0.014) + v * 0.016 : 0.006 + v * 0.988;
      uv.setXY(i, (design + px) / 6, py);
      if (pos.getZ(i) > 0) pos.setZ(i, 0.5 + 0.034 * (1 - 4 * x * x));
    }
    g.computeVertexNormals();
    const row = new T.InstancedMesh(g, material, matrices.length);
    matrices.forEach((m, i) => row.setMatrixAt(i, m));
    row.castShadow = true;
    row.receiveShadow = true;
    rig.add(row);
  });
  return rig;
}

export function bookcase(
  p: Paint,
  walnut: T.Texture,
  weave: T.Texture,
  bindings: T.Texture,
  art: Record<PrintName, T.Texture>
) {
  const w = world(),
    rig = shelving(walnut, bindings);
  w.root.add(rig);
  const editions = new Map<string, Edition>();
  const tones = [
    p.tone("success", 900, "grey", 700, 0.58),
    p.tone("danger", 900, "warning", 800, 0.44),
    p.tone("primary", 900, "grey", 700, 0.51),
    p.tone("warning", 100, "grey", 200, 0.72),
  ];
  function add(key: string, name: string, slot: number, engraving?: T.Texture) {
    const page = printedPage(p, key),
      book = hardback(p, key, name, tones[slot], widths[slot], weave, bindings, engraving, page.page, page.uv);
    const edition = { book, page, slot, height: heightScale[slot], thickness: widths[slot] };
    editions.set(key, edition);
    rig.add(book.root);
    pickable(w, book.root, key);
  }
  (Object.keys(books) as PrintName[]).forEach((k, i) => add(k, k, i, art[k]));
  let extra: string | undefined;
  function ensure(key: string, name: string) {
    if (editions.has(key)) return;
    if (extra) {
      const old = editions.get(extra)!;
      rig.remove(old.book.root);
      w.hits.splice(w.hits.indexOf(old.book.root), 1);
      editions.delete(extra);
      const geometry = new Set<T.BufferGeometry>(),
        materials = new Set<T.Material>(),
        maps = new Set<T.Texture>();
      old.book.root.traverse((o) => {
        if (o instanceof T.Mesh) {
          geometry.add(o.geometry);
          const m = o.material as T.MeshStandardMaterial;
          materials.add(m);
          if (m.map && m.map !== weave) maps.add(m.map);
        }
      });
      geometry.forEach((g) => g.dispose());
      materials.forEach((m) => m.dispose());
      maps.forEach((t) => t.dispose());
      old.page.macro.dispose();
    }
    add(key, name, 0);
    extra = key;
  }
  const pivot = new T.Vector3();
  function pose(key: string, progress: number, approach = 0) {
    const pull = smooth(progress, 0, 0.39),
      turn = smooth(progress, 0.19, 0.63),
      lower = smooth(progress, 0.46, 0.91);
    const opening = smooth(progress, 0.57, 1.0),
      align = smooth(approach, 0.12, 0.83);
    const readingZ = 5.04;
    const edition = editions.get(key)!;
    const landingY = -1.87 + (edition.thickness / 2 + 0.034) * edition.height;
    // Track the solid book from its back to its gutter. The book keeps its size against its neighbours.
    const track = smooth(progress, 0.24, 0.86);
    const magnification = mix(1, readingScale / edition.height, track);
    rig.scale.setScalar(magnification);
    rig.rotation.set(lower * mix(1.13, Math.PI / 2, align), 0.056 * (1 - lower), 0, "XYZ");
    const x = turn * (1 - opening) * coverWidth * edition.height * 0.36 - 0.1 * lower;
    const z = 0.14 + 1.86 * pull + (readingZ - 2) * turn;
    pivot.set(x, (landingY + 0.46 / magnification) * lower, z * track).applyEuler(rig.rotation);
    rig.position.copy(pivot).multiplyScalar(-magnification);
    rig.position.z += mix(0, 1.6, track);
    for (const [k, e] of editions) {
      const selected = k === key,
        book = e.book,
        t = selected ? turn : 0,
        down = selected ? lower : 0;
      book.root.visible = !(k === extra && !selected) && !(k === "Fable" && key === extra);
      const spineYaw = T.MathUtils.degToRad([81, 84, 79, 80][e.slot]);
      book.root.position.set(
        mix(slotX[e.slot], -0.1, t),
        mix(-1.87 + (coverHeight * e.height) / 2, landingY, down) + 0.27 * (selected ? Math.sin(Math.PI * pull) : 0),
        0.14 + (selected ? 1.86 * pull + (readingZ - 2) * turn : 0)
      );
      book.root.scale.setScalar(e.height);
      book.root.rotation.set(
        (-down * Math.PI) / 2,
        mix(spineYaw, -0.06 * (1 - align), t),
        mix([0.016, -0.019, 0.026, -0.008][e.slot], -0.078 * (1 - align), t),
        "XYZ"
      );
      book.open(selected ? opening : 0);
    }
    const selected = editions.get(key)!;
    selected.book.right.updateWorldMatrix(true, false);
    const worldPoint = selected.book.right.localToWorld(selected.book.target.clone());
    w.portal.copy(w.root.worldToLocal(worldPoint));
    return selected.page.macro;
  }
  return {
    world: w,
    ensure,
    pose,
    lightDirection(out: T.Vector3, yaw: number) {
      return out.set(-4.8, 6.2, 8.7).applyEuler(rig.rotation).applyAxisAngle(rig.up, yaw);
    },
    refresh() {
      for (const e of editions.values()) e.book.refresh();
    },
    dispose() {
      for (const e of editions.values()) e.page.macro.dispose();
    },
  };
}
