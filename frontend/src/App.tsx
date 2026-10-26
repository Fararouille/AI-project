import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  askStream,
  getHealth,
  getSources,
  runEvaluation,
  type AskResult,
  type EvalResponse,
  type Health,
  type Source,
} from "./api";

const EXAMPLES = [
  "How many tiles can a dashboard have?",
  "Why do my events return 202 but never show up?",
  "How much does event overage cost on the Growth plan?",
];

function CitationText({
  text,
  onCite,
  active,
}: {
  text: string;
  onCite: (marker: number) => void;
  active: number | null;
}) {
  const parts = useMemo(() => text.split(/(\[\d{1,2}\])/g), [text]);
  return (
    <p className="answer-text">
      {parts.map((part, index) => {
        const match = /^\[(\d{1,2})\]$/.exec(part);
        if (!match) return <span key={index}>{part}</span>;
        const marker = Number(match[1]);
        return (
          <button
            key={index}
            className={`cite-chip${active === marker ? " cite-chip-active" : ""}`}
            onClick={() => onCite(marker)}
            title="Show source"
          >
            {marker}
          </button>
        );
      })}
    </p>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
      <span className="metric-value">{value}</span>
      <span className="metric-label">{label}</span>
    </div>
  );
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [question, setQuestion] = useState("");
  const [k, setK] = useState(5);
  const [mode, setMode] = useState("auto");
  const [rerank, setRerank] = useState(false);

  const [answer, setAnswer] = useState<AskResult | null>(null);
  const [streamed, setStreamed] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeCitation, setActiveCitation] = useState<number | null>(null);

  const [evaluation, setEvaluation] = useState<EvalResponse | null>(null);
  const [evalBusy, setEvalBusy] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    getHealth().then(setHealth).catch((err: Error) => setError(err.message));
    getSources().then(setSources).catch(() => setSources([]));
  }, []);

  const submit = useCallback(
    async (raw?: string) => {
      const value = (raw ?? question).trim();
      if (!value || busy) return;
      setQuestion(value);
      setBusy(true);
      setError(null);
      setAnswer(null);
      setStreamed("");
      setActiveCitation(null);

      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      try {
        const result = await askStream(
          { question: value, k, mode, rerank },
          (delta) => setStreamed((prev) => prev + delta),
          controller.signal,
        );
        setAnswer(result);
        setStreamed("");
      } catch (err) {
        if ((err as Error).name !== "AbortError") {
          setError((err as Error).message);
        }
      } finally {
        setBusy(false);
      }
    },
    [busy, question, k, mode, rerank],
  );

  const evaluate = useCallback(async () => {
    setEvalBusy(true);
    setError(null);
    try {
      setEvaluation(await runEvaluation());
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setEvalBusy(false);
    }
  }, []);

  const displayText = answer?.answer ?? streamed;
  const citations = answer?.citations ?? [];
  const activeSource = citations.find((c) => c.marker === activeCitation) ?? null;

  return (
    <div className="app">
      <header className="header">
        <div className="brand">
          <span className="brand-mark">[</span>
          <h1>cite</h1>
          <span className="brand-mark">]</span>
          <span className="brand-tag">answers with receipts</span>
        </div>
        <div className="status">
          {health ? (
            <>
              <span className={`dot dot-${health.status}`} />
              {health.documents} docs · {health.chunks} chunks · {health.embedder}
            </>
          ) : (
            <span>connecting…</span>
          )}
        </div>
      </header>

      <main className="layout">
        <section className="panel panel-main">
          <form
            className="ask"
            onSubmit={(event) => {
              event.preventDefault();
              void submit();
            }}
          >
            <input
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="Ask a question about the handbook…"
              aria-label="Question"
              autoFocus
            />
            <button type="submit" disabled={busy || !question.trim()}>
              {busy ? "retrieving…" : "ask"}
            </button>
          </form>

          <div className="controls">
            <label>
              depth
              <select value={k} onChange={(event) => setK(Number(event.target.value))}>
                {[3, 5, 8, 10].map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
            <label>
              retriever
              <select value={mode} onChange={(event) => setMode(event.target.value)}>
                <option value="auto">auto</option>
                <option value="bm25">bm25</option>
                <option value="dense">dense</option>
                <option value="hybrid">hybrid</option>
              </select>
            </label>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={rerank}
                onChange={(event) => setRerank(event.target.checked)}
              />
              rerank
            </label>
          </div>

          {!answer && !busy && !streamed && (
            <div className="examples">
              <span className="examples-label">try</span>
              {EXAMPLES.map((example) => (
                <button key={example} onClick={() => void submit(example)}>
                  {example}
                </button>
              ))}
            </div>
          )}

          {error && <div className="error">{error}</div>}

          {(displayText || busy) && (
            <article className={`answer${busy ? " answer-busy" : ""}`}>
              <CitationText
                text={displayText}
                onCite={(marker) =>
                  setActiveCitation((current) => (current === marker ? null : marker))
                }
                active={activeCitation}
              />
              {answer && (
                <footer className="answer-meta">
                  <span>{answer.latency_ms.toFixed(1)} ms</span>
                  <span>{answer.mode}</span>
                  <span>{answer.provider}</span>
                  <span className={answer.citations.length ? "ok" : "warn"}>
                    {answer.citations.length} citation
                    {answer.citations.length === 1 ? "" : "s"}
                  </span>
                </footer>
              )}
            </article>
          )}

          {citations.length > 0 && (
            <div className="sources">
              <h2>Sources</h2>
              {citations.map((citation) => (
                <button
                  key={citation.marker}
                  className={`source${activeCitation === citation.marker ? " source-active" : ""}`}
                  onClick={() =>
                    setActiveCitation((current) =>
                      current === citation.marker ? null : citation.marker,
                    )
                  }
                >
                  <div className="source-head">
                    <span className="source-marker">{citation.marker}</span>
                    <span className="source-title">{citation.title}</span>
                    {citation.heading && (
                      <span className="source-heading">{citation.heading}</span>
                    )}
                  </div>
                  <p className="source-snippet">{citation.snippet}</p>
                </button>
              ))}
            </div>
          )}

          {activeSource && (
            <div className="excerpt">
              <span className="excerpt-label">
                matched chunk · score {activeSource.score.toFixed(3)}
              </span>
              <p>{activeSource.snippet}</p>
            </div>
          )}
        </section>

        <aside className="panel panel-side">
          <div className="metrics">
            <Metric label="documents" value={String(health?.documents ?? 0)} />
            <Metric label="chunks" value={String(health?.chunks ?? 0)} />
            <Metric label="provider" value={health?.provider ?? "—"} />
          </div>

          <div className="side-block">
            <div className="side-head">
              <h2>Benchmark</h2>
              <button className="ghost" onClick={() => void evaluate()} disabled={evalBusy}>
                {evalBusy ? "running…" : "run"}
              </button>
            </div>
            {evaluation ? (
              <table className="bench">
                <thead>
                  <tr>
                    <th>config</th>
                    <th>R@k</th>
                    <th>MRR</th>
                    <th>nDCG</th>
                  </tr>
                </thead>
                <tbody>
                  {evaluation.results.map((row) => (
                    <tr key={row.name}>
                      <td>{row.name}</td>
                      <td>{row.recall_at_k.toFixed(3)}</td>
                      <td>{row.mrr.toFixed(3)}</td>
                      <td>{row.ndcg_at_k.toFixed(3)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="muted">
                Run the ablation to compare BM25, dense, hybrid, and reranking on{" "}
                {health ? "the golden set" : "the corpus"}.
              </p>
            )}
            {evaluation && (
              <p className="muted small">
                {evaluation.queries} hand-labelled queries · k=5
              </p>
            )}
          </div>

          <div className="side-block">
            <h2>Corpus</h2>
            <ul className="corpus">
              {sources.map((source) => (
                <li key={source.doc_id}>
                  <span className="corpus-title">{source.title}</span>
                  <span className="corpus-count">{source.chunks}</span>
                </li>
              ))}
            </ul>
          </div>
        </aside>
      </main>

      <footer className="footer">
        Retrieval is measured, not assumed — see <code>cite eval</code> and{" "}
        <code>eval/results.md</code>.
      </footer>
    </div>
  );
}
