export type Citation = {
  marker: number;
  doc_id: string;
  title: string;
  heading: string;
  snippet: string;
  score: number;
};

export type AskResult = {
  question: string;
  answer: string;
  citations: Citation[];
  latency_ms: number;
  mode: string;
  reranked: boolean;
  provider: string;
  cost_usd: number;
};

export type Health = {
  status: string;
  version: string;
  documents: number;
  chunks: number;
  embedder: string;
  provider: string;
};

export type Source = {
  doc_id: string;
  title: string;
  chunks: number;
  headings: string[];
};

export type EvalRow = {
  name: string;
  recall_at_k: number;
  mrr: number;
  ndcg_at_k: number;
  latency_p50_ms: number;
  latency_p95_ms: number;
};

export type EvalResponse = {
  markdown: string;
  queries: number;
  results: EvalRow[];
};

const API_KEY_STORAGE = "cite.apiKey";

export function getApiKey(): string {
  try {
    return localStorage.getItem(API_KEY_STORAGE) ?? "";
  } catch {
    return "";
  }
}

export function setApiKey(key: string): void {
  try {
    if (key) localStorage.setItem(API_KEY_STORAGE, key);
    else localStorage.removeItem(API_KEY_STORAGE);
  } catch {
    // Private-mode browsers can reject storage; the in-memory state still works.
  }
}

function headers(init?: RequestInit): Headers {
  const merged = new Headers(init?.headers);
  if (!merged.has("Content-Type")) merged.set("Content-Type", "application/json");
  const key = getApiKey();
  if (key) merged.set("X-API-Key", key);
  return merged;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: headers(init) });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

export const getHealth = () => request<Health>("/api/health");
export const getSources = () => request<Source[]>("/api/sources");

export const runEvaluation = () =>
  request<EvalResponse>("/api/eval", { method: "POST" });

export type AskOptions = {
  question: string;
  k: number;
  mode: string;
  rerank: boolean;
};

export function ask(options: AskOptions): Promise<AskResult> {
  return request<AskResult>("/api/ask", {
    method: "POST",
    body: JSON.stringify(options),
  });
}

/**
 * Reads the server-sent-events stream by hand rather than pulling in a library.
 * The event format is small and fixed: `event: token` frames carry a delta,
 * `event: done` carries the full answer payload.
 */
export async function askStream(
  options: AskOptions,
  onToken: (delta: string) => void,
  signal?: AbortSignal,
): Promise<AskResult> {
  const response = await fetch("/api/ask/stream", {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(options),
    signal,
  });
  if (!response.ok || !response.body) {
    throw new Error(await response.text());
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let final: AskResult | null = null;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";

    for (const frame of frames) {
      const event = frame
        .split("\n")
        .find((line) => line.startsWith("event: "))
        ?.slice(7);
      const data = frame
        .split("\n")
        .filter((line) => line.startsWith("data: "))
        .map((line) => line.slice(6))
        .join("\n");
      if (!data) continue;

      if (event === "token") {
        onToken((JSON.parse(data) as { delta: string }).delta);
      } else if (event === "done") {
        final = JSON.parse(data) as AskResult;
      }
    }
  }

  if (!final) throw new Error("stream ended without a final answer");
  return final;
}
