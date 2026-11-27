import { useCallback, useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { Badge, Button, Input, cn } from "../../ui";
import { getJson, post, postJson } from "../../api";
import { ConfirmButton } from "../../components/ConfirmButton";
import type { AgentAccount, AgentAccounts, CodexLogin } from "../../types";
import { Block, Row, Section, inputCls } from "./Form";

export function useAgentAccounts() {
  const [accounts, setAccounts] = useState<AgentAccounts | null>(null);
  const [error, setError] = useState("");
  const reload = useCallback(() => {
    setAccounts(null);
    getJson<AgentAccounts>("/api/agents/accounts").then((x) => { setAccounts(x); setError(""); }, (e) => setError(String(e)));
  }, []);
  useEffect(reload, [reload]);
  return { accounts, error, reload };
}

const METHOD: Record<string, string> = { cli: "CLI 로그인", oauth_token: "tp 토큰", api_key: "API 키" };
const errBox = "mt-3 rounded-lg border border-danger-200 bg-danger-50 p-3 text-sm text-danger-700";

/** 에이전트 로그인 buttons of one tool (TP-179): codex Device Flow; claude setup-token, a pasted token or an API key.
 * Codes, tokens and keys go to the server and are never shown again. */
function ToolLogin({ tool, t, onChanged }: { tool: string; t: AgentAccount; onChanged: () => void }) {
  const [codex, setCodex] = useState<CodexLogin | null>(null);
  const [oauthUrl, setOAuthUrl] = useState("");
  const [form, setForm] = useState<"" | "token" | "apikey">("");
  const [val, setVal] = useState("");
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [copied, setCopied] = useState(false);
  const base = `/api/agents/${tool}`;
  const run = async (what: string, f: () => Promise<unknown>) => {
    setBusy(what);
    setErr("");
    try {
      await f();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy("");
    }
  };
  // codex: poll until codex login status says done, or the code expires / the login fails
  useEffect(() => {
    if (codex?.state !== "pending") return;
    const id = window.setInterval(() => {
      getJson<CodexLogin>(`${base}/login`).then((r) => {
        if (r.state === "pending") return;
        setCodex(null);
        // idle: another tab or poll already took the result; the refreshed status shows it
        if (r.state === "done" || r.state === "idle") onChanged();
        else setErr(r.error || "로그인하지 못했어요. 다시 해 주세요");
      }, (e: Error) => { setCodex(null); setErr(e.message); });
    }, 3000);
    return () => clearInterval(id);
  }, [codex, base, onChanged]);
  const start = () => run("login", async () => {
    setCopied(false);
    const r = await postJson<CodexLogin>(`${base}/login`, {});
    if (tool === "codex") setCodex(r);
    else if (r.state === "done") onChanged();
    else setOAuthUrl(r.url || "");
  });
  const cancel = () => run("cancel", async () => {
    await post(`${base}/login/cancel`, {});
    setCodex(null);
    setOAuthUrl("");
    setVal("");
  });
  const submit = (path: string, body: unknown) => run("submit", async () => {
    try {
      await post(`${base}/${path}`, body);
    } finally {
      // a wrong code ends the server's setup-token too: the form goes with it (log in again)
      if (path === "login/code") { setOAuthUrl(""); setVal(""); }
    }
    setVal("");
    setForm("");
    setOAuthUrl("");
    onChanged();
  });
  const logout = () => run("logout", async () => { await post(`${base}/logout`, {}); onChanged(); });
  const cliOk = t.method === "cli";
  const pending = !!codex || !!oauthUrl;
  const secretForm = (label: string, ph: string, path: string, key: string) => (
    <form className="flex flex-wrap items-center gap-2" onSubmit={(e) => { e.preventDefault(); void submit(path, { [key]: val }); }}>
      <Input type="password" autoComplete="off" aria-label={label} placeholder={ph} className={cn(inputCls, "w-72 font-mono")}
        value={val} onChange={(e) => setVal(e.target.value)} />
      <Button size="sm" type="submit" disabled={!val.trim()} loading={busy === "submit"}>{path === "login/code" ? "확인" : "저장"}</Button>
      <Button size="sm" variant="secondary" type="button" onClick={() => (path === "login/code" ? void cancel() : (setForm(""), setVal("")))}>취소</Button>
    </form>
  );
  return (
    <div className="space-y-2">
      {!pending && !form && (
        <div className="flex flex-wrap items-center gap-2">
          {(tool === "codex" ? t.logged_in : !!t.saved) ? (
            <ConfirmButton size="sm" variant="secondary" confirmLabel="로그아웃" onConfirm={() => void logout()} loading={busy === "logout"}
              message={tool === "codex" ? "codex logout을 실행해요. 다시 로그인할 때까지 codex 에이전트가 멈춰요."
                : tool === "agy" ? "tp에 저장한 Gemini API 키 연결을 해제해요."
                  : "tp에 저장한 토큰·키만 지워요. claude CLI 자체 로그인(~/.claude, 키체인)은 그대로예요."}>
              로그아웃
            </ConfirmButton>
          ) : (!t.logged_in || tool === "agy" && cliOk) && (
            <>
              {!t.logged_in && <Button size="sm" onClick={() => void start()} loading={busy === "login"}>
                {tool === "agy" ? "Google 계정 로그인" : "로그인"}
              </Button>}
              {tool === "agy" && <Button size="sm" variant="secondary" onClick={() => setForm("apikey")}>Gemini API 키</Button>}
              {tool === "claude" && <>
                <Button size="sm" variant="secondary" onClick={() => setForm("token")}>토큰 붙여넣기</Button>
                <Button size="sm" variant="secondary" onClick={() => setForm("apikey")}>API 키</Button>
              </>}
            </>
          )}
        </div>
      )}
      {tool === "agy" && cliOk && <p className="text-xs text-text-muted">Google 계정에서 로그아웃하려면 실행 환경의 터미널에서 agy를 열고 /logout을 입력하세요.</p>}
      {tool === "agy" && t.method === "api_key" && !t.saved &&
        <p className="text-xs text-text-muted">실행 환경의 GEMINI_API_KEY를 쓰고 있어요. 연결을 해제하려면 agy 설정에서 modelProvider를 지우세요.</p>}
      {tool === "claude" && cliOk && !t.saved && <p className="text-xs text-text-muted">CLI 로그인을 그대로 써요. tp는 따로 토큰을 넣지 않아요.</p>}
      {tool === "claude" && t.saved && cliOk && <p className="text-xs text-text-muted">CLI 로그인이 있어서 tp에 저장한 {METHOD[t.saved]}은 쓰지 않아요.</p>}
      {codex?.code && (
        <div className="rounded-lg border border-border bg-grey-50 p-4">
          <p className="text-sm text-text-main">주소를 열어 이 코드를 입력하세요. 입력하면 자동으로 로그인돼요.</p>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <span className="font-mono text-3xl font-bold tracking-widest text-text-main">{codex.code}</span>
            <Button size="sm" variant="secondary" onClick={() => navigator.clipboard.writeText(codex.code ?? "").then(() => setCopied(true), () => setCopied(false))}>
              {copied ? "복사됨" : "복사"}
            </Button>
            <a href={codex.url} target="_blank" rel="noreferrer" className="text-sm font-semibold text-primary-700 underline">열기</a>
            <Button size="sm" variant="secondary" onClick={() => void cancel()} loading={busy === "cancel"}>취소</Button>
          </div>
          <p className="mt-2 text-xs text-text-muted" role="status">기다리는 중… 코드는 {Math.round((codex.expires_in ?? 900) / 60)}분 동안 쓸 수 있어요.</p>
        </div>
      )}
      {oauthUrl && (
        <div className="space-y-3 rounded-lg border border-border bg-grey-50 p-4 text-sm text-text-main">
          <p><a href={oauthUrl} target="_blank" rel="noreferrer" className="font-semibold text-primary-700 underline">
            {tool === "agy" ? "Google 로그인 열기" : "Claude 로그인 열기"}
          </a></p>
          <p>{tool === "agy" ? "Google에서 받은 인증 코드를 붙여넣으세요. CLI가 로그인을 저장해요."
            : "Claude 구독 계정으로 로그인하고 코드를 붙여넣으세요. 1년짜리 토큰을 tp에 저장해요."}</p>
          {secretForm(tool === "agy" ? "Google 로그인 코드" : "Claude 로그인 코드", "코드 붙여넣기", "login/code", "code")}
        </div>
      )}
      {form === "token" && (
        <div className="space-y-2 text-sm text-text-main">
          <p className="text-xs text-text-muted">터미널에서 <code className="font-mono text-text-main">claude setup-token</code>을 실행하고 출력된 토큰(sk-ant-oat…)을 붙여넣으세요.</p>
          {secretForm("Claude 토큰", "sk-ant-oat…", "token", "token")}
        </div>
      )}
      {form === "apikey" && (
        <div className="space-y-2 text-sm text-text-main">
          <p className="text-xs text-text-muted">{tool === "agy"
            ? "결제가 활성화된 Google AI Studio 프로젝트의 키를 사용하세요. 실행 비용은 그 프로젝트에 청구돼요."
            : "Claude 구독이 없으면 Anthropic API 키(ANTHROPIC_API_KEY)를 써요. Anthropic에 확인한 뒤에만 저장해요."}</p>
          {secretForm(tool === "agy" ? "Gemini API 키" : "Anthropic API 키",
            tool === "agy" ? "키 붙여넣기" : "sk-ant-…", "apikey", "key")}
        </div>
      )}
      {err && <p role="alert" className={errBox}>{err}</p>}
    </div>
  );
}

/** Shared by 에이전트 settings and 시작하기. */
export function Accounts({ a, error, onRefresh, children }: { a: AgentAccounts | null; error: string; onRefresh: () => void; children?: React.ReactNode }) {
  return (
    <Section title="활성 계정" desc="이 컴퓨터의 Claude, Codex, Antigravity를 확인하고 로그인해요. 쓰는 것 하나만 로그인해도 돼요. 로그인하지 않은 도구는 아래 카드에서 고를 수 없어요."
      aside={<Button size="sm" variant="secondary" onClick={onRefresh}><RefreshCw className="h-4 w-4" /> 다시 확인</Button>}>
      {!a && error ? (
        <Block><p className="text-sm text-text-muted">확인하지 못했어요: {error}</p></Block>
      ) : !a ? (
        // the rows' frame while checking, so the cards below do not move when the answer arrives (TP-174)
        ["claude", "codex", "agy"].map((tool) => (
          <Row key={tool} group label={tool === "agy" ? "Antigravity (agy)" : tool} desc={"\u00a0"}>
            <div className="space-y-1 text-sm">
              <Badge>확인하는 중…</Badge>
              <p aria-hidden>&nbsp;</p>
            </div>
          </Row>
        ))
      ) : Object.entries(a.tools).map(([tool, t]) => (
        <Row key={tool} group label={tool === "agy" ? "Antigravity (agy)" : tool} desc={t.version ?? undefined}>
          <div className="space-y-2 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <Badge status={t.logged_in ? "success" : t.installed ? "warning" : "danger"}>
                {t.logged_in ? "로그인됨" : t.installed ? "로그인 안 됨" : "설치 안 됨"}
              </Badge>
              {t.method && <span className="text-xs text-text-muted">{METHOD[t.method]}</span>}
            </div>
            {t.logged_in && t.account.length > 0 && <p className="text-text-main">{t.account.join(" · ")}</p>}
            {!t.installed && <p className="text-text-muted">PATH에서 {tool} 명령을 찾지 못했어요{a.docker && tool !== "agy" ? " (Docker 이미지를 다시 빌드해요)" : ""}.</p>}
            {t.neon_reason && <p className="text-text-muted">{t.neon_reason}</p>}
            {!t.installed && tool === "agy" && <a href="https://antigravity.google/docs/getting-started/"
              target="_blank" rel="noreferrer" className="font-semibold text-primary-700 underline">Antigravity CLI 설치 안내</a>}
            {t.installed && (a.admin ? <ToolLogin tool={tool} t={t} onChanged={onRefresh} />
              : <p className="text-xs text-text-muted">로그인은 이 컴퓨터(127.0.0.1){a.docker ? "나 TP_ADMINS의 사람" : ""}만 할 수 있어요.</p>)}
          </div>
        </Row>
      ))}
      {children}
    </Section>
  );
}
