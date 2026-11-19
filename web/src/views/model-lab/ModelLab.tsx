import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowLeft, ArrowUp, Atom, Check, ChevronRight, CircleCheck, Layers3, Orbit, Origami } from "lucide-react";
import { fetchConfig, getJson } from "../../api";
import { modelsForTool } from "../../agentModels";
import { Select } from "../../components/Select";
import type { AgentAccounts, ConfigState } from "../../types";
import { Badge, Button, Card, cn } from "../../ui";
import { floorsFor, providers, readSelection, selectionKey, tools, type Sculpture, type Selection, type Tool, type Visit } from "./catalog";
import { GalleryCanvas } from "./GalleryCanvas";

const emblems = { agy: Atom, claude: Origami, codex: Orbit };
const tones = { agy: "bg-info-100 text-info-800", claude: "bg-warning-100 text-warning-800", codex: "bg-primary-100 text-primary-800" };
const captions: Record<Sculpture, string> = {
  Hydrogen: "단일 핵과 수소 기호를 새긴 금속 타일", Neon: "네온 타일 뒤로 교차하는 두 겹의 궤도",
  Argon: "아르곤 타일을 감싸는 세 겹의 궤도", Haiku: "종이 한 장에서 날개를 펼친 학",
  Sonnet: "산과 골이 번갈아 이어지는 종이 주름", Opus: "굽은 페이지 사이로 펼쳐지는 악보",
  Fable: "책장을 펼치면 솟아나는 성과 숲", Luna: "겹친 유리 곡면으로 빚은 초승달",
  Terra: "유리 표면 아래 흐르는 물결 모양의 지층", Sol: "투명한 구 안에서 빛나는 중심핵",
  Astra: "서로 다른 빛을 품은 별들의 성단", Other: "계정 카탈로그의 모델",
};

