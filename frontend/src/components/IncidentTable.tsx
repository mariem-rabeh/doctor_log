import { Link } from "react-router-dom";
import type { Incident } from "../types";
import { formatDate } from "../utils";
import StatusBadge from "./StatusBadge";

export default function IncidentTable({ incidents }: { incidents: Incident[] }) {
  if (incidents.length === 0) return <p className="text-sm text-slate-500">Aucun incident.</p>;
  return (
    <table className="w-full text-left text-sm">
      <thead className="text-slate-500">
        <tr>
          <th className="py-2">Titre</th>
          <th>Dataset</th>
          <th>Statut</th>
          <th>Créé le</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {incidents.map((incident) => (
          <tr key={incident.id} className="border-t border-slate-100">
            <td className="py-2">
              <Link className="font-medium hover:underline" to={`/incidents/${incident.id}`}>
                {incident.title}
              </Link>
            </td>
            <td>{incident.dataset_name ?? "live"}</td>
            <td>
              <StatusBadge status={incident.status} />
            </td>
            <td>{formatDate(incident.created_at)}</td>
            <td className="text-right">
              {incident.has_report && (
                <Link
                  className="text-blue-700 hover:underline"
                  to={`/incidents/${incident.id}/report`}
                >
                  Rapport
                </Link>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
