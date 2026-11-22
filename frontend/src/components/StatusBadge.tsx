import type { AgentStatus, IncidentStatus } from "../types";

const styles: Record<IncidentStatus | AgentStatus, [string, string]> = {
  pending: ["En attente", "bg-slate-100 text-slate-700"],
  running: ["En cours", "bg-blue-100 text-blue-800"],
  completed: ["Terminé", "bg-green-100 text-green-800"],
  failed: ["Erreur", "bg-red-100 text-red-800"],
};

export default function StatusBadge({ status }: { status: IncidentStatus | AgentStatus }) {
  const [label, className] = styles[status];
  return <span className={`rounded px-2 py-0.5 text-xs font-medium ${className}`}>{label}</span>;
}
