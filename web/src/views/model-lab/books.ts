import * as T from "three";
import { bookFor, books, type PrintName } from "./catalog";
import { mesh, pickable, seeded, texture, world, type Paint } from "./materials";

export const volumeWidth = 2.08,
  volumeHeight = 3.1;
const paperWidth = volumeWidth - 0.12,
  paperHeight = volumeHeight - 0.1;
const pageW = 720,
  pageH = 1024;
const smooth = T.MathUtils.smoothstep,
  mix = T.MathUtils.lerp;

function paperGeometry() {
  const g = new T.PlaneGeometry(paperWidth, paperHeight, 28, 1);
  const v = g.getAttribute("position");
  for (let i = 0; i < v.count; i++) {
    const u = (v.getX(i) + paperWidth / 2) / paperWidth;
    v.setZ(i, 0.065 * Math.sin(Math.PI * u) - 0.035 * Math.exp(-u * 12));
  }
  g.computeVertexNormals();
  return g;
}

function textSize(ctx: CanvasRenderingContext2D, text: string, max: number, start: number) {
  let size = start;
  do {
    ctx.font = size + "px LabMincho, Georgia, serif";
    if (ctx.measureText(text).width <= max) break;
    size -= 2;
  } while (size > 20);
}

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
  const macroW = 150,
    macroH = 100;
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

function coverMap(p: Paint, name: string, key: string, base: string) {
  return texture(480, 720, (c) => {
    c.fillStyle = base;
    c.fillRect(0, 0, 480, 720);
    const rand = seeded(51);
    for (let j = 0; j < 720; j += 2) {
      c.fillStyle = p.color("grey", 50) + (rand() > 0.5 ? "09" : "04");
      c.fillRect(0, j, 480, 0.7);
    }
    for (let x = 0; x < 480; x += 3) {
      c.fillStyle = p.color("grey", 900) + "21";
      c.fillRect(x, 0, 0.8, 720);
    }
    const foil = p.tone("warning", 200, "grey", 200, 0.25);
    c.strokeStyle = foil;
    c.lineWidth = 1.4;
    c.strokeRect(29, 25, 424, 670);
    c.strokeRect(35, 31, 412, 658);
    c.fillStyle = foil;
    c.textAlign = "center";
    textSize(c, name, 390, 84);
    c.fillText(name, 241, 148, 390);
    c.font = "25px LabMincho, Georgia, serif";
    c.fillText(bookFor(key)?.genre ?? "단행본", 240, 620);
    c.fillRect(208, 654, 64, 2);
    if (key.startsWith("model/")) {
      c.font = "21px monospace";
      c.fillText(key.slice(6), 240, 480, 400);
    }
    const groove = c.createLinearGradient(0, 0, 32, 0);
    groove.addColorStop(0, p.color("grey", 900) + "99");
    groove.addColorStop(0.48, p.color("grey", 50) + "24");
    groove.addColorStop(1, p.color("grey", 900) + "00");
    c.fillStyle = groove;
    c.fillRect(0, 0, 32, 720);
  });
}

