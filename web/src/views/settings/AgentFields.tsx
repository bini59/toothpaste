import { useState } from "react";
import { Input, cn } from "../../ui";
import { Select } from "../../components/Select";
import type { AgentAccounts, AgentConf, ConfigState } from "../../types";
import { CheckRow, Row, inputCls, selectCls } from "./Form";
import { text } from "./values";

/** An empty card inherits the next level up. */
const agentOf = (n: AgentConf): AgentConf | null => {
  const kept = Object.fromEntries(Object.entries(n).filter(([, v]) => v !== null && v !== undefined && v !== "" && v !== false));
  return Object.keys(kept).length ? kept : null;
};

/**
 * tool / model / effort / ultracode rows for one agent setting: a repo's (repo.<name>.agent) or a role card's
 * (global.agents.<role>, TP-168). `inherit` = what an empty tool follows; a tool the account check (설정 > 에이전트)
 * found logged out is disabled, unless it is the saved value.
 */
export function AgentFields({ a, onChange, c, inherit, accounts }: {
  a: AgentConf; onChange: (v: AgentConf | null) => void; c: ConfigState; inherit: { label: string; tool: string }; accounts?: AgentAccounts | null;
}) {
  const tool = a.tool || inherit.tool;
  const models = (tool === "agy" ? accounts?.tools.agy?.models : undefined) ?? c.agent_models?.[tool] ?? [];
  const listed = models.find((m) => m.id === a.model);
  // a saved model the list does not have (hidden in the catalog, a full claude name) stays editable as typed text
  const [typing, setTyping] = useState(false);
  const customModel = typing || (!!a.model && !listed);
  const efforts = listed?.efforts?.length ? listed.efforts : c.agent_efforts[tool] || [];
  const save = (f: (n: AgentConf) => void) => { const n: AgentConf = { ...a }; f(n); onChange(agentOf(n)); };
  const pickTool = (v: string) => {
    setTyping(false);
    // model names and effort levels differ per CLI: start the rest over for the new tool
    save((n) => { n.tool = v || null; n.model = null; n.effort = null; n.ultracode = null; });
  };
  const pickModel = (v: string) => {
    if (v === "__typed") { setTyping(true); return; }
    setTyping(false);
    const m = models.find((x) => x.id === v);
    save((n) => {
      n.model = v || null;
      const ok = m?.efforts?.length ? m.efforts : c.agent_efforts[tool] || [];
      if (n.effort && (!ok.includes(n.effort) || tool === "agy" && m?.default_effort)) n.effort = null;
    });
  };
  const out = (x: string) => accounts?.tools[x] && !accounts.tools[x].logged_in;
  const defaultHint = listed?.default_effort ? " · 이 모델 기본값 " + listed.default_effort : "";
  const modelHint = {
    claude: "별칭은 그 계열의 최신 모델 · 전체 이름은 직접 입력",
    codex: models.length ? "codex에 로그인한 계정이 쓸 수 있는 모델 (codex debug models)" : "모델 목록을 못 불러왔어요 · 직접 입력",
    agy: accounts?.tools.agy?.neon_reason || "연결된 Antigravity 계정의 모델 (agy models)",
  }[tool];
  const defaultModel = { claude: "Claude Code 설정 따름", codex: "~/.codex/config.toml 따름", agy: "Antigravity CLI 설정 따름" }[tool];
  return (
    <>
      <Row label="도구" desc={`비우면 ${inherit.label} (${inherit.tool})`}>
        <Select className={selectCls} value={a.tool || ""} onChange={(e) => pickTool(e.target.value)}>
          <option value="">{inherit.label} 따름 ({inherit.tool})</option>
          {c.agents.map((x) => <option key={x} value={x} disabled={out(x) && a.tool !== x}>{x}{out(x) ? " (로그인 안 됨)" : ""}</option>)}
        </Select>
      </Row>
      <Row group label="모델" desc={modelHint}>
        <Select className={selectCls} value={tool !== "agy" && customModel ? "__typed" : a.model || ""} onChange={(e) => pickModel(e.target.value)} aria-label="모델">
          <option value="">{defaultModel}</option>
          {models.map((m) => <option key={m.id} value={m.id}>{m.name === m.id ? m.id : m.name + " (" + m.id + ")"}</option>)}
          {tool === "agy" ? (a.model && !listed && <option value={a.model} disabled>{a.model} (현재 목록에 없음)</option>)
            : <option value="__typed">직접 입력…</option>}
        </Select>
        {tool !== "agy" && customModel && (
          <Input className={cn(inputCls, "mt-2 sm:w-72")} value={text(a.model)} placeholder={tool === "claude" ? "claude-opus-5-5" : "gpt-6.1-sol"} aria-label="모델 이름"
            onChange={(e) => { const v = e.target.value.trim(); save((n) => { n.model = v || null; }); }} />
        )}
      </Row>
      {!(tool === "agy" && listed?.default_effort) && <Row label="추론 강도" desc={"비우면 CLI 설정 그대로" + defaultHint}>
        <Select className={selectCls} value={a.effort || ""} onChange={(e) => { const v = e.target.value; save((n) => { n.effort = v || null; }); }}>
          <option value="">CLI 설정 따름</option>
          {efforts.map((x) => <option key={x} value={x}>{x}</option>)}
        </Select>
      </Row>}
      {tool === "claude" && (
        <CheckRow label="ultracode" checked={!!a.ultracode} onChange={(v) => save((n) => { n.ultracode = v || null; })}
          desc="Claude가 큰 작업을 여러 하위 에이전트로 나눠 병렬로 처리해요. 결과는 더 꼼꼼해지지만 사용량이 크게 늘어요. dynamic workflows가 켜진 계정에서, xhigh를 지원하는 모델일 때만 실제로 켜져요." />
      )}
    </>
  );
}
