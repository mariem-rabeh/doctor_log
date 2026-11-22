import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import Card from "../components/Card";
import IncidentTable from "../components/IncidentTable";
import Spinner from "../components/Spinner";
import { api } from "../services/api";
import type { Incident, IncidentStatus } from "../types";
import { errorMessage } from "../utils";

const STATUSES: [IncidentStatus, string][] = [
  ["pending", "En attente"],
  ["running", "En cours"],
  ["completed", "Terminés"],
  ["failed", "En erreur"],
];

export default function Dashboard() {
  const [counts, setCounts] = useState<Record<string, number> | null>(null);
  const [latest, setLatest] = useState<Incident[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Un appel par statut avec limit=1 : on ne lit que le total.
    Promise.all(STATUSES.map(([status]) => api.listIncidents({ status, limit: 1 })))
      .then((pages) => setCounts(Object.fromEntries(pages.map((p, i) => [STATUSES[i][0], p.total]))))
      .catch((e) => setError(errorMessage(e)));
    api
      .listIncidents({ limit: 5 })
      .then((page) => setLatest(page.items))
      .catch((e) => setError(errorMessage(e)));
  }, []);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Tableau de bord</h1>
        <Link to="/new" className="rounded bg-slate-900 px-4 py-2 text-sm text-white">
          Nouvelle investigation
        </Link>
      </div>
      {error && <p className="text-sm text-red-700">{error}</p>}
      {!counts && !error ? (
        <Spinner />
      ) : (
        <div className="grid grid-cols-4 gap-4">
          {STATUSES.map(([status, label]) => (
            <Card key={status}>
              <p className="text-sm text-slate-500">{label}</p>
              <p className="text-3xl font-semibold">{counts?.[status] ?? "–"}</p>
            </Card>
          ))}
        </div>
      )}
      <Card title="Derniers incidents">
        <IncidentTable incidents={latest} />
      </Card>
    </div>
  );
}
