import { useEffect, useState } from "react";
import Card from "../components/Card";
import IncidentTable from "../components/IncidentTable";
import Spinner from "../components/Spinner";
import { api } from "../services/api";
import type { IncidentPage, IncidentStatus } from "../types";
import { errorMessage } from "../utils";

const PAGE_SIZE = 10;

export default function History() {
  const [status, setStatus] = useState<IncidentStatus | "">("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<IncidentPage | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setPage(null);
    api
      .listIncidents({ status: status || undefined, limit: PAGE_SIZE, offset })
      .then(setPage)
      .catch((e) => setError(errorMessage(e)));
  }, [status, offset]);

  // La recherche par titre filtre la page affichée (l'API ne propose pas encore ce filtre).
  const items = (page?.items ?? []).filter((i) =>
    i.title.toLowerCase().includes(search.trim().toLowerCase()),
  );
  const total = page?.total ?? 0;

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Historique</h1>
      <div className="flex gap-3">
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Rechercher par titre"
          className="w-64 rounded border border-slate-300 px-3 py-2 text-sm"
        />
        <select
          value={status}
          onChange={(e) => {
            setStatus(e.target.value as IncidentStatus | "");
            setOffset(0);
          }}
          className="rounded border border-slate-300 px-3 py-2 text-sm"
        >
          <option value="">Tous les statuts</option>
          <option value="pending">En attente</option>
          <option value="running">En cours</option>
          <option value="completed">Terminés</option>
          <option value="failed">En erreur</option>
        </select>
      </div>
      {error && <p className="text-sm text-red-700">{error}</p>}
      <Card>
        {page ? <IncidentTable incidents={items} /> : !error && <Spinner />}
        <div className="mt-4 flex items-center justify-between text-sm">
          <span className="text-slate-500">
            {total === 0 ? "0" : `${offset + 1}–${Math.min(offset + PAGE_SIZE, total)}`} sur {total}
          </span>
          <div className="flex gap-2">
            <button
              disabled={offset === 0}
              onClick={() => setOffset(offset - PAGE_SIZE)}
              className="rounded border px-3 py-1 disabled:opacity-40"
            >
              Précédent
            </button>
            <button
              disabled={offset + PAGE_SIZE >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
              className="rounded border px-3 py-1 disabled:opacity-40"
            >
              Suivant
            </button>
          </div>
        </div>
      </Card>
    </div>
  );
}
