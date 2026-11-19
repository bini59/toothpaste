import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  Atom,
  BookOpen,
  Check,
  CircleCheck,
  Orbit,
  Star,
  ZoomIn,
} from "lucide-react";
import { fetchConfig, getJson } from "../../api";
import { modelsForTool } from "../../agentModels";
import { Select } from "../../components/Select";
import type { AgentAccounts, ConfigState } from "../../types";
import { Badge, Button, Card, cn } from "../../ui";
import {
  arrivals,
  backFrom,
  zoomFrom,
  floorsFor,
  modelStatus,
  printFor,
  providers,
  readSelection,
  selectionKey,
  type Selection,
  type Visit,
} from "./catalog";
import { GalleryCanvas } from "./GalleryCanvas";

const scenes = {
  codex: { name: "태양계", icon: Orbit },
  claude: { name: "일본", icon: BookOpen },
  agy: { name: "원자 모형", icon: Atom },
};
const symbols: Record<string, string> = { Hydrogen: "H", Neon: "Ne", Argon: "Ar" };
const captions: Record<string, string> = {
  Hydrogen: "전자 한 개가 핵을 둘러싼 수소",
  Neon: "두 전자껍질을 가진 네온",
  Argon: "세 전자껍질을 가진 아르곤",
  Luna: "지구 곁을 도는 달",
  Terra: "태양계의 세 번째 행성, 지구",
  Sol: "행성들이 도는 중심별, 태양",
  Astra: "태양계 너머의 별 무리",
};

