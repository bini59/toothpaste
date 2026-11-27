import { useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { Button, Input, cn } from "../../ui";
import { ConfigError, postConfig } from "../../api";
import { ConfirmButton } from "../../components/ConfirmButton";
import { Select } from "../../components/Select";
import { Table } from "../../components/Table";
import type { ConfigState, ProjectConf, ProjectSource } from "../../types";
import { emptyNotion, notionError, NotionSource } from "../NotionSource";
import { Errors } from "./ConfigFeedback";
import { Block, Check, Key, Row, Section, inputCls, selectCls } from "./Form";
import { History } from "./History";
import { ID_RE, SOURCE_LABEL, slug } from "./projectFields";
import { SaveBar } from "./SaveBar";
import { ConfigLoading, useConfig, useConfigDraft } from "./useConfig";
import { same, text } from "./values";

const EMPTY: ProjectConf = { name: "", source: "none", asana_gid: null, repos: [] };

function ProjectEditor({ id, c, onReload, onSaved, onDeleted }: {
  id: string | null; c: ConfigState; onReload: () => void; onSaved?: (id: string) => void; onDeleted?: () => void;
}) {
  const orig = id ? c.config.project[id] : EMPTY;
  const { draft: d, edit: set, reset } = useConfigDraft(orig);
  const [newId, setNewId] = useState("");
  const [add, setAdd] = useState("");
  const [delErr, setDelErr] = useState("");
  const pid = id ?? (newId || slug(d.name));
  const idErr = id ? "" : !pid ? "id를 정해 주세요 (영문 소문자, 숫자, -)" : !ID_RE.test(pid) ? "영문 소문자, 숫자, - 로 40자 이내" : pid in c.config.project || pid === "unsorted" ? "이미 있는 id예요" : "";
  const saveError = idErr ? `id: ${idErr}` : d.source === "notion" ? notionError(d.notion ?? emptyNotion) : "";
  // Asana projects another tp project already links are not offered again
  const usedGids = new Set(Object.entries(c.config.project).filter(([k]) => k !== id).map(([, p]) => p.asana_gid).filter(Boolean));
  const free = Object.keys(c.config.repo).filter((n) => !d.repos.some((r) => r.repo === n));
  // before TP-91 a project's history was recorded under its Asana gid
  const hist = id ? c.history.filter((h) => h.scope === `project:${id}` || (!!orig.asana_gid && h.scope === `project:${orig.asana_gid}`)) : [];
  const remove = async () => {
    try { await postConfig("/api/config", { scope: `project.${id}`, value: null }); onDeleted?.(); onReload(); } catch (e) { setDelErr(e instanceof ConfigError ? e.errors.join("; ") : String(e)); }
  };
  return (
    <div className="space-y-4">
      <Section title={id ? "프로젝트 정보" : "새 프로젝트"}>
        <Row label="이름">
          <Input className={inputCls} value={d.name} onChange={(e) => set((x) => void (x.name = e.target.value))} />
        </Row>
        <Row label="id" desc={id ? "주소(#projects/<id>)에 쓰여요. 만든 뒤에는 바꿀 수 없어요" : idErr ? <span className="text-danger-700">{idErr}</span> : "주소(#projects/<id>)에 쓰여요"}>
          <Input className={cn(inputCls, "font-mono")} value={pid} disabled={!!id} onChange={(e) => setNewId(e.target.value)} />
        </Row>
        {id && (
          <Row group label="프로젝트 삭제" desc="레포와 레포별 설정, 티켓은 그대로예요. 다른 프로젝트에 없는 레포는 미분류로 가요.">
            {delErr && <div className="mb-2"><Errors errors={[delErr]} /></div>}
            <ConfirmButton size="sm" variant="secondary" className="text-danger-700" confirmLabel="삭제" onConfirm={remove}
              message="이 프로젝트를 지울까요? 레포와 레포별 설정, 티켓은 그대로예요. 다른 프로젝트에 없는 레포는 미분류로 가요.">
              <Trash2 className="h-4 w-4" /> 프로젝트 삭제
            </ConfirmButton>
          </Row>
        )}
      </Section>
      <Section title="레포" desc="한 레포를 여러 프로젝트에 넣을 수 있어요. GitHub에서 새 레포를 받아 등록하려면 위의 새 레포 등록을 눌러요.">
        <Block>
          {d.repos.length ? (
            <Table head={["레포", "role", "기본 레포", ""]}>
              {d.repos.map((r, i) => (
                <tr key={r.repo}>
                  <td className="font-mono text-xs">{r.repo}</td>
                  <td>
                    <Input aria-label="role" className={cn(inputCls, "w-28")} value={text(r.role)} onChange={(e) => set((x) => void (x.repos[i].role = e.target.value))} />
                  </td>
                  <td>
                    {/* one default per project: the chat runs in its checkout, the others are opened with it */}
                    <Check label="기본" checked={!!r.default} onChange={(v) => set((x) => x.repos.forEach((rr, j) => { if (j === i && v) rr.default = true; else delete rr.default; }))} />
                  </td>
                  <td className="text-right">
                    <Button size="sm" variant="secondary" aria-label={`${r.repo} 빼기`} onClick={() => set((x) => void x.repos.splice(i, 1))}>
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </td>
                </tr>
              ))}
            </Table>
          ) : (
            <p className="text-sm text-text-muted">아직 레포가 없어요.</p>
          )}
        </Block>
        <Row group label="레포 추가" desc="처음 넣는 레포가 기본 레포가 돼요">
          <div className="flex items-center gap-2">
            <Select className="h-9 min-w-0 flex-1 sm:max-w-72" value={add} onChange={(e) => setAdd(e.target.value)} aria-label="추가할 레포">
              <option value="">레포 고르기…</option>
              {free.map((n) => <option key={n} value={n}>{n}</option>)}
            </Select>
            <Button size="sm" variant="secondary" disabled={!add}
              onClick={() => { set((x) => void x.repos.push({ repo: add, role: "app", ...(x.repos.some((r) => r.default) ? {} : { default: true }) })); setAdd(""); }}>
              <Plus className="h-4 w-4" /> 추가
            </Button>
          </div>
        </Row>
      </Section>
      <Section title="티켓 설정">
        <Row label="티켓 출처">
          <Select className={selectCls} value={d.source} onChange={(e) => set((x) => {
            x.source = e.target.value as ProjectSource;
            if (x.source !== "asana") x.asana_gid = null;
            if (x.source !== "notion") delete x.notion;
          })}>
            {Object.entries(SOURCE_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
          </Select>
        </Row>
        {d.source === "asana" && (
          <Row label="Asana 프로젝트">
            <Select className={selectCls} value={d.asana_gid ?? ""} onChange={(e) => set((x) => void (x.asana_gid = e.target.value || null))}>
              <option value="">고르세요…</option>
              {Object.entries(c.projects).filter(([g]) => g === d.asana_gid || !usedGids.has(g)).map(([g, n]) => <option key={g} value={g}>{n} ({g})</option>)}
            </Select>
          </Row>
        )}
        {d.source === "github" && <Block>
          <p className="text-sm text-text-muted">위에서 연결한 레포의 열린 이슈 중 <Key k="tp" /> 라벨이 붙은 이슈를 가져와요.</p>
        </Block>}
        {d.source === "notion" && <NotionSource projectId={pid} value={d.notion ?? emptyNotion} repos={d.repos.map((r) => r.repo)}
          workTag={c.config.global.asana_tag} onChange={(v) => set((x) => { x.notion = v; })} />}
      </Section>
      {id && (
        <Section title="이력">
          <Block><History rows={hist} /></Block>
        </Section>
      )}
      <SaveBar dirty={!id || !same(d, orig)} onReset={reset} onSaved={() => { onReload(); onSaved?.(pid); }}
        run={(dry) => (saveError ? Promise.reject(new ConfigError(saveError, [saveError])) : postConfig("/api/config", { scope: `project.${pid}`, value: d, dry_run: dry, create: !id }))} />
    </div>
  );
}

/** 프로젝트 > <id> > 설정 (project info); id null = the 새 프로젝트 form */
export function ProjectSettings({ id, onChanged, onSaved, onDeleted }: { id: string | null; onChanged: () => void; onSaved?: (id: string) => void; onDeleted?: () => void }) {
  const { c, error, load } = useConfig();
  if (!c) return <ConfigLoading error={error} />;
  if (id && !c.config.project[id]) return <p className="text-sm text-text-muted">설정에 없는 프로젝트예요: {id}</p>;
  return <ProjectEditor key={id ?? ""} id={id} c={c} onReload={() => { load(); onChanged(); }} onSaved={onSaved} onDeleted={onDeleted} />;
}