function bindBook(p: Paint, key: string, name: string, art: T.Texture | undefined, tone: string) {
  const root = new T.Group(),
    hinge = new T.Group(),
    w = volumeWidth,
    h = volumeHeight;
  const cloth = new T.MeshStandardMaterial({ color: tone, roughness: 0.97 });
  const paper = new T.MeshStandardMaterial({ color: p.color("warning", 100), roughness: 1 });
  const printed = printedPage(p, key);
  mesh(root, new T.BoxGeometry(w + 0.04, h + 0.03, 0.085), cloth, w / 2, 0, -0.22);
  mesh(root, new T.BoxGeometry(paperWidth, paperHeight, 0.23), paper, w / 2 + 0.01, 0, -0.061);
  const right = mesh(
    root,
    paperGeometry(),
    new T.MeshBasicMaterial({ map: printed.page }),
    w / 2 + 0.01,
    0,
    0.068
  );
  mesh(root, new T.CylinderGeometry(0.12, 0.12, h, 18, 1), cloth, 0.035, 0, -0.05);
  hinge.position.set(0.015, 0, 0.12);
  root.add(hinge);
  mesh(hinge, new T.BoxGeometry(w, h, 0.09), cloth, w / 2);
  const cover = mesh(
    hinge,
    new T.PlaneGeometry(w, h),
    new T.MeshBasicMaterial({ map: coverMap(p, name, key, tone) }),
    w / 2,
    0,
    0.054
  );
  if (art) {
    const swatch =
      key === "Sonnet"
        ? new T.CircleGeometry(0.71, 64)
        : new T.PlaneGeometry(
            key === "Haiku" ? 0.88 : 1.62,
            key === "Haiku" ? 0.88 : key === "Opus" ? 1.09 : 1.48
          );
    mesh(hinge, swatch, new T.MeshBasicMaterial({ map: art }), w / 2, key === "Haiku" ? -0.14 : -0.03, 0.06);
  }
  const face = new T.Group();
  face.rotation.y = Math.PI;
  face.position.set(w / 2, 0, -0.06);
  hinge.add(face);
  const frontispiece = texture(480, 720, (c) => {
    c.fillStyle = p.tone("warning", 50, "grey", 200, 0.23);
    c.fillRect(0, 0, 480, 720);
    c.fillStyle = p.color("grey", 800);
    c.textAlign = "center";
    textSize(c, name, 402, 58);
    c.fillText(name, 240, 115, 402);
    c.font = "24px LabMincho, Georgia, serif";
    c.fillText(bookFor(key)?.genre ?? "단행본", 240, 604);
    c.fillRect(213, 641, 54, 1.5);
  });
  mesh(face, new T.PlaneGeometry(w - 0.06, h - 0.05), new T.MeshBasicMaterial({ map: frontispiece }));
  if (art) mesh(face, new T.PlaneGeometry(1.55, 1.55), new T.MeshBasicMaterial({ map: art }), 0, -0.05, 0.01);
  const glyphHit = mesh(
    right,
    new T.PlaneGeometry(0.37, 0.3),
    new T.MeshBasicMaterial({ visible: false }),
    (printed.uv.x - 0.5) * paperWidth,
    (printed.uv.y - 0.5) * paperHeight,
    0.081
  );
  glyphHit.userData.choice = "@ink";
  return { root, hinge, cover, right, printed, glyphHit };
}

