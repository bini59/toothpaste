import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useThemeVersion } from "../../theme/theme";
import type { Gallery, SceneVisit } from "./scene";

export function GalleryCanvas({ visit, onPick, onStep }: {
  visit: SceneVisit; onPick: (choice: string) => void; onStep: (direction: number) => void;
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
      if (status !== "ready" || (!visit && event.deltaY < 0) || event.ctrlKey || Math.abs(event.deltaX) > Math.abs(event.deltaY)) return;
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
  }, [visit, onStep, status]);

  const pointer = useRef<{ id: number; x: number; y: number; lastX: number; turned: boolean } | null>(null);
  return (
    <div data-renderer={status} className="relative aspect-[1.6] w-full max-h-[min(63dvh,42rem)] min-h-60 overflow-hidden bg-grey-900">
      <canvas ref={canvas} aria-label={visit ? ({ codex: "OpenAI 태양계", claude: "일본의 네 문학 삽화", agy: "AGY 원자 모형" })[visit.tool] : "별 무리와 태양계 입구"} role="img"
        className="absolute inset-0 block h-full w-full"
        style={{ touchAction: status === "ready" ? "pinch-zoom" : "pan-y pinch-zoom" }}
        onPointerDown={(e) => {
          if (status !== "ready" || !e.isPrimary || e.button !== 0) return;
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
            e.currentTarget.style.cursor = gallery.current?.pick(e.clientX, e.clientY) ? "pointer" : visit ? "grab" : "default";
          }
        }}
        onPointerCancel={() => { pointer.current = null; }}
        onPointerUp={(e) => {
          const drag = pointer.current;
          pointer.current = null;
          if (!drag || e.pointerId !== drag.id || drag.turned) return;
          const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
          if (Math.abs(dy) >= 36 && Math.abs(dy) > Math.abs(dx)) onStep(dy < 0 ? 1 : -1);
          else if (Math.hypot(dx, dy) < 8) {
            const choice = gallery.current?.pick(e.clientX, e.clientY);
            if (choice) onPick(choice);
          }
        }} />
      {status !== "ready" && <div role="status" className="pointer-events-none absolute inset-0 grid place-items-center bg-grey-100 p-6 text-center text-sm text-text-muted">
        {status === "loading" ? "3D 장면을 여는 중…" : "이 환경에서는 3D 장면을 표시할 수 없어요. 아래나 옆의 버튼에서 같은 장면과 모델을 선택할 수 있어요."}
      </div>}
    </div>
  );
}
