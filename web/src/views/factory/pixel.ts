// Native-resolution pixel renderer for the factory: every shape is rasterized at 1 px = 1 art pixel
// (2:1 iso stair lines, 1 px outer outline, lit top edges), then the canvas is scaled up by an integer with no smoothing.
// Colors are theme token names ("grey-500"), read from the theme's CSS variables.

import { themeVersion } from "../../theme/theme";
import type { Proj } from "./layout";

export type Paint = string | ((x: number, y: number) => string);
type Pt = [number, number];


const cache = new Map<string, number>();
let cachedFor = -1; // theme version the cache was filled under
function rgba(token: string): number {
  if (cachedFor !== themeVersion()) {
    cache.clear();
    cachedFor = themeVersion();
  }
  let v = cache.get(token);
  if (v === undefined) {
    const hex = getComputedStyle(document.documentElement).getPropertyValue("--color-" + token).trim();
    const n = parseInt(hex.slice(1), 16);
    // ImageData is RGBA bytes; on little-endian a Uint32 reads them as ABGR
    v = (255 << 24) | ((n & 255) << 16) | (n & 0xff00) | ((n >> 16) & 255);
    v >>>= 0;
    cache.set(token, v);
  }
  return v;
}

export class Frame {
  buf: Uint32Array<ArrayBuffer>;
  constructor(public w: number, public h: number) {
    this.buf = new Uint32Array(w * h);
  }
  layer() {
    return new Layer(this);
  }
  fill(token: string) {
    this.buf.fill(rgba(token));
  }
  draw(ctx: CanvasRenderingContext2D) {
    ctx.putImageData(new ImageData(new Uint8ClampedArray(this.buf.buffer), this.w, this.h), 0, 0);
  }
}

/** one object: drawn into its own pixel set, outlined, then committed in painter order */
export class Layer {
  px = new Map<number, string>();
  constructor(readonly f: Frame) {}
  set(x: number, y: number, paint: Paint) {
    if (x < 0 || y < 0 || x >= this.f.w || y >= this.f.h) return;
    this.px.set(y * this.f.w + x, typeof paint === "string" ? paint : paint(x, y));
  }
  /** convex polygon, filled by pixel centers so 2:1 edges come out as clean 2-px stairs; returns the filled indices */
  poly(p: Pt[], paint: Paint): number[] {
    const xs = p.map((q) => q[0]);
    const ys = p.map((q) => q[1]);
    const out: number[] = [];
    for (let y = Math.floor(Math.min(...ys)); y < Math.ceil(Math.max(...ys)); y++)
      for (let x = Math.floor(Math.min(...xs)); x < Math.ceil(Math.max(...xs)); x++) {
        const [cx, cy] = [x + 0.5, y + 0.5];
        let pos = false;
        let neg = false;
        for (let i = 0; i < p.length; i++) {
          const [ax, ay] = p[i];
          const [bx, by] = p[(i + 1) % p.length];
          const c = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax);
          if (c > 1e-9) pos = true;
          if (c < -1e-9) neg = true;
        }
        if (!(pos && neg)) this.set(x, y, paint), out.push(y * this.f.w + x);
      }
    return out;
  }
  /** pixel-art sprite: rows of palette chars, '.' is clear; bottom-center lands on (ax, ay) */
  sprite(rows: string[], pal: Record<string, string>, ax: number, ay: number, flip = false) {
    const w = rows[0].length;
    const x0 = Math.round(ax - w / 2);
    const y0 = Math.round(ay - rows.length);
    rows.forEach((r, y) => [...r].forEach((ch, x) => ch !== "." && pal[ch] && this.set(x0 + (flip ? w - 1 - x : x), y0 + y, pal[ch])));
  }
  /** 1 px outer outline, then copy into the frame */
  commit(outline: string | null = "grey-900") {
    const { w, buf } = this.f;
    if (outline)
      for (const i of this.px.keys())
        for (const j of [i - 1, i + 1, i - w, i + w]) if (j >= 0 && j < buf.length && !this.px.has(j)) buf[j] = rgba(outline);
    for (const [i, t] of this.px) buf[i] = rgba(t);
  }
}

export interface Shade { top: Paint; left: Paint; right: Paint; hi?: string; edge?: string; foot?: string }
/** box over grid x0..x1 by y0..y1, z..z+h art px. hi: front edges of the top; edge: the front vertical corner;
 * foot: the bottom row of the side faces (contact shade) */
export function box(L: Layer, at: Proj, x0: number, y0: number, x1: number, y1: number, z: number, h: number, s: Shade) {
  const z1 = z + h;
  const w = L.f.w;
  if (h > 0) {
    const left = L.poly([at(x0, y1, z), at(x1, y1, z), at(x1, y1, z1), at(x0, y1, z1)], s.left);
    const right = L.poly([at(x1, y0, z), at(x1, y1, z), at(x1, y1, z1), at(x1, y0, z1)], s.right);
    const ls = new Set(left);
    const sides = new Set([...left, ...right]);
    if (s.foot) for (const i of sides) if (!sides.has(i + w)) L.px.set(i, s.foot);
    if (s.edge) for (const i of right) if (ls.has(i - 1)) L.px.set(i, s.edge);
  }
  const top = L.poly([at(x0, y0, z1), at(x1, y0, z1), at(x1, y1, z1), at(x0, y1, z1)], s.top);
  if (s.hi) {
    const set = new Set(top);
    for (const i of top) if (!set.has(i + w)) L.px.set(i, s.hi);
  }
}
/** upright quad on a box face */
export const face = (L: Layer, at: Proj, a: Pt, b: Pt, z0: number, z1: number, paint: Paint) =>
  L.poly([at(a[0], a[1], z0), at(b[0], b[1], z0), at(b[0], b[1], z1), at(a[0], a[1], z1)], paint);

/** soft drop shadow on the floor: the footprint pushed toward the front-left, in one darker floor tone */
export const shadow = (L: Layer, at: Proj, x0: number, y0: number, x1: number, y1: number, d = 0.18) =>
  L.poly([at(x0, y0 + d), at(x1, y0 + d), at(x1, y1 + d), at(x0, y1 + d)], "grey-400");
