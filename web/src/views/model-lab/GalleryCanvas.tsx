import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useThemeVersion } from "../../theme/theme";
import type { Tool } from "./catalog";
import type { Gallery, SceneVisit } from "./scene";

export function GalleryCanvas({ visit, onEnter, onStep }: {
  visit: SceneVisit; onEnter: (tool: Tool) => void; onStep: (direction: number) => void;
}) {
  const canvas = useRef<HTMLCanvasElement>(null), gallery = useRef<Gallery | null>(null);
  const current = useRef(visit);
  const [status, setStatus] = useState<"loading" | "ready" | "unavailable">("loading");
  const version = useThemeVersion();
  useLayoutEffect(() => { current.current = visit; gallery.current?.visit(visit); }, [visit]);
  useEffect(() => {
    const el = canvas.current!;
    let active = true;
    const lost = (e: Event) => { e.preventDefault(); setStatus("unavailable"); };
    const restored = () => queueMicrotask(() => {
      if (active && gallery.current) { gallery.current.visit(current.current, true); setStatus("ready"); }
    });
    el.addEventListener("webglcontextlost", lost);
    el.addEventListener("webglcontextrestored", restored);
    import("./scene").then(({ createGallery }) => {
      if (!active) return;
      gallery.current = createGallery(el);
      gallery.current.visit(current.current, true);
      setStatus("ready");
    }).catch(() => { if (active) setStatus("unavailable"); });
    return () => {
      active = false;
      el.removeEventListener("webglcontextlost", lost);
      el.removeEventListener("webglcontextrestored", restored);
      gallery.current?.dispose();
      gallery.current = null;
    };
  }, [version]);

  const wheel = useRef({ last: -Infinity, distance: 0, stepped: false });
  useEffect(() => {
    const el = canvas.current!;
    function move(event: WheelEvent) {
      if (!visit || event.ctrlKey || Math.abs(event.deltaX) > Math.abs(event.deltaY)) return;
      const gesture = wheel.current, now = performance.now();
      if (now - gesture.last > 190) { gesture.distance = 0; gesture.stepped = false; }
      gesture.last = now;
      event.preventDefault();
      if (gesture.stepped) return;
      gesture.distance += event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? el.clientHeight : 1);
      if (Math.abs(gesture.distance) >= 32) {
        gesture.stepped = true;
        onStep(Math.sign(gesture.distance));
      }
    }
    el.addEventListener("wheel", move, { passive: false });
    return () => el.removeEventListener("wheel", move);
  }, [visit, onStep]);

  const pointer = useRef<{ id: number; x: number; y: number; lastX: number; turned: boolean } | null>(null);
  return (
    <div className="relative overflow-hidden bg-grey-900">
      <canvas ref={canvas} aria-label={visit ? "모델 전시물" : "세 프로바이더의 원형 입구 데크"} role="img"
        className="block h-[clamp(15rem,56vw,26rem)] w-full @min-[48rem]:h-[clamp(25rem,59dvh,38rem)]"
        style={{ touchAction: visit ? "pinch-zoom" : "pan-y pinch-zoom" }}
        onPointerDown={(e) => {
          if (!e.isPrimary || e.button !== 0) return;
          pointer.current = { id: e.pointerId, x: e.clientX, y: e.clientY, lastX: e.clientX, turned: false };
          e.currentTarget.setPointerCapture(e.pointerId);
        }}
        onPointerMove={(e) => {
          const drag = pointer.current;
          if (drag?.id === e.pointerId && visit) {
            const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
            if (drag.turned || (Math.abs(dx) > 8 && Math.abs(dx) > Math.abs(dy) * 1.4)) {
              drag.turned = true;
              gallery.current?.turn(e.clientX - drag.lastX);
              drag.lastX = e.clientX;
            }
          } else if (!drag && e.pointerType === "mouse") {
            e.currentTarget.style.cursor = visit ? "grab" : gallery.current?.entrance(e.clientX, e.clientY) ? "pointer" : "default";
          }
        }}
        onPointerCancel={() => { pointer.current = null; }}
        onPointerUp={(e) => {
          const drag = pointer.current;
          pointer.current = null;
          if (!drag || e.pointerId !== drag.id || drag.turned) return;
          const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
          if (visit && Math.abs(dy) >= 36 && Math.abs(dy) > Math.abs(dx)) onStep(dy < 0 ? 1 : -1);
          else if (!visit && Math.hypot(dx, dy) < 8) {
            const tool = gallery.current?.entrance(e.clientX, e.clientY);
            if (tool) onEnter(tool);
          }
        }} />
      {status !== "ready" && <div role="status" className="pointer-events-none absolute inset-0 grid place-items-center bg-grey-100 p-6 text-center text-sm text-text-muted">
        {status === "loading" ? "3D 전시실을 여는 중…" : "이 환경에서는 3D 장면을 표시할 수 없어요. 목록에서 입구와 층을 고를 수 있어요."}
      </div>}
    </div>
  );
}