export function bookcase(p: Paint, shelving: T.Texture, art: Record<PrintName, T.Texture>) {
  const w = world();
  mesh(w.root, new T.PlaneGeometry(18.6, 12.4), new T.MeshBasicMaterial({ map: shelving }), 0, 1.22, -0.76);
  const shadowMap = texture(128, 128, (ctx) => {
    const gradient = ctx.createRadialGradient(64, 64, 8, 64, 64, 64);
    gradient.addColorStop(0, p.color("grey", 900) + "dd");
    gradient.addColorStop(0.65, p.color("grey", 900) + "7a");
    gradient.addColorStop(1, p.color("grey", 900) + "00");
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, 128, 128);
  });
  const shadow = new T.MeshBasicMaterial({ map: shadowMap, transparent: true, depthWrite: false });
  for (let i = 0; i < 4; i++) {
    const x = (i - 1.5) * 3.02;
    mesh(w.root, new T.PlaneGeometry(3.2, 3.55), shadow, x + 0.19, -0.1, -0.69);
    mesh(w.root, new T.PlaneGeometry(2.7, 0.27), shadow, x + 0.1, -1.54, 0.0);
  }
  const shade = new T.MeshBasicMaterial({
    color: p.color("grey", 900),
    transparent: true,
    opacity: 0,
    depthWrite: false,
  });
  const screen = mesh(w.root, new T.PlaneGeometry(29, 18), shade, 0, 0, 0.81);
  const rows = new Map<string, ReturnType<typeof bindBook>>(),
    slots = new Map<string, number>();
  const tones = [
    p.tone("success", 900, "grey", 800, 0.6),
    p.tone("danger", 900, "warning", 800, 0.5),
    p.tone("primary", 900, "grey", 700, 0.4),
    p.tone("success", 800, "warning", 800, 0.5),
  ];
  function add(key: string, name: string, i: number, cover?: T.Texture) {
    const row = bindBook(p, key, name, cover, tones[i]);
    rows.set(key, row);
    slots.set(key, i);
    w.root.add(row.root);
    pickable(w, row.root, key);
    return row;
  }
  (Object.keys(books) as PrintName[]).forEach((k, i) => add(k, k, i, art[k]));
  // A catalog-only volume can occupy the first display stand without expanding the shelf.
  let extra: string | undefined;
  function ensure(key: string, name: string) {
    if (rows.has(key)) return;
    if (extra) {
      const row = rows.get(extra)!;
      w.root.remove(row.root);
      w.hits.splice(w.hits.indexOf(row.root), 1);
      rows.delete(extra);
      slots.delete(extra);
      disposeBook(row);
    }
    add(key, name, 0);
    extra = key;
  }
  function pose(key: string, progress: number) {
    const pull = smooth(progress, 0, 0.67),
      open = smooth(progress, 0.3, 1);
    screen.visible = pull > 0.02;
    shade.opacity = pull * 0.86;
    for (const [k, row] of rows) {
      const i = slots.get(k)!,
        active = k === key,
        t = active ? pull : 0,
        opening = active ? open : 0;
      row.root.visible = !(k === extra && k !== key) && !(k === "Fable" && key === extra);
      const left = (i - 1.5) * 3.02 - volumeWidth / 2;
      row.root.position.set(mix(left, -0.03, t), 0.005 + 0.28 * Math.sin(Math.PI * t), mix(0.23, 2.05, t));
      const width = k === "Opus" ? 1.12 : k === "Haiku" ? 0.85 : 1,
        height = k === "Haiku" ? 0.84 : k === "Opus" ? 1.045 : 1;
      const size = mix(1, 1.91, t);
      row.root.scale.set(mix(width, 1, t) * size, mix(height, 1, t) * size, size);
      row.root.position.y += (((height - 1) * volumeHeight) / 2) * (1 - t);
      row.root.rotation.set(-0.055 * (1 - t), mix((i - 1.5) * 0.08, 0.015, t), 0.035 * Math.sin(Math.PI * t));
      row.hinge.rotation.y = -opening * (Math.PI - 0.045);
      row.glyphHit.visible = active && open > 0.98;
    }
    const row = rows.get(key)!;
    const { uv } = row.printed;
    row.right.updateWorldMatrix(true, false);
    const pos = new T.Vector3((uv.x - 0.5) * paperWidth, (uv.y - 0.5) * paperHeight, 0.05);
    row.right.localToWorld(pos);
    w.root.worldToLocal(pos);
    w.portal.copy(pos);
    return row.printed.macro;
  }
  return {
    world: w,
    ensure,
    pose,
    dispose: () => {
      for (const row of rows.values()) row.printed.macro.dispose();
    },
  };
}

function disposeBook(row: ReturnType<typeof bindBook>) {
  const mats = new Set<T.Material>(),
    maps = new Set<T.Texture>();
  row.root.traverse((o) => {
    if (o instanceof T.Mesh) {
      o.geometry.dispose();
      const m = o.material as T.Material & { map?: T.Texture };
      mats.add(m);
      if (m.map) maps.add(m.map);
    }
  });
  mats.forEach((m) => m.dispose());
  maps.forEach((m) => m.dispose());
  row.printed.macro.dispose();
}
