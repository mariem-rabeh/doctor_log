// Client typé de l'API. Toutes les erreurs du backend ont la forme {error: {code, message, details}}.

import type { Dataset, Incident, IncidentCreate, IncidentPage, IncidentStatus } from "../types";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details: unknown = null,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      headers: { "Content-Type": "application/json" },
      ...init,
    });
  } catch {
    throw new ApiError(0, "network_error", "Serveur injoignable");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const error = body?.error;
    throw new ApiError(
      response.status,
      error?.code ?? "error",
      error?.message ?? `Erreur ${response.status}`,
      error?.details,
    );
  }
  return (await response.json()) as T;
}

export const api = {
  listDatasets: () => request<Dataset[]>("/datasets"),
  listIncidents: (params: { status?: IncidentStatus; limit?: number; offset?: number } = {}) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined) query.set(key, String(value));
    }
    return request<IncidentPage>(`/incidents?${query}`);
  },
  getIncident: (id: string) => request<Incident>(`/incidents/${id}`),
  createIncident: (body: IncidentCreate) =>
    request<Incident>("/incidents", { method: "POST", body: JSON.stringify(body) }),
};