export default function ModelLab() {
  const [visit, setVisit] = useState<Visit | null>(null);
  const [catalog, setCatalog] = useState<ConfigState["agent_models"]>();
  const [accounts, setAccounts] = useState<AgentAccounts | null>(null);
  const [loading, setLoading] = useState(true),
    [failed, setFailed] = useState(false),
    [retry, setRetry] = useState(0);
  const [pickedId, setPickedId] = useState("");
  const [saved, setSaved] = useState<Selection | null>(() => {
    try {
      return readSelection(localStorage.getItem(selectionKey));
    } catch {
      return null;
    }
  });
  const [storageError, setStorageError] = useState(false);
  const region = useRef<HTMLElement>(null),
    list = useRef<HTMLElement>(null),
    nextButton = useRef<HTMLButtonElement>(null),
    startButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setFailed(false);
    Promise.allSettled([fetchConfig(), getJson<AgentAccounts>("/api/agents/accounts")]).then(([config, acc]) => {
      if (!active) return;
      setCatalog(config.status === "fulfilled" ? config.value.agent_models : undefined);
      setAccounts(acc.status === "fulfilled" ? acc.value : null);
      setFailed(config.status === "rejected" || acc.status === "rejected");
      setLoading(false);
    });
    return () => {
      active = false;
    };
  }, [retry]);

  const tool = visit?.tool ?? "codex";
  const floors = useMemo(() => floorsFor(tool, modelsForTool(tool, catalog, accounts)), [tool, catalog, accounts]);
  const index = Math.max(
      0,
      floors.findIndex((f) => f.key === visit?.key)
    ),
    floor = floors[index];
  const model =
    floor.models.find((m) => m.id === pickedId) ??
    floor.models.find((m) => saved?.tool === tool && m.id === saved.id) ??
    floor.models[0];
  const account = accounts?.tools[tool];
  const hasCatalog = (tool === "agy" && !!account?.models) || !!catalog?.[tool];
  const availability = modelStatus(tool, account, model, loading, hasCatalog);
  const selectable = availability === "사용 가능" || availability === "계정 권한 미확인";
  const confirmed = !!model && saved?.tool === tool && saved.id === model.id;
  const isHaiku = tool === "claude" && floor.key === "Haiku";
  const Icon = visit ? scenes[tool].icon : Star;
  const image = printFor(floor.key);

  useLayoutEffect(() => {
    const nav = list.current,
      item = nav?.querySelector<HTMLButtonElement>('[aria-current="step"]');
    if (!nav || !item) return;
    if (nav.contains(document.activeElement)) item.focus({ preventScroll: true });
    const bounds = nav.getBoundingClientRect(),
      row = item.getBoundingClientRect();
    if (row.bottom > bounds.bottom) nav.scrollTop += row.bottom - bounds.bottom;
    else if (row.top < bounds.top) nav.scrollTop += row.top - bounds.top;
  }, [visit, index]);

  function navigate(next: Visit | null) {
    setPickedId("");
    setVisit(next);
    if (next) region.current?.focus({ preventScroll: true });
    else requestAnimationFrame(() => startButton.current?.focus({ preventScroll: true }));
  }
  function zoom() {
    const next = zoomFrom(visit);
    if (next) navigate(next);
  }
  function back() {
    navigate(backFrom(tool));
  }
  function selectFamily(i: number) {
    if (i < 0 || i >= floors.length) return;
    setPickedId("");
    setVisit({ tool, key: floors[i].key });
  }
  function step(direction: number) {
    // A step can disable the button that triggered it. Keep arrow keys in the lab on arrival.
    if (document.activeElement === nextButton.current) region.current?.focus({ preventScroll: true });
    if (!visit) {
      if (direction > 0) zoom();
      return;
    }
    if (direction < 0 && index === 0) {
      back();
      return;
    }
    if (direction > 0 && (isHaiku || (tool === "codex" && index === floors.length - 1))) {
      zoom();
      return;
    }
    selectFamily(index + direction);
  }
  function chooseObject(key: string) {
    if (key === "@solar" || (isHaiku && key === "Haiku")) zoom();
    else if (visit) selectFamily(floors.findIndex((f) => f.key === key));
  }
  function confirm() {
    if (!model || !selectable) return;
    const selection = { tool, id: model.id, name: model.name };
    setSaved(selection);
    region.current?.focus({ preventScroll: true });
    try {
      localStorage.setItem(selectionKey, JSON.stringify(selection));
      setStorageError(false);
    } catch {
      setStorageError(true);
    }
  }

  return (
    <section
      ref={region}
      tabIndex={-1}
      aria-label="모델 선택 실험실"
      className="@container mx-auto max-w-[94rem] outline-none"
      onKeyDown={(e) => {
        if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey || e.target instanceof HTMLSelectElement) return;
        if (e.key === "Escape" && visit) back();
        else if (e.key === "ArrowDown") step(1);
        else if (e.key === "ArrowUp" && visit) step(-1);
        else if (e.key === "Home" && visit) selectFamily(0);
        else if (e.key === "End" && visit) selectFamily(floors.length - 1);
        else return;
        e.preventDefault();
      }}
    >
      <header className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold tracking-tight text-text-main">모델 선택</h1>
        <div role="status" className="flex min-w-0 flex-wrap items-center gap-2 text-sm text-text-muted">
          <CircleCheck aria-hidden className={cn("size-4", saved && "text-success-700")} />
          <span>확정 모델</span>
          <strong className="wrap-anywhere text-text-main">
            {saved ? providers[saved.tool].name + " · " + saved.name : "미선택"}
          </strong>
        </div>
      </header>
      <Card className="grid overflow-hidden @min-[54rem]:grid-cols-[minmax(0,1fr)_19rem]">
        <figure className="min-w-0 bg-background">
          <GalleryCanvas visit={visit} onPick={chooseObject} onStep={step} />
          <figcaption className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-4 py-3 sm:px-5">
            <div className="flex min-w-0 items-start gap-3">
              <Icon aria-hidden className="mt-1 size-4 shrink-0 text-text-muted" />
              <div aria-live="polite" aria-atomic="true" className="min-w-0 wrap-anywhere">
                <p className="font-semibold text-text-main">
                  {visit ? scenes[tool].name + " · " + floor.name : "별 무리"}
                </p>
                <p className="mt-0.5 text-xs text-text-muted">
                  {visit
                    ? (image?.alt ?? captions[floor.sculpture] ?? "계정 카탈로그의 모델")
                    : "빛나는 별을 눌러 태양계로 들어가세요."}
                </p>
              </div>
            </div>
            {visit && (
              <div className="flex gap-1.5">
                <Button size="sm" variant="secondary" aria-label="이전 모델 또는 장면" onClick={() => step(-1)}>
                  <ArrowUp className="size-4" />
                </Button>
                <Button
                  size="sm"
                  variant="secondary"
                  ref={nextButton}
                  aria-label="다음 모델 또는 장면"
                  disabled={index === floors.length - 1 && tool !== "codex" && !isHaiku}
                  onClick={() => step(1)}
                >
                  <ArrowDown className="size-4" />
                </Button>
              </div>
            )}
          </figcaption>
        </figure>
        <div className="min-w-0 border-t border-border p-4 @min-[54rem]:border-l @min-[54rem]:border-t-0 sm:p-5">
          {!visit ? (
            <>
              <h2 className="mb-2 text-xl font-bold text-text-main">별에서 원자까지</h2>
              <p className="mb-5 text-sm leading-relaxed text-text-muted">
                태양계의 천체, 일본의 문학 삽화, 원자 모형을 차례로 확대하며 모델을 고르세요.
              </p>
              <Button ref={startButton} className="w-full" onClick={zoom}>
                <Orbit className="size-4" />
                태양계로 들어가기
              </Button>
              <nav aria-label="프로바이더 바로가기" className="mt-5 grid gap-2 border-t border-border pt-4">
                <Button size="sm" variant="secondary" onClick={() => navigate(arrivals.claude)}>
                  <BookOpen className="size-4" />
                  Anthropic · 일본으로
                </Button>
                <Button size="sm" variant="secondary" onClick={() => navigate(arrivals.agy)}>
                  <Atom className="size-4" />
                  AGY · 원자 모형으로
                </Button>
              </nav>
            </>
          ) : (
            <>
              <div className="flex items-center justify-between gap-2">
                <h2 className="flex items-center gap-2 text-xl font-bold text-text-main">
                  <Icon aria-hidden className="size-5" />
                  {providers[tool].name}
                </h2>
                <Button variant="secondary" size="sm" onClick={back}>
                  <ArrowLeft className="size-3.5" />
                  {tool === "agy" ? "Haiku로" : tool === "claude" ? "태양계로" : "별 무리로"}
                </Button>
              </div>
              <nav ref={list} aria-label="모델 목록" className="my-3 grid max-h-64 grid-cols-2 gap-1.5 overflow-y-auto">
                {floors.map((f, i) => {
                  const art = printFor(f.key);
                  return (
                    <Button
                      key={f.key}
                      size="sm"
                      variant="secondary"
                      aria-current={i === index ? "step" : undefined}
                      className={cn(
                        "h-auto min-h-10 min-w-0 justify-start gap-2 py-1.5 text-left",
                        i === index ? "border-primary-300 bg-primary-50 text-primary-800" : "text-text-muted"
                      )}
                      onClick={() => selectFamily(i)}
                    >
                      {art ? (
                        <img
                          alt=""
                          src={art.image}
                          width={32}
                          height={32}
                          className="size-8 shrink-0 rounded object-cover"
                        />
                      ) : tool === "agy" && symbols[f.key] ? (
                        <span className="shrink-0 font-mono font-semibold">{symbols[f.key]}</span>
                      ) : null}
                      <span className="min-w-0 flex-1 wrap-anywhere">{f.name}</span>
                      {saved?.tool === tool && f.models.some((m) => m.id === saved.id) && (
                        <Check aria-label="확정 모델" className="size-3 shrink-0" />
                      )}
                    </Button>
                  );
                })}
              </nav>
              <div className="border-t border-border pt-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-xs font-semibold text-text-muted">실행 모델</span>
                  <Badge status={availability === "사용 가능" ? "success" : "draft"}>{availability}</Badge>
                </div>
                {floor.models.length > 1 ? (
                  <Select
                    aria-label="실행 모델"
                    className="mt-2 w-full"
                    value={model.id}
                    onChange={(e) => setPickedId(e.target.value)}
                  >
                    {floor.models.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.name} ({m.id})
                      </option>
                    ))}
                  </Select>
                ) : (
                  <p className="mt-2 wrap-anywhere text-sm font-semibold text-text-main">{model?.name ?? floor.name}</p>
                )}
                {model && (
                  <p className="mt-0.5 break-all font-mono text-xs text-text-muted">
                    {tool} / {model.id}
                  </p>
                )}
                {selectable && tool === "claude" && (
                  <p className="mt-2 text-xs leading-relaxed text-text-muted">
                    Claude CLI 별칭이에요. 실제 이용 권한은 계정에 따라 달라요.
                  </p>
                )}
                <Button className="mt-4 w-full" disabled={!selectable || confirmed} onClick={confirm}>
                  {confirmed && <Check className="size-4" />}
                  {confirmed ? "선택 확정됨" : isHaiku ? "Haiku 선택" : "이 모델 선택"}
                </Button>
              </div>
              {tool === "codex" && (
                <Button variant="secondary" className="mt-2 w-full" onClick={zoom}>
                  Anthropic · 지구에서 일본으로 <ArrowRight className="size-4" />
                </Button>
              )}
              {isHaiku && (
                <Button variant="secondary" className="mt-2 w-full" onClick={zoom}>
                  <ZoomIn className="size-4" />더 확대 · AGY 원자 모형
                </Button>
              )}
            </>
          )}
          {failed && (
            <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-text-muted" role="status">
              계정 또는 모델 목록을 불러오지 못했어요.{" "}
              <Button size="sm" variant="secondary" onClick={() => setRetry((n) => n + 1)}>
                다시 확인
              </Button>
            </div>
          )}
          {storageError && (
            <p role="status" className="mt-3 text-xs text-text-muted">
              브라우저 저장 공간을 사용할 수 없어 이번 방문 동안만 선택을 기억해요.
            </p>
          )}
        </div>
      </Card>
      {visit && (
        <p className="mt-3 text-xs leading-relaxed text-text-muted">
          휠·세로 스와이프 또는 ↑ ↓로 이동 · 좌우 드래그로 회전 · Esc로 이전 장면
        </p>
      )}
    </section>
  );
}
