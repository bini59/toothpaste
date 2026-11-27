import type { ProjectSource } from "../../types";

export const SOURCE_LABEL: Record<ProjectSource, string> = { asana: "Asana 프로젝트", github: "GitHub 이슈", notion: "Notion 데이터베이스", none: "없음" };
export const ID_RE = /^[a-z0-9][a-z0-9-]{0,39}$/;
export const slug = (name: string) => name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40).replace(/-+$/, "");