export default function ModelLab() {
  const [visit, setVisit] = useState<Visit | null>(null);
  const [catalog, setCatalog] = useState<ConfigState["agent_models"]>();
  const [accounts, setAccounts] = useState<AgentAccounts | null>(null);
  const [loading, setLoading] = useState(true), [failed, setFailed] = useState(false), [retry, setRetry] = useState(0);
  const [pickedId, setPickedId] = useState("");
  const [saved, setSaved] = useState<Selection | null>(() => readSelection(localStorage.getItem(selectionKey)));
  const [storageError, setStorageError] = useState(false);
  const region = useRef<HTMLElement>(null), entranceButtons = useRef<Partial<Record<Tool, HTMLButtonElement | null>>>({});
  useEffect(() => {
    let active = true;
    setLoading(true); setFailed(false);
    Promise.allSettled([fetchConfig(), getJson<AgentAccounts>("/api/agents/accounts")]).then(([config, acc]) => {
      if (!active) return;
      setCatalog(config.status === "fulfilled" ? config.value.agent_models : undefined);
      setAccounts(acc.status === "fulfilled" ? acc.value : null);
      setFailed(config.status === "rejected" || acc.status === "rejected");
      setLoading(false);
    });
    return () => { active = false; };
  }, [retry]);

  const tool = visit?.tool ?? "agy";
  const floors = useMemo(() => floorsFor(tool, modelsForTool(tool, catalog, accounts)), [tool, catalog, accounts]);
  const index = Math.max(0, floors.findIndex((f) => f.key === visit?.key)), floor = floors[index];
  const sceneVisit = useMemo(() => visit ? { tool, floors, index } : null, [visit, tool, floors, index]);
  const model = floor.models.find((m) => m.id === pickedId) ??
    floor.models.find((m) => saved?.tool === tool && m.id === saved.id) ?? floor.models[0];
  const account = accounts?.tools[tool];
  const available = !loading && !!account?.installed && account.logged_in && !!model;
  const availability = loading ? "계정 확인 중" : !account ? "계정 확인 필요" : !account.installed ? "도구 미설치" :
    !account.logged_in ? "로그인 필요" : available ? "사용 가능" : failed && !catalog ? "목록 확인 필요" : "계정 목록에 없음";
  const confirmed = !!model && saved?.tool === tool && saved.id === model.id;

  function enter(next: Tool) {
    setPickedId("");
    setVisit({ tool: next, key: floorsFor(next, [])[0].key });
    region.current?.focus({ preventScroll: true });
  }
  function exit() {
    setVisit(null);
    requestAnimationFrame(() => entranceButtons.current[tool]?.focus({ preventScroll: true }));
  }
  function move(i: number) {
    if (i < 0) { exit(); return; }
    if (i >= floors.length) return;
    setPickedId("");
    setVisit({ tool, key: floors[i].key });
  }
  function confirm() {
    if (!model || !available) return;
    const selection = { tool, id: model.id, name: model.name };
    setSaved(selection);
    try { localStorage.setItem(selectionKey, JSON.stringify(selection)); setStorageError(false); }
    catch { setStorageError(true); }
  }

  const ModelIcon = emblems[tool];
  return (
    <section ref={region} tabIndex={-1} aria-label="모델 선택 실험실" className="@container mx-auto max-w-[94rem] outline-none"
      onKeyDown={(e) => {
        if (!visit || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey || e.target instanceof HTMLSelectElement) return;
        if (e.key === "Escape") exit();
        else if (e.key === "ArrowDown") move(index + 1);
        else if (e.key === "ArrowUp") move(index - 1);
        else if (e.key === "Home") move(0);
        else if (e.key === "End") move(floors.length - 1);
        else return;
        e.preventDefault();
      }}>
      <header className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold tracking-tight text-text-main">모델 선택</h1>
        <div role="status" className="flex min-w-0 flex-wrap items-center gap-2 text-sm text-text-muted">
          <CircleCheck aria-hidden className={cn("size-4", saved && "text-success-700")} />
          <span>확정 모델</span>
          <strong className="break-all text-text-main">{saved ? providers[saved.tool].name + " · " + saved.name : "미선택"}</strong>
        </div>
      </header>
      <Card className="grid overflow-hidden @min-[48rem]:grid-cols-[minmax(0,1fr)_18rem]">
        <figure className="min-w-0 bg-background">
          <GalleryCanvas visit={sceneVisit} onEnter={enter} onStep={(d) => move(index + d)} />
          <figcaption className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-4 py-3 sm:px-5">
            <div className="flex min-w-0 items-start gap-3">
              <Layers3 aria-hidden className="mt-1 size-4 shrink-0 text-text-muted" />
              <div aria-live="polite" aria-atomic="true">
                <p className="font-semibold text-text-main">{visit ? `B${index + 1} · ${floor.name}` : "입구 홀"}</p>
                <p className="mt-0.5 text-xs text-text-muted">{visit ? captions[floor.sculpture] : "데크의 입구나 목록에서 프로바이더를 고르세요."}</p>
              </div>
            </div>
            {visit && <div className="flex gap-1.5">
              <Button size="sm" variant="secondary" aria-label="위층" title="위층 (↑)" onClick={() => move(index - 1)}><ArrowUp className="size-4" /></Button>
              <Button size="sm" variant="secondary" aria-label="아래층" title="아래층 (↓)" disabled={index === floors.length - 1} onClick={() => move(index + 1)}><ArrowDown className="size-4" /></Button>
            </div>}
          </figcaption>
        </figure>
        <div className="min-w-0 border-t border-border p-4 @min-[48rem]:border-l @min-[48rem]:border-t-0 sm:p-5">
          {!visit ? <>
            <h2 className="mb-3 text-base font-bold text-text-main">프로바이더 입구</h2>
            <div className="grid gap-2">
              {tools.map((id) => {
                const Icon = emblems[id];
                return <Button key={id} ref={(el) => { entranceButtons.current[id] = el; }} variant="secondary"
                  className="h-auto w-full justify-start px-3 py-3.5 text-left" onClick={() => enter(id)}>
                  <span className={cn("grid size-10 shrink-0 place-items-center rounded-lg", tones[id])}><Icon className="size-5" /></span>
                  <span className="min-w-0 flex-1"><strong className="block text-base">{providers[id].name}</strong>
                    <span className="text-xs text-text-muted">{providers[id].material}</span></span>
                  <ChevronRight className="size-4 text-text-muted" aria-hidden />
                </Button>;
              })}
            </div>
          </> : <>
            <Button variant="secondary" size="sm" onClick={exit}><ArrowLeft className="size-3.5" /> 입구로</Button>
            <h2 className="mt-4 flex items-center gap-2 text-xl font-bold text-text-main">
              <ModelIcon aria-hidden className="size-5" />{providers[tool].name}
            </h2>
            <nav aria-label="모델 층" className="my-3 grid max-h-60 gap-1 overflow-y-auto">
              {floors.map((f, i) => (
                <Button key={f.key} size="sm" variant="secondary" aria-current={i === index ? "step" : undefined}
                  className={cn("h-auto min-h-9 w-full justify-start gap-3 border-0 py-2 text-left", i === index ? "bg-primary-50 text-primary-800" : "text-text-muted")}
                  onClick={() => move(i)}>
                  <span className="font-mono text-xs tabular-nums">B{i + 1}</span><span className="min-w-0 flex-1 break-words">{f.name}</span>
                  {saved?.tool === tool && f.models.some((m) => m.id === saved.id) && <Check aria-label="확정 모델" className="size-4 shrink-0" />}
                </Button>
              ))}
            </nav>
            <div className="border-t border-border pt-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-xs font-semibold text-text-muted">실행 모델</span>
                <Badge status={available ? "success" : "draft"}>{availability}</Badge>
              </div>
              {floor.models.length > 1 ? <Select aria-label="실행 모델" className="mt-2 w-full" value={model.id} onChange={(e) => setPickedId(e.target.value)}>
                {floor.models.map((m) => <option key={m.id} value={m.id}>{m.name} ({m.id})</option>)}
              </Select> : <p className="mt-2 break-words text-sm font-semibold text-text-main">{model?.name ?? floor.name}</p>}
              {model && <p className="mt-0.5 break-all font-mono text-xs text-text-muted">{tool} / {model.id}</p>}
              <Button className="mt-4 w-full" disabled={!available || confirmed} onClick={confirm}>
                {confirmed && <Check className="size-4" />}{confirmed ? "선택 확정됨" : "이 모델 선택"}
              </Button>
            </div>
          </>}
          {failed && <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-text-muted" role="status">
            계정 또는 모델 목록을 불러오지 못했어요. <Button size="sm" variant="secondary" onClick={() => setRetry((n) => n + 1)}>다시 확인</Button>
          </div>}
          {storageError && <p role="status" className="mt-3 text-xs text-text-muted">브라우저 저장 공간을 사용할 수 없어 이번 방문 동안만 선택을 기억해요.</p>}
        </div>
      </Card>
      {visit && <p className="mt-3 text-xs leading-relaxed text-text-muted">휠·세로 스와이프 또는 ↑ ↓ 키로 층 이동 · 좌우 드래그로 회전 · Esc로 입구 이동</p>}
    </section>
  );
}
