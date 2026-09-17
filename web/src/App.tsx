import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  AdvancedPayload,
  Analysis,
  RunStatus,
  SessionInfo,
  connect,
  disconnect,
  getRun,
  loadCollections,
  loadSession,
  reportUrl,
  startRun,
} from "./api";

const DEFAULT_ADVANCED = {
  mode: "existing" as const,
  documents: "",
  document_size: "",
  concurrency: "1, 8",
  selectivity: "0.01, 0.1",
  cache_state: "hot, cold",
  scales: `{
  "10000": "mi_transformation",
  "50000": "mi_quotes_50000",
  "100000": "mi_quotes_100000"
}`,
  slo_p95: 100,
  duration_seconds: 12,
  repetitions: 2,
  generator_command: "",
  generator_working_dir: "",
  allow_external_writes: false,
  synthetic_seed: 42,
  synthetic_fields: "",
  second_query: "",
  second_model: "alt",
};

const DEFAULT_SYNTHETIC_FIELDS = `{
  "group_id": { "type": "integer", "cardinality": 200, "distribution": "zipf", "alpha": 1.3 },
  "status": { "type": "categorical", "values": { "Quoted": 0.7, "Ineligible": 0.2, "Failed": 0.1 } },
  "created_at": { "type": "datetime", "distribution": "uniform", "start": "365d" },
  "amount": { "type": "float", "min": 1, "max": 10000 }
}`;

type AdvancedForm = typeof DEFAULT_ADVANCED;

function shortError(message: string): string {
  if (/failed to connect|server selection timeout|nodename nor servname|could not connect/i.test(message)) {
    return "Could not connect. Check the URI and that the cluster is reachable.";
  }
  if (message.length > 220) return `${message.slice(0, 217)}…`;
  return message;
}

function progressLabel(run: RunStatus | null): string {
  const progress = run?.progress;
  if (!run) return "";
  if (progress?.phase === "cell") {
    const docs = progress.documents != null ? `${Number(progress.documents).toLocaleString()} docs` : "";
    const db = progress.database ? String(progress.database) : "";
    const conc = progress.concurrency != null ? `c=${progress.concurrency}` : "";
    const sel = progress.selectivity != null ? `sel=${progress.selectivity}` : "";
    const cache = progress.cache_state ? String(progress.cache_state).replace("estimated_", "") : "";
    return [docs, db, conc, sel, cache].filter(Boolean).join(" · ");
  }
  if (run.status === "queued") return "Queued";
  if (run.status === "complete") return "Done";
  if (run.status === "failed") return "Failed";
  return progress?.phase ? progress.phase.replace(/_/g, " ") : "Running";
}

function parseJson(text: string, label: string): unknown {
  const trimmed = text.trim();
  if (!trimmed) throw new Error(`${label} is empty`);
  try {
    return JSON.parse(trimmed);
  } catch {
    throw new Error(`${label} is not valid JSON`);
  }
}

function buildAdvanced(form: AdvancedForm): AdvancedPayload {
  const payload: AdvancedPayload = {
    mode: form.mode,
    documents: form.documents.trim() || undefined,
    document_size: form.document_size.trim() || undefined,
    concurrency: form.concurrency.trim() || undefined,
    selectivity: form.selectivity.trim() || undefined,
    cache_state: form.cache_state.trim() || undefined,
    scales: form.mode === "existing" && form.scales.trim() ? form.scales.trim() : undefined,
    slo_p95: Number(form.slo_p95),
    duration_seconds: Number(form.duration_seconds),
    repetitions: Number(form.repetitions),
    generator_command: form.generator_command.trim() || undefined,
    generator_working_dir: form.generator_working_dir.trim() || undefined,
    allow_external_writes: form.allow_external_writes,
    synthetic_seed: Number(form.synthetic_seed),
    second_model: form.second_model.trim() || "alt",
  };
  if (form.synthetic_fields.trim()) {
    payload.synthetic_fields = parseJson(form.synthetic_fields, "Synthetic fields") as Record<string, unknown>;
  }
  if (form.second_query.trim()) {
    payload.second_query = parseJson(form.second_query, "Second query");
  }
  return payload;
}

