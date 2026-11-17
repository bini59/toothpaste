import { useState } from "react";
import { Button, Input } from "../ui";
import { Select } from "../components/Select";
import { StageBadge } from "../components/StageBadge";
import { Table } from "../components/Table";
import { STAGES, ago, byDue, inProject, isOverdue, latestPr, onBoard, openTickets, reposFor, ticketRepos, type Index } from "../lib";
import type { Project, State, Ticket } from "../types";

const isDone = (t: Ticket) => ["done", "closed"].includes(t.stage);
/** open first by due date, then finished ones, most recently finished first */
const byDoneThenDue = (a: Ticket, b: Ticket) =>
  +isDone(a) - +isDone(b) || (isDone(a) ? (b.done_at || "").localeCompare(a.done_at || "") : byDue(a, b));

/** #tickets: my open tickets. With project (프로젝트 전체 티켓, TP-81/TP-91): every ticket of that project, finished
 * ones included */
export function Tickets({ s, I, project }: { s: State; I?: Index; project?: Project }) {
  const [q, setQ] = useState("");
  const [stage, setStage] = useState("");
  const [proj, setProj] = useState("");
  const [withDone, setWithDone] = useState(true);
  const inc = project && I ? s.tickets.filter((t) => onBoard(t) && inProject(s, I, t, project)) : openTickets(s);
  const projs = [...new Set(inc.map((t) => t.project_name || "(없음)"))].sort();
  const rows = inc
    .filter(
      (t) =>
        (!q || `${t.key} ${t.name}`.toLowerCase().includes(q.toLowerCase())) &&
        (!stage || t.stage === stage) &&
        (!proj || (t.project_name || "(없음)") === proj) &&
        (!project || withDone || !isDone(t)),
    )
    .sort(project ? byDoneThenDue : byDue);
  const link = (href: string | null | undefined, text: string) =>
    href ? (
      <a href={href} target="_blank" rel="noreferrer" className="text-primary hover:text-primary-700 hover:underline">
        {text}
      </a>
    ) : (
      text
    );
  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Input type="search" placeholder="키 / 제목 검색" value={q} onChange={(e) => setQ(e.target.value)} className="w-60" />
        <Select value={stage} onChange={(e) => setStage(e.target.value)}>
          <option value="">모든 단계</option>
          {Object.entries(STAGES).map(([k, [l]]) => (
            <option key={k} value={k}>
              {l}
            </option>
          ))}
        </Select>
        {project ? (
          <Button size="sm" variant="secondary" aria-pressed={withDone} onClick={() => setWithDone(!withDone)}>
            완료 포함
          </Button>
        ) : (
          <Select value={proj} onChange={(e) => setProj(e.target.value)}>
            <option value="">모든 프로젝트</option>
            {projs.map((p) => (
              <option key={p}>{p}</option>
            ))}
          </Select>
        )}
        <span className="text-sm text-text-muted">{rows.length}개</span>
      </div>
      {project && I ? (
        <Table head={["키", "제목", "레포", "단계", "기한", "PR", "완료일", "최근 변경"]}>
          {rows.map((t) => {
            const pr = latestPr(s, t.gid);
            const changed = [
              t.asana_modified_at,
              t.done_at,
              ...(I.runBy[t.gid] ?? []).map((r) => r.updated_at),
              ...(I.prBy[t.gid] ?? []).map((p) => p.updated_at),
            ]
              .filter((v): v is string => !!v)
              .sort((a, b) => new Date(b).getTime() - new Date(a).getTime())[0];
            return (
              <tr key={t.gid}>
                <td className="whitespace-nowrap">{link(t.url, t.key)}</td>
                <td className="text-text-main">{t.name}</td>
                <td className="whitespace-nowrap text-text-muted">{ticketRepos(s, I, t).join(", ") || "-"}</td>
                <td>
                  <StageBadge stage={t.stage} />
                </td>
                <td className={`whitespace-nowrap ${!isDone(t) && isOverdue(t.due_on) ? "font-medium text-danger-700" : ""}`}>
                  {t.due_on || "-"}
                </td>
                <td className="whitespace-nowrap">{pr ? link(pr.url, `#${pr.number}`) : "-"}</td>
                <td className="whitespace-nowrap text-text-muted">{t.done_at ? t.done_at.slice(0, 10) : "-"}</td>
                <td className="whitespace-nowrap text-text-muted">{ago(changed)}</td>
              </tr>
            );
          })}
        </Table>
      ) : (
        <Table head={["키", "제목", "프로젝트 / 섹션", "상위", "기한", "단계", "매핑 레포"]}>
          {rows.map((t) => (
            <tr key={t.gid}>
              <td className="whitespace-nowrap">
                <a href={t.url} target="_blank" rel="noreferrer" className="text-primary hover:text-primary-700 hover:underline">
                  {t.key}
                </a>
              </td>
              <td className="text-text-main">{t.name}</td>
              <td>
                {t.project_name || "-"}
                <div className="text-xs text-text-muted">{t.section}</div>
              </td>
              <td className="text-text-muted">{t.parent_name}</td>
              <td className={`whitespace-nowrap ${isOverdue(t.due_on) ? "font-medium text-danger-700" : ""}`}>{t.due_on || "-"}</td>
              <td>
                <StageBadge stage={t.stage} />
              </td>
              <td className="text-text-muted">{t.repo_override || reposFor(s, t.project_gid).map((r) => r.repo).join(", ") || "-"}</td>
            </tr>
          ))}
        </Table>
      )}
    </>
  );
}
