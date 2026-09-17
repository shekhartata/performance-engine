export type SessionInfo = {
  id: string;
  uri_redacted: string;
  databases: string[];
};

export type RunProgress = {
  phase?: string;
  model?: string;
  documents?: number;
  concurrency?: number;
  selectivity?: number;
  cache_state?: string;
  database?: string;
  round?: number;
};

export type EnvelopeRow = {
  dataset_size?: number;
  concurrency?: number;
  cache_state?: string;
  p95_ms?: number;
  class?: string;
};

export type ScaleCurve = {
  dataset_size: number;
  mean_p95_ms?: number;
  vs_concurrency?: { concurrency: number; p95_ms: number }[];
};

export type Analysis = {
  worst_class?: string;
  envelope?: EnvelopeRow[];
  per_scale?: ScaleCurve[];
  slo_boundary?: {
    kind?: string;
    safe_region_documents?: number | null;
    estimated_breakpoint_documents?: number | null;
    note?: string;
  };
  projection?: { dataset_size: number; predicted_p95_ms: number; extrapolated?: boolean }[];
  validation?: { mape?: number };
  models?: string[];
};

export type RunStatus = {
  id: string;
  status: "queued" | "running" | "complete" | "failed" | string;
  database: string;
  collection: string;
  progress: RunProgress | null;
  error: string | null;
  analysis: Analysis | null;
  observations: Record<string, unknown>[] | null;
};

export type AdvancedPayload = {
  mode: "existing" | "synthetic" | "external";
  documents?: string;
  document_size?: string;
  concurrency?: string;
  selectivity?: string;
  cache_state?: string;
  scales?: string | Record<string, string>;
  slo_p95: number;
  duration_seconds: number;
  repetitions: number;
  generator_command?: string;
  generator_working_dir?: string;
  allow_external_writes?: boolean;
  synthetic_seed?: number;
  synthetic_fields?: Record<string, unknown>;
  second_query?: unknown;
  second_model?: string;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { credentials: "include", ...init, headers });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = await response.json();
      const raw = payload.detail;
      if (typeof raw === "string") detail = raw;
      else if (Array.isArray(raw)) detail = raw.map((item) => item.msg || JSON.stringify(item)).join("; ");
    } catch {
      /* keep status text */
    }
    throw new Error(detail);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function connect(uri: string) {
  return request<SessionInfo>("/api/session", { method: "POST", body: JSON.stringify({ uri }) });
}

export function loadSession() {
  return request<SessionInfo>("/api/session");
}

export function disconnect() {
  return request<{ status: string }>("/api/session", { method: "DELETE" });
}

export function loadCollections(database: string) {
  return request<{ collections: string[] }>(`/api/databases/${encodeURIComponent(database)}/collections`);
}

export function startRun(body: {
  database: string;
  collection: string;
  query: unknown;
  ack_non_production: boolean;
  advanced?: AdvancedPayload;
}) {
  return request<RunStatus>("/api/runs", { method: "POST", body: JSON.stringify(body) });
}

export function getRun(id: string) {
  return request<RunStatus>(`/api/runs/${encodeURIComponent(id)}`);
}

export function reportUrl(id: string, format: "html" | "json" | "md") {
  return `/api/runs/${encodeURIComponent(id)}/report?format=${format}`;
}