export default function App() {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [uri, setUri] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [database, setDatabase] = useState("");
  const [collections, setCollections] = useState<string[]>([]);
  const [collection, setCollection] = useState("");
  const [query, setQuery] = useState("");
  const [ack, setAck] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [advanced, setAdvanced] = useState<AdvancedForm>(DEFAULT_ADVANCED);
  const [run, setRun] = useState<RunStatus | null>(null);
  const [error, setError] = useState("");
  const [booting, setBooting] = useState(true);

  const connected = Boolean(session);
  const running = run?.status === "queued" || run?.status === "running";

  useEffect(() => {
    loadSession()
      .then((info) => setSession(info))
      .catch(() => undefined)
      .finally(() => setBooting(false));
  }, []);

  useEffect(() => {
    if (!database || !session) {
      setCollections([]);
      setCollection("");
      return;
    }
    loadCollections(database)
      .then((payload) => {
        setCollections(payload.collections);
        setCollection((current) => (payload.collections.includes(current) ? current : ""));
      })
      .catch((err: Error) => setError(err.message));
  }, [database, session]);

  const runId = run?.id;
  const runStatus = run?.status;
  useEffect(() => {
    if (!runId || (runStatus !== "queued" && runStatus !== "running")) return;
    const timer = window.setInterval(() => {
      getRun(runId)
        .then(setRun)
        .catch((err: Error) => setError(err.message));
    }, 800);
    return () => window.clearInterval(timer);
  }, [runId, runStatus]);

  async function onConnect(event: FormEvent) {
    event.preventDefault();
    setError("");
    setConnecting(true);
    try {
      const info = await connect(uri.trim());
      setSession(info);
      setDatabase("");
      setCollection("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not connect");
    } finally {
      setConnecting(false);
    }
  }

  async function onDisconnect() {
    await disconnect().catch(() => undefined);
    setSession(null);
    setDatabase("");
    setCollections([]);
    setCollection("");
    setRun(null);
  }

  async function onRun(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const parsed = parseJson(query, "Query");
      const payload = buildAdvanced(advanced);
      const next = await startRun({
        database,
        collection,
        query: parsed,
        ack_non_production: ack,
        advanced: payload,
      });
      setRun(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start the run");
    }
  }

  const ready = connected && database && collection && query.trim() && ack && !running;

  return (
    <div className="page">
      <header className="mast">
        <p className="eyebrow">Performance Envelope Engine</p>
        <h1>How far does this query hold?</h1>
        <p className="lede">
          Paste an Atlas URI, pick a collection, drop in the query, and run. The engine measures p95
          against your SLO and draws the operating envelope.
        </p>
      </header>

      {error ? <div className="banner">{shortError(error)}</div> : null}

      <section className={`card ${booting ? "is-booting" : ""}`}>
        {session ? (
          <div className="session-bar">
            <span className="dot" />
            <code>{session.uri_redacted}</code>
            <button type="button" className="text-btn" onClick={onDisconnect}>
              Disconnect
            </button>
          </div>
        ) : (
          <form className="connect" onSubmit={onConnect}>
            <label htmlFor="uri">MongoDB URI</label>
            <div className="row">
              <input
                id="uri"
                type="password"
                autoComplete="off"
                placeholder="mongodb+srv://…"
                value={uri}
                onChange={(event) => setUri(event.target.value)}
                required
              />
              <button type="submit" disabled={connecting || !uri.trim()}>
                {connecting ? "Connecting…" : "Connect"}
              </button>
            </div>
          </form>
        )}

        <form className={`stack ${connected ? "is-open" : "is-closed"}`} onSubmit={onRun}>
          <div className="split">
            <label>
              Database
              <select
                value={database}
                onChange={(event) => setDatabase(event.target.value)}
                disabled={!connected}
              >
                <option value="">Select…</option>
                {(session?.databases || []).map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Collection
              <select
                value={collection}
                onChange={(event) => setCollection(event.target.value)}
                disabled={!connected || !database}
              >
                <option value="">Select…</option>
                {collections.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <label>
            Query
            <textarea
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              disabled={!connected}
              spellCheck={false}
              placeholder={`{\n  "filter": {\n    "status": "{{status}}",\n    "created_at": { "$gte": "{{start_date}}" }\n  },\n  "sort": { "created_at": -1 },\n  "limit": 50\n}`}
            />
          </label>
          <p className="hint">
            Prefer a parameterized find with <code>limit</code> (not a frozen id) so selectivity and
            result size can move. Placeholders like <code>{"{{status}}"}</code> and{" "}
            <code>{"{{start_date}}"}</code> are filled per cell.
          </p>

          <label className="ack">
            <input type="checkbox" checked={ack} onChange={(event) => setAck(event.target.checked)} />
            This is a non-production cluster. I understand the test will generate load.
          </label>

          <div className="actions">
            <button type="submit" className="run" disabled={!ready}>
              {running ? "Running…" : "Run test"}
            </button>
            <button
              type="button"
              className="ghost"
              onClick={() => setAdvancedOpen((open) => !open)}
            >
              {advancedOpen ? "Hide advanced" : "Advanced"}
            </button>
          </div>

          <div className={`advanced ${advancedOpen ? "is-open" : ""}`}>
            <AdvancedFields form={advanced} onChange={setAdvanced} />
          </div>
        </form>
      </section>

      {run ? <RunPanel run={run} slo={advanced.slo_p95} /> : null}
    </div>
  );
}

function AdvancedFields({
  form,
  onChange,
}: {
  form: AdvancedForm;
  onChange: (form: AdvancedForm) => void;
}) {
  function patch<K extends keyof AdvancedForm>(key: K, value: AdvancedForm[K]) {
    onChange({ ...form, [key]: value });
  }
  return (
    <div className="advanced-grid">
      <label>
        Dataset
        <select
          value={form.mode}
          onChange={(event) => {
            const mode = event.target.value as AdvancedForm["mode"];
            const next = { ...form, mode };
            if (mode === "synthetic") {
              if (!form.document_size.trim()) next.document_size = "512, 2048";
              if (!form.documents.trim()) next.documents = "1000, 5000";
              if (!form.synthetic_fields.trim()) next.synthetic_fields = DEFAULT_SYNTHETIC_FIELDS;
            }
            onChange(next);
          }}
        >
          <option value="existing">Existing collection</option>
          <option value="synthetic">Synthetic</option>
          <option value="external">External generator</option>
        </select>
      </label>
      <label>
        Documents
        <input
          value={form.documents}
          onChange={(event) => patch("documents", event.target.value)}
          placeholder={form.mode === "existing" ? "from scale map keys" : "10000, 50000, 100000"}
          disabled={form.mode === "existing" && !!form.scales.trim()}
        />
      </label>
      {form.mode === "existing" ? (
        <label className="wide">
          Scale map (size → database)
          <textarea
            value={form.scales}
            onChange={(event) => patch("scales", event.target.value)}
            placeholder='{"10000":"mi_transformation","50000":"mi_quotes_50000","100000":"mi_quotes_100000"}'
          />
          <span className="hint">
            Same collection name is read from each database. Clear to use a single existing collection.
          </span>
        </label>
      ) : null}
      <label>
        Document size (bytes)
        <input
          value={form.document_size}
          onChange={(event) => patch("document_size", event.target.value)}
          placeholder="512, 2048 (synthetic)"
          disabled={form.mode !== "synthetic"}
        />
      </label>
      <label>
        Selectivity
        <input value={form.selectivity} onChange={(event) => patch("selectivity", event.target.value)} placeholder="0.01, 0.1" />
      </label>
      <label>
        Concurrency
        <input value={form.concurrency} onChange={(event) => patch("concurrency", event.target.value)} />
      </label>
      <label>
        Cache state
        <input value={form.cache_state} onChange={(event) => patch("cache_state", event.target.value)} placeholder="hot, cold" />
      </label>
      <label>
        SLO p95 (ms)
        <input type="number" value={form.slo_p95} onChange={(event) => patch("slo_p95", Number(event.target.value))} />
      </label>
      <label>
        Duration (seconds)
        <input type="number" value={form.duration_seconds} onChange={(event) => patch("duration_seconds", Number(event.target.value))} />
      </label>
      <label>
        Repetitions
        <input type="number" value={form.repetitions} onChange={(event) => patch("repetitions", Number(event.target.value))} />
      </label>
      {form.mode === "synthetic" ? (
        <>
          <label>
            Seed
            <input type="number" value={form.synthetic_seed} onChange={(event) => patch("synthetic_seed", Number(event.target.value))} />
          </label>
          <label className="wide">
            Synthetic fields (JSON)
            <textarea
              value={form.synthetic_fields}
              onChange={(event) => patch("synthetic_fields", event.target.value)}
              placeholder={DEFAULT_SYNTHETIC_FIELDS}
            />
          </label>
        </>
      ) : null}
      {form.mode === "external" ? (
        <>
          <label className="wide">
            Generator command
            <input value={form.generator_command} onChange={(event) => patch("generator_command", event.target.value)} placeholder="python generate.py --count {documents} --uri {uri} --db {database} --collection {collection}" />
          </label>
          <label className="wide">
            Working directory
            <input value={form.generator_working_dir} onChange={(event) => patch("generator_working_dir", event.target.value)} />
          </label>
          <label className="ack wide">
            <input
              type="checkbox"
              checked={form.allow_external_writes}
              onChange={(event) => patch("allow_external_writes", event.target.checked)}
            />
            Allow writes outside the perfenv_ prefix
          </label>
        </>
      ) : null}
      <label>
        Compare model name
        <input value={form.second_model} onChange={(event) => patch("second_model", event.target.value)} />
      </label>
      <label className="wide">
        Second query (JSON, optional)
        <textarea value={form.second_query} onChange={(event) => patch("second_query", event.target.value)} placeholder="Same shape, different model" />
      </label>
    </div>
  );
}

function RunPanel({ run, slo }: { run: RunStatus; slo: number }) {
  const analysis = run.analysis;
  const chart = useMemo(() => chartSeries(analysis, run.observations), [analysis, run.observations]);
  const worst = analysis?.worst_class || (run.status === "complete" ? "GREEN" : "");
  const boundary = analysis?.slo_boundary;

  return (
    <section className="card results">
      <div className="results-head">
        <div>
          <p className="eyebrow">Run {run.id}</p>
          <h2>{progressLabel(run)}</h2>
        </div>
        {worst ? <span className={`pill ${worst.toLowerCase()}`}>{worst}</span> : null}
      </div>

      {run.status === "queued" || run.status === "running" ? (
        <div className="meter" aria-hidden="true">
          <span />
        </div>
      ) : null}

      {run.error ? <p className="fail">{run.error}</p> : null}

      {run.status === "complete" && analysis ? (
        <>
          <p className="summary">
            {boundary?.note ||
              (boundary?.safe_region_documents != null
                ? `Safe region through ${Number(boundary.safe_region_documents).toLocaleString()} documents.`
                : "Envelope computed.")}
            {boundary?.kind ? ` Boundary: ${boundary.kind}.` : ""}
            {analysis.validation?.mape != null ? ` MAPE ${analysis.validation.mape.toFixed(1)}%.` : ""}
          </p>
          {chart.length ? (
            <div className="chart">
              <ResponsiveContainer width="100%" height={280}>
                <LineChart data={chart} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                  <CartesianGrid stroke="rgba(31, 27, 22, 0.08)" vertical={false} />
                  <XAxis dataKey="x" tick={{ fill: "#6f675d", fontSize: 12 }} />
                  <YAxis
                    tick={{ fill: "#6f675d", fontSize: 12 }}
                    label={{ value: "p95 ms", angle: -90, position: "insideLeft", fill: "#6f675d" }}
                  />
                  <Tooltip />
                  <ReferenceLine y={slo} stroke="#c24e1d" strokeDasharray="4 4" />
                  <Line type="monotone" dataKey="p95" stroke="#1f1b16" strokeWidth={2.4} dot={{ r: 3 }} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          ) : null}
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Documents</th>
                  <th>Concurrency</th>
                  <th>Cache</th>
                  <th>p95 ms</th>
                  <th>Class</th>
                </tr>
              </thead>
              <tbody>
                {(analysis.envelope || []).map((row, index) => (
                  <tr key={index}>
                    <td>{row.dataset_size?.toLocaleString()}</td>
                    <td>{row.concurrency}</td>
                    <td>{String(row.cache_state || "").replace("estimated_", "")}</td>
                    <td>{row.p95_ms != null ? row.p95_ms.toFixed(1) : "—"}</td>
                    <td>
                      <span className={`pill tiny ${String(row.class || "").toLowerCase()}`}>{row.class}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="downloads">
            <a href={reportUrl(run.id, "html")} target="_blank" rel="noreferrer">
              HTML report
            </a>
            <a href={reportUrl(run.id, "json")} target="_blank" rel="noreferrer">
              JSON
            </a>
            <a href={reportUrl(run.id, "md")} target="_blank" rel="noreferrer">
              Markdown
            </a>
          </div>
        </>
      ) : null}
    </section>
  );
}

function chartSeries(analysis: Analysis | null, observations: Record<string, unknown>[] | null) {
  const envelope = analysis?.envelope || [];
  const sizes = new Set(envelope.map((row) => row.dataset_size));
  if (sizes.size > 1) {
    const grouped = new Map<number, number[]>();
    for (const row of envelope) {
      if (row.dataset_size == null || row.p95_ms == null) continue;
      const bucket = grouped.get(row.dataset_size) || [];
      bucket.push(row.p95_ms);
      grouped.set(row.dataset_size, bucket);
    }
    return [...grouped.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([size, values]) => ({
        x: size.toLocaleString(),
        p95: values.reduce((sum, value) => sum + value, 0) / values.length,
      }));
  }
  if (envelope.length) {
    return envelope.map((row) => ({
      x: `c=${row.concurrency ?? "?"}`,
      p95: row.p95_ms ?? 0,
    }));
  }
  if (!observations?.length) return [];
  return observations.map((row, index) => ({
    x: String(row.concurrency ?? index),
    p95: Number(row.p95_ms ?? 0),
  }));
}
