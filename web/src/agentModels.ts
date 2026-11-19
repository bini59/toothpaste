import type { AgentAccounts, ConfigState } from "./types";

export function modelsForTool(tool: string, catalog?: ConfigState["agent_models"], accounts?: AgentAccounts | null) {
  return (tool === "agy" ? accounts?.tools.agy?.models : undefined) ?? catalog?.[tool] ?? [];
}
