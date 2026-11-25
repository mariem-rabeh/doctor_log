import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import Card from "../components/Card";
import Spinner from "../components/Spinner";
import { api } from "../services/api";
import type { Conclusion, DiagnosisReport as Report, Evidence, SignalSource } from "../types";
import { errorMessage, formatDate } from "../utils";

const VERDICTS: Record<Conclusion, [string, string]> = {
  confirmed: ["Cause confirmée", "bg-green-100 text-green-900 border-green-300"],
  probable: ["Hypothèse probable", "bg-amber-100 text-amber-900 border-amber-300"],
  insufficient_evidence: ["Preuves insuffisantes", "bg-slate-100 text-slate-800 border-slate-300"],
};
const SOURCES: [SignalSource, string][] = [
  ["logs", "Logs"],
  ["metrics", "Métriques"],
  ["traces", "Traces"],
];

function EvidenceRow({ evidence }: { evidence: Evidence }) {
  return (
    <li className="py-2 text-sm">
      <span className="mr-2 rounded bg-slate-100 px-1.5 font-mono text-xs">{evidence.id}</span>
      <span className="font-medium">{evidence.service}</span> — {evidence.observation}
      <div className="text-xs text-slate-500">
        valeur {String(evidence.value)} {evidence.unit ?? ""} · {formatDate(evidence.period_start)} →{" "}
        {formatDate(evidence.period_end)}
      </div>
    </li>
  );
}

function List({ items, empty }: { items: string[]; empty: string }) {
  if (items.length === 0) return <p className="text-sm text-slate-500">{empty}</p>;
  return (
    <ul className="list-disc space-y-1 pl-5 text-sm">
      {items.map((item) => (
        <li key={item}>{item}</li>
      ))}
    </ul>
  );
}

export default function DiagnosisReport() {
  const { id = "" } = useParams();
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.getReport(id).then(setReport).catch((e) => setError(errorMessage(e)));
  }, [id]);

  if (error) return <p className="text-sm text-red-700">{error}</p>;
  if (!report) return <Spinner />;

  const [label, style] = VERDICTS[report.conclusion];
  const [top, ...others] = report.hypotheses;

  return (
    <div className="max-w-4xl space-y-6">
      <h1 className="text-2xl font-semibold">Rapport de diagnostic</h1>

      <section className={`rounded-lg border-2 p-5 ${style}`}>
        <p className="text-sm font-semibold uppercase tracking-wide">{label}</p>
        <p className="mt-1 text-lg">{report.probable_cause ?? "Aucune cause ne peut être affirmée."}</p>
        <p className="mt-2 text-sm">
          Confiance : {Math.round(report.confidence * 100)} % · {report.iteration_count} itération(s)
        </p>
      </section>

      {top && (
        <Card title="Justification">
          <p className="text-sm">{top.justification}</p>
        </Card>
      )}

      {others.length > 0 && (
        <Card title="Hypothèses concurrentes">
          <ol className="space-y-2 text-sm">
            {others.map((h) => (
              <li key={h.rank}>
                <span className="font-medium">#{h.rank}</span> {h.cause}
                <span className="text-slate-500"> — preuves : {h.supporting_evidence_ids.join(", ")}</span>
              </li>
            ))}
          </ol>
        </Card>
      )}

      <Card title="Preuves">
        {SOURCES.map(([source, title]) => {
          const items = report.evidences.filter((e) => e.source === source);
          return (
            <div key={source} className="mb-4">
              <h3 className="text-sm font-semibold text-slate-600">
                {title} ({items.length})
              </h3>
              <ul className="divide-y divide-slate-100">
                {items.map((e) => (
                  <EvidenceRow key={e.id} evidence={e} />
                ))}
              </ul>
            </div>
          );
        })}
      </Card>

      <div className="grid grid-cols-2 gap-4">
        <Card title="Actions de vérification">
          <List items={report.verification?.to_verify ?? []} empty="Aucune." />
        </Card>
        <Card title="Recommandations">
          <List items={report.recommendations} empty="Aucune." />
        </Card>
      </div>

      {report.documents.length > 0 && (
        <Card title="Documents consultés (base de connaissances)">
          <ul className="space-y-1 text-sm">
            {report.documents.map((d) => (
              <li key={d.id}>
                <span className="mr-2 rounded bg-slate-100 px-1.5 font-mono text-xs">{d.id}</span>
                {d.title} <span className="text-slate-500">· knowledge/{d.path}</span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-slate-500">
            Des connaissances, pas des preuves : elles ne comptent pas dans la confiance.
          </p>
        </Card>
      )}

      {(report.missing_data.length > 0 || (report.verification?.issues.length ?? 0) > 0) && (
        <Card title="Limites de l'analyse">
          <List
            items={[...report.missing_data, ...(report.verification?.issues ?? [])]}
            empty=""
          />
        </Card>
      )}
    </div>
  );
}
