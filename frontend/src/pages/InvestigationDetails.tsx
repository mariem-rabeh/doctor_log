import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import Card from "../components/Card";
import Spinner from "../components/Spinner";
import StatusBadge from "../components/StatusBadge";
import { api } from "../services/api";
import type { AgentName, Incident, IncidentStatusInfo } from "../types";
import { errorMessage, formatDate } from "../utils";

const AGENTS: [AgentName, string][] = [
  ["log", "Log Agent"],
  ["metrics", "Metrics Agent"],
  ["trace", "Trace Agent"],
  ["rca", "Root Cause Analysis"],
  ["verification", "Vérification"],
];
const POLL_MS = 2000;

export default function InvestigationDetails() {
  const { id = "" } = useParams();
  const [incident, setIncident] = useState<Incident | null>(null);
  const [status, setStatus] = useState<IncidentStatusInfo | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.getIncident(id).then(setIncident).catch((e) => setError(errorMessage(e)));
  }, [id]);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    const poll = () =>
      api
        .getStatus(id)
        .then((info) => {
          setStatus(info);
          // On arrête de demander dès que l'analyse est finie (ou en échec).
          if (info.status === "running" || info.status === "pending") timer = setTimeout(poll, POLL_MS);
        })
        .catch((e) => setError(errorMessage(e)));
    poll();
    return () => clearTimeout(timer);
  }, [id]);

  return (
    <div className="max-w-3xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">{incident?.title ?? "Investigation"}</h1>
        {incident && (
          <p className="text-sm text-slate-500">
            {incident.dataset_name ?? "live"} · fenêtre {formatDate(incident.window_start)} →{" "}
            {formatDate(incident.window_end)}
          </p>
        )}
      </div>
      {error && <p className="text-sm text-red-700">{error}</p>}
      {!status ? (
        !error && <Spinner />
      ) : (
        <>
          <Card title="Analyse">
            <div className="mb-4 flex items-center gap-3">
              <StatusBadge status={status.status} />
              {status.status === "running" && <Spinner label="Analyse en cours..." />}
            </div>
            <ul className="divide-y divide-slate-100">
              {AGENTS.map(([name, label]) => (
                <li key={name} className="flex items-center justify-between py-2 text-sm">
                  {label}
                  <StatusBadge status={status.agent_statuses[name] ?? "pending"} />
                </li>
              ))}
            </ul>
          </Card>
          {status.status === "failed" && (
            <Card title="Erreur">
              <p className="text-sm text-red-700">{status.error}</p>
            </Card>
          )}
          {status.has_report && (
            <Link
              to={`/incidents/${id}/report`}
              className="inline-block rounded bg-slate-900 px-4 py-2 text-sm text-white"
            >
              Voir le rapport de diagnostic
            </Link>
          )}
        </>
      )}
    </div>
  );
}
