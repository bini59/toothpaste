import { useCallback, useEffect, useState } from "react";
import { ArrowRight } from "lucide-react";
import { Badge, Button, Card, Input } from "../ui";
import { ConfigError, fetchConfig, getJson, postConfig } from "../api";
import type { AgentAccounts, GlobalConf, Project } from "../types";
import { Accounts, AsanaConnect, GitHubConnect, Row, Section } from "./Settings";

const DEFAULT_OWNER = "the user"; // core.DEFAULT_SETTINGS owner_name

/** 시작하기 (#start, TP-144): what `tp setup` leaves to the browser, each step reusing its settings screen */
export function Start({ projects, onChanged }: { projects: Project[] | null; onChanged: () => void }) {
  const [asana, setAsana] = useState<boolean | null>(null);
  const [github, setGithub] = useState<boolean | null>(null);
  const [g, setG] = useState<GlobalConf | null>(null);
  const [name, setName] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [agents, setAgents] = useState<AgentAccounts | null>(null);
  const [agentErr, setAgentErr] = useState("");
  const loadAgents = useCallback(() => {
    setAgents(null);
    getJson<AgentAccounts>("/api/agents/accounts").then((x) => { setAgents(x); setAgentErr(""); }, (e) => setAgentErr(String(e)));
  }, []);
  useEffect(loadAgents, [loadAgents]);
  useEffect(() => {
    fetchConfig().then((c) => { setG(c.config.global); setName(c.config.global.owner_name === DEFAULT_OWNER ? "" : String(c.config.global.owner_name)); },
      (e) => setErr(String(e)));
  }, []);
  const named = !!g && g.owner_name !== DEFAULT_OWNER;
  const steps: [string, string, boolean | null][] = [
    ["asana", "Asana 연결", asana],
    ["github", "GitHub 연결", github],
    // TP-179: logged in from the 활성 계정 card (Docker, Linux, macOS alike); one connected agent is enough
    ["agents", "에이전트 로그인", agents ? Object.values(agents.tools).some((t) => t.logged_in) : agentErr ? false : null],
    ["name", "사용자 이름", g ? named : null],
    ["project", "첫 프로젝트 추가", projects ? projects.length > 0 : null],
  ];
  const done = steps.every(([, , ok]) => ok);
  const saveName = async () => {
    if (!g) return;
    setBusy(true);
    setErr("");
    try {
      // only owner_name: g is the mount-time copy, sending it whole would undo a workspace picked above (TP-148)
      await postConfig("/api/config", { scope: "global", value: { owner_name: name.trim() } });
      setG({ ...g, owner_name: name.trim() });
      onChanged();
    } catch (e) {
      setErr(e instanceof ConfigError ? e.errors.join("; ") : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <div>
        <h1 className="text-xl font-bold text-text-main sm:text-2xl">시작하기</h1>
        <p className="mt-1 text-sm text-text-muted">toothpaste(치약)를 처음 쓰는 순서예요. 아래 다섯 단계를 마치면 티켓을 받아 에이전트가 일하기 시작해요. 나중에 설정에서 다시 열 수 있어요.</p>
      </div>
      <Card className="p-4 sm:p-5">
        <ol className="space-y-2">
          {steps.map(([id, label, ok], i) => (
            <li key={id} className="flex items-center gap-3 text-sm">
              <span className="w-5 text-right font-semibold text-text-muted">{i + 1}</span>
              <button type="button" onClick={() => document.getElementById(`start-${id}`)?.scrollIntoView({ behavior: "smooth" })}
                className="flex-1 text-left font-semibold text-text-main hover:underline">{label}</button>
              {/* one width for every state: 확인 중 -> 완료 must not move the row (TP-174) */}
              <Badge status={ok === null ? "draft" : ok ? "success" : "warning"} className="w-16 justify-center">{ok === null ? "확인 중" : ok ? "완료" : "남음"}</Badge>
            </li>
          ))}
        </ol>
      </Card>
      <div id="start-asana"><AsanaConnect onStatus={setAsana} /></div>
      <div id="start-github"><GitHubConnect onStatus={setGithub} /></div>
      <div id="start-agents"><Accounts a={agents} error={agentErr} onRefresh={() => { loadAgents(); onChanged(); }} /></div>
      <div id="start-name">
        <Section title="사용자 이름" desc="에이전트가 질문하거나 보고할 때 이 이름으로 불러요.">
          <Row label="이름">
            <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); void saveName(); }}>
              <Input className="h-9 text-sm" value={name} placeholder="예: the owner" onChange={(e) => setName(e.target.value)} />
              <Button size="sm" type="submit" className="shrink-0 whitespace-nowrap" disabled={!g || !name.trim() || name.trim() === g.owner_name} loading={busy}>저장</Button>
            </form>
            {err && <p role="alert" className="mt-2 text-sm text-danger-700">{err}</p>}
          </Row>
        </Section>
      </div>
      <div id="start-project">
        <Section title="첫 프로젝트 추가" desc="GitHub 레포와 티켓 출처(Asana 프로젝트, GitHub 이슈, Notion 데이터베이스)를 고르세요."
          aside={<Button size="sm" variant={projects?.length ? "secondary" : undefined} onClick={() => { location.hash = "projects"; }}>
            {projects?.length ? "프로젝트 보기" : "프로젝트 추가"}
          </Button>}>
          <p className="py-4 text-sm text-text-muted">
            {projects?.length ? `프로젝트 ${projects.length}개가 있어요.` : "아직 프로젝트가 없어요."}
          </p>
        </Section>
      </div>
      {/* last: it shows only once every step is known, and would push the steps below it down (TP-174) */}
      {done && (
        <div role="status" className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-success-200 bg-success-50 p-3">
          <span className="text-sm font-semibold text-success-700">준비가 끝났어요. 보드에서 티켓을 확인하세요.</span>
          <Button size="sm" onClick={() => { location.hash = "board"; }}>보드로 가기 <ArrowRight className="h-4 w-4" /></Button>
        </div>
      )}
      <p className="text-sm text-text-muted">
        티켓 단계, 마일스톤, 배포가 궁금하면 <a href="#help" className="font-semibold text-primary hover:underline">도움말</a>을 보세요.
      </p>
    </div>
  );
}
