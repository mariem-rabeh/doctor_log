import { type FormEvent, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import Card from "../components/Card";
import { api } from "../services/api";
import type { Dataset } from "../types";
import { errorMessage } from "../utils";

export default function NewInvestigation() {
  const navigate = useNavigate();
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [title, setTitle] = useState("");
  const [dataset, setDataset] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listDatasets()
      .then((list) => {
        setDatasets(list);
        setDataset(list[0]?.name ?? "");
      })
      .catch((e) => setError(errorMessage(e)));
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const incident = await api.createIncident({ title, dataset_name: dataset });
      await api.analyze(incident.id);
      navigate(`/incidents/${incident.id}`);
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }

  return (
    <div className="max-w-xl space-y-6">
      <h1 className="text-2xl font-semibold">Nouvelle investigation</h1>
      <Card>
        <form onSubmit={submit} className="space-y-4">
          <label className="block text-sm">
            Titre
            <input
              required
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Latence élevée sur /orders"
              className="mt-1 w-full rounded border border-slate-300 px-3 py-2"
            />
          </label>
          <label className="block text-sm">
            Dataset
            <select
              required
              value={dataset}
              onChange={(e) => setDataset(e.target.value)}
              className="mt-1 w-full rounded border border-slate-300 px-3 py-2"
            >
              {datasets.map((d) => (
                <option key={d.name} value={d.name}>
                  {d.name}
                </option>
              ))}
            </select>
          </label>
          {error && <p className="text-sm text-red-700">{error}</p>}
          <button
            disabled={busy || !dataset}
            className="rounded bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-50"
          >
            {busy ? "Lancement..." : "Créer et lancer l'analyse"}
          </button>
        </form>
      </Card>
    </div>
  );
}
