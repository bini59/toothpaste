import { useEffect, useMemo, useState } from "react";
import { Card, Input } from "../ui";
import { getJson } from "../api";
import { Select } from "../components/Select";
import type { HistoryRow } from "../types";
import { ACTOR, History } from "./settings/History";

type Kind = "" | "global" | "repo" | "project";
const KIND: Record<Exclude<Kind, "">, string> = { global: "전역", repo: "저장소", project: "프로젝트" };
const kindOf = (scope: string): Exclude<Kind, ""> => (scope === "global" ? "global" : scope.startsWith("project:") ? "project" : "repo");
const day = (ts: string) => { const d = new Date(ts); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };

/** 모니터링 > 설정 이력 (TP-168): every settings_history row, filtered by scope, actor and date. `sub` = one scope (#history/<scope>). */
export function SettingsHistory({ sub }: { sub: string }) {
  const [rows, setRows] = useState<HistoryRow[] | null>(null);
  const [error, setError] = useState("");
  const [kind, setKind] = useState<Kind>(sub ? kindOf(sub) : "");
  const [scope, setScope] = useState(sub);
  const [actor, setActor] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  // leaving #history/<scope> by picking another 범위 keeps that pick
  useEffect(() => { if (sub) setKind(kindOf(sub)); setScope(sub); }, [sub]);
  useEffect(() => void getJson<{ rows: HistoryRow[] }>("/api/config/history").then((r) => setRows(r.rows), (e) => setError(String(e))), []);
  const scopes = useMemo(() => [...new Set((rows ?? []).map((r) => r.scope))].filter((s) => !kind || kindOf(s) === kind).sort(), [rows, kind]);
  const actors = useMemo(() => [...new Set((rows ?? []).map((r) => r.actor))].sort(), [rows]);
  const shown = (rows ?? []).filter((r) => (!kind || kindOf(r.scope) === kind) && (!scope || r.scope === scope) && (!actor || r.actor === actor)
    && (!from || day(r.ts) >= from) && (!to || day(r.ts) <= to));
  const pick = (v: string) => { setScope(v); if (v || sub) location.replace(v ? `#history/${encodeURIComponent(v)}` : "#history"); };
  return (
    <div>
      <div className="mb-4">
        <h1 className="text-xl font-bold text-text-main sm:text-2xl">설정 이력</h1>
        <p className="mt-1 text-sm text-text-muted">전역, 저장소, 프로젝트 설정이 바뀐 기록이에요. 누가(대시보드, CLI, GitHub 아이디) 언제 무엇을 바꿨는지 보여줘요.</p>
      </div>
      <Card className="p-4 sm:p-5">
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col text-xs font-semibold text-text-muted">범위
            <Select className="mt-1 h-9 w-32" value={kind} onChange={(e) => { setKind(e.target.value as Kind); pick(""); }}>
              <option value="">전체</option>
              {Object.entries(KIND).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
            </Select>
          </label>
          <label className="flex flex-col text-xs font-semibold text-text-muted">대상
            <Select className="mt-1 h-9 w-56" value={scope} onChange={(e) => pick(e.target.value)}>
              <option value="">전체</option>
              {/* a filtered scope with no rows yet still shows as picked */}
              {[...new Set([...scopes, ...(scope ? [scope] : [])])].map((s) => <option key={s} value={s}>{s}</option>)}
            </Select>
          </label>
          <label className="flex flex-col text-xs font-semibold text-text-muted">누가
            <Select className="mt-1 h-9 w-40" value={actor} onChange={(e) => setActor(e.target.value)}>
              <option value="">전체</option>
              {actors.map((a) => <option key={a} value={a}>{ACTOR[a] ?? a}</option>)}
            </Select>
          </label>
          <label className="flex flex-col text-xs font-semibold text-text-muted">부터
            <Input type="date" className="mt-1 h-9 w-40 text-sm" value={from} onChange={(e) => setFrom(e.target.value)} />
          </label>
          <label className="flex flex-col text-xs font-semibold text-text-muted">까지
            <Input type="date" className="mt-1 h-9 w-40 text-sm" value={to} onChange={(e) => setTo(e.target.value)} />
          </label>
          {rows && <span className="pb-2 text-xs text-text-muted">{shown.length} / {rows.length}개</span>}
        </div>
        <div className="mt-4">
          {!rows ? <p className="text-sm text-text-muted">{error ? `불러오지 못했어요: ${error}` : "불러오는 중…"}</p> : <History rows={shown} scope />}
        </div>
      </Card>
    </div>
  );
}
