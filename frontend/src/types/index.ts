// Miroir des schémas Pydantic du backend (app/schemas et app/api). À garder synchronisé.

export type IncidentStatus = "pending" | "running" | "completed" | "failed";
export type AgentStatus = "pending" | "running" | "completed" | "failed";
export type AgentName = "log" | "metrics" | "trace" | "rca" | "verification";
export type Verdict = "accepted" | "revision_requested" | "insufficient_evidence";
export type Conclusion = "confirmed" | "probable" | "insufficient_evidence";
export type SignalSource = "logs" | "metrics" | "traces";

export interface Evidence {
  id: string;
  source: SignalSource;
  service: string;
  period_start: string;
  period_end: string;
  observation: string;
  value: number | string;
  unit: string | null;
}

export interface Hypothesis {
  cause: string;
  rank: number;
  justification: string;
  supporting_evidence_ids: string[];
  contradicting_evidence_ids: string[];
  verification_actions: string[];
}

export interface VerificationResult {
  verdict: Verdict;
  confidence: number;
  issues: string[];
  to_verify: string[];
}

export interface DiagnosisReport {
  incident_id: string;
  conclusion: Conclusion;
  probable_cause: string | null;
  confidence: number;
  hypotheses: Hypothesis[];
  evidences: Evidence[];
  verification: VerificationResult | null;
  recommendations: string[];
  missing_data: string[];
  iteration_count: number;
  generated_at: string;
}

export interface Incident {
  id: string;
  title: string;
  source: "dataset" | "live";
  dataset_name: string | null;
  window_start: string;
  window_end: string;
  status: IncidentStatus;
  agent_statuses: Partial<Record<AgentName, AgentStatus>>;
  error: string | null;
  has_report: boolean;
  created_at: string;
  updated_at: string;
}

export interface IncidentStatusInfo {
  id: string;
  status: IncidentStatus;
  agent_statuses: Partial<Record<AgentName, AgentStatus>>;
  error: string | null;
  has_report: boolean;
}

export interface IncidentPage {
  items: Incident[];
  total: number;
  limit: number;
  offset: number;
}

export interface Dataset {
  name: string;
  window_start: string;
  window_end: string;
}

export interface IncidentCreate {
  title: string;
  source?: "dataset" | "live";
  dataset_name?: string;
  window_start?: string;
  window_end?: string;
}
