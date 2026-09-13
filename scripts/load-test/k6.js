// Load profile for the gateway: streaming chat, sync chat, embeddings.
//
//   k6 run -e API_KEY=sk-... scripts/load-test/k6.js
//
// Env: BASE_URL (default https://api.openmodel.test), API_KEY (required),
// INSECURE=1 to accept the cluster's self-signed certificate.
//
// The VU counts are sized to stay under what the *backend* can serve, not what
// the plan allows. The pro plan permits 10 concurrent requests, but Ollama runs
// OLLAMA_NUM_PARALLEL=2: streams that queue behind it keep holding their
// concurrency slots, so anything above ~8 in flight turns into a 429 storm and
// k6 scores every one of those as a failed request. Raise these numbers only
// together with the backend's parallelism.
import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'https://api.openmodel.test';
const API_KEY = __ENV.API_KEY;
const CHAT_MODEL = __ENV.CHAT_MODEL || 'qwen2.5:0.5b';
const EMBED_MODEL = __ENV.EMBED_MODEL || 'nomic-embed-text';

const scenarios = {
  // 3 VUs streaming: the steady in-flight load the HPA reacts to.
  // STREAM_VUS lets the runbook's OLLAMA_NUM_PARALLEL comparison run leaner.
  chat_stream: {
    executor: 'constant-vus',
    vus: Number(__ENV.STREAM_VUS || 3),
    duration: '60s',
    exec: 'chatStream',
  },
  chat_sync: {
    executor: 'constant-arrival-rate',
    rate: 1,
    timeUnit: '1s',
    duration: '60s',
    preAllocatedVUs: 3,
    exec: 'chatSync',
  },
  embeddings: {
    executor: 'constant-arrival-rate',
    rate: 2,
    timeUnit: '1s',
    duration: '60s',
    preAllocatedVUs: 2,
    exec: 'embeddings',
  },
};

// ONLY=chat_stream runs one scenario — used by the runbook's OLLAMA_NUM_PARALLEL
// experiment, where the other traffic would muddy the comparison.
if (__ENV.ONLY) {
  for (const name of Object.keys(scenarios)) {
    if (!__ENV.ONLY.split(',').includes(name)) delete scenarios[name];
  }
}

export const options = {
  insecureSkipTLSVerify: __ENV.INSECURE === '1',
  scenarios,
  thresholds: {
    http_req_failed: ['rate<0.05'],
    http_req_duration: ['p(95)<10000'],
  },
};

function params(tag) {
  return {
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${API_KEY}` },
    timeout: '120s',
    tags: { endpoint: tag },
  };
}

function chat(stream, maxTokens) {
  return JSON.stringify({
    model: CHAT_MODEL,
    messages: [{ role: 'user', content: 'Name one primary colour.' }],
    // MAX_TOKENS shortens every generation. On a host slow enough that a 64-token
    // stream outlives the 60s scenario, nothing completes and k6 reports nothing —
    // drop it to 8 and the run still produces TTFT and tokens/s to compare.
    max_tokens: Number(__ENV.MAX_TOKENS || maxTokens),
    stream,
  });
}

export function chatStream() {
  // k6 reads the whole SSE body before returning, so duration covers the
  // full generation, not just time to first byte.
  const res = http.post(`${BASE_URL}/v1/chat/completions`, chat(true, 64), params('chat_stream'));
  check(res, {
    'stream 200': (r) => r.status === 200,
    'stream has deltas': (r) => r.body && r.body.includes('data: ') && r.body.includes('[DONE]'),
  });
}

export function chatSync() {
  const res = http.post(`${BASE_URL}/v1/chat/completions`, chat(false, 32), params('chat_sync'));
  check(res, {
    'sync 200': (r) => r.status === 200,
    'sync has content': (r) => {
      if (r.status !== 200) return false;
      const c = r.json('choices.0.message.content');
      return typeof c === 'string' && c.length > 0;
    },
  });
}

export function embeddings() {
  const body = JSON.stringify({ model: EMBED_MODEL, input: 'the quick brown fox' });
  const res = http.post(`${BASE_URL}/v1/embeddings`, body, params('embeddings'));
  check(res, {
    'embed 200': (r) => r.status === 200,
    'embed has vector': (r) => r.status === 200 && r.json('data.0.embedding.0') !== undefined,
  });
}

export function handleSummary(data) {
  const m = data.metrics;
  const n = (metric, stat) => (m[metric] && m[metric].values[stat] !== undefined
    ? m[metric].values[stat].toFixed(2)
    : 'n/a');
  const lines = [
    `requests        ${n('http_reqs', 'count')} (${n('http_reqs', 'rate')}/s)`,
    `failed          ${(Number(n('http_req_failed', 'rate')) * 100).toFixed(2)}%`,
    `duration avg    ${n('http_req_duration', 'avg')} ms`,
    `duration p(95)  ${n('http_req_duration', 'p(95)')} ms`,
    `checks passed   ${n('checks', 'passes')} / failed ${n('checks', 'fails')}`,
    '',
  ].join('\n');
  return {
    stdout: `\n${lines}`,
    'scripts/load-test/summary.json': JSON.stringify(data, null, 2),
  };
}

// Smoke path: `k6 run --vus 1 --duration 5s ...` overrides the scenarios above
// and needs a default export to run at all.
export default chatSync;
