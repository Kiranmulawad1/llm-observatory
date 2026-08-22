// Ingest load benchmark.
//
//   make bench                          # defaults below
//   BENCH_VUS=100 BENCH_DURATION=2m make bench
//
// Measures what the platform accepts, not what the load generator can produce.
// See bench/README.md for the methodology and the caveats that go with any
// number this prints.

import http from 'k6/http';
import { check } from 'k6';
import { Counter, Trend } from 'k6/metrics';
import { randomBytes } from 'k6/crypto';

const BASE = __ENV.BENCH_BASE_URL || 'http://localhost:8000';
const ADMIN = __ENV.LO_ADMIN_TOKEN;
// 20 is past the knee — see bench/README.md. Chosen as the default because a
// regression guard should run where queueing is visible, not where the server
// is idle and every change looks free.
const VUS = parseInt(__ENV.BENCH_VUS || '20', 10);
const DURATION = __ENV.BENCH_DURATION || '60s';
// Spans per request. The SDK batches, so a single-span request is not the
// shape real traffic arrives in — measuring it would flatter per-request
// overhead and understate the per-span cost that actually scales.
const BATCH = parseInt(__ENV.BENCH_BATCH || '50', 10);

// Spans, not requests. A request-rate number is meaningless here because the
// batch size is a free variable: doubling BATCH halves requests/sec while
// doing strictly more work.
const spansIngested = new Counter('spans_ingested');
const spansRejected = new Counter('spans_rejected');
const nativeLatency = new Trend('native_ingest_latency', true);
const otlpLatency = new Trend('otlp_ingest_latency', true);

export const options = {
  scenarios: {
    // The two ingest paths, run separately rather than mixed, so their
    // latencies are attributable. OTLP does protobuf-shaped decoding and
    // GenAI attribute mapping that the native path does not, and the whole
    // point of measuring both is to find out what that costs.
    native: {
      executor: 'constant-vus',
      vus: VUS,
      duration: DURATION,
      exec: 'nativeIngest',
      tags: { path: 'native' },
    },
    otlp: {
      executor: 'constant-vus',
      vus: VUS,
      duration: DURATION,
      exec: 'otlpIngest',
      startTime: DURATION,
      tags: { path: 'otlp' },
    },
  },
  thresholds: {
    // Thresholds make this a regression guard rather than a report someone has
    // to read and interpret. A build that halves ingest throughput should fail,
    // not produce a slightly worse number nobody notices.
    //
    // Calibrated against the measured baseline at BENCH_VUS=20 with roughly 2x
    // headroom: tight enough to catch a real regression, loose enough not to
    // fire on the run-to-run variance of a developer laptop that is also
    // running Postgres.
    'http_req_failed': ['rate<0.01'],
    // The throughput guard, and the one that matters most. Latency alone can
    // look healthy while the server quietly does less work.
    'spans_ingested': ['rate>2000'],
    'native_ingest_latency': ['p(95)<800'],
    'otlp_ingest_latency': ['p(95)<1000'],
  },
  // The summary prints these percentiles; p99 is the one that shows queueing,
  // and an average would hide it entirely.
  summaryTrendStats: ['avg', 'min', 'p(50)', 'p(95)', 'p(99)', 'max'],
};

function hex(bytes) {
  return Array.from(new Uint8Array(randomBytes(bytes)))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

// One project per path, so the per-project rate limiter is not the thing being
// measured — and two of them, so the two scenarios cannot contend for the same
// window if they are ever run concurrently.
export function setup() {
  if (!ADMIN) {
    throw new Error('LO_ADMIN_TOKEN is required; run via `make bench`');
  }
  const headers = {
    Authorization: `Bearer ${ADMIN}`,
    'Content-Type': 'application/json',
  };

  const keys = {};
  for (const name of ['native', 'otlp']) {
    const slug = `bench-${name}-${hex(4)}`;
    const created = http.post(
      `${BASE}/projects`,
      JSON.stringify({ slug, name: `Benchmark ${name}` }),
      { headers },
    );
    if (created.status !== 201) {
      throw new Error(`could not create project: ${created.status} ${created.body}`);
    }
    const key = http.post(
      `${BASE}/projects/${slug}/api-keys`,
      JSON.stringify({ name: 'bench', scopes: ['ingest'] }),
      { headers },
    );
    if (key.status !== 201) {
      throw new Error(`could not issue key: ${key.status} ${key.body}`);
    }
    keys[name] = JSON.parse(key.body).key;
  }
  return keys;
}

function nativeBatch() {
  const traceId = hex(16);
  const rootId = hex(8);
  const startedAt = new Date().toISOString();
  const spans = [
    {
      trace_id: traceId,
      span_id: rootId,
      parent_span_id: null,
      name: 'answer_question',
      kind: 'chain',
      status: 'ok',
      started_at: startedAt,
      duration_ms: 1200,
    },
  ];
  // A realistic tree rather than a flat list: children are what exercise the
  // parent-pointer writes and the rollup recompute.
  for (let i = 1; i < BATCH; i++) {
    const llm = i % 3 === 0;
    spans.push({
      trace_id: traceId,
      span_id: hex(8),
      parent_span_id: rootId,
      name: llm ? 'generate' : 'retrieve',
      kind: llm ? 'llm' : 'retrieval',
      status: 'ok',
      started_at: startedAt,
      duration_ms: 40 + (i % 30),
      model: llm ? 'claude-sonnet-5' : null,
      prompt_tokens: llm ? 900 : null,
      completion_tokens: llm ? 150 : null,
    });
  }
  return spans;
}

export function nativeIngest(data) {
  const spans = nativeBatch();
  const res = http.post(`${BASE}/v1/traces`, JSON.stringify({ spans }), {
    headers: {
      Authorization: `Bearer ${data.native}`,
      'Content-Type': 'application/json',
    },
    tags: { name: 'POST /v1/traces' },
  });

  nativeLatency.add(res.timings.duration);
  const ok = check(res, { 'native 202': (r) => r.status === 202 });
  if (ok) spansIngested.add(spans.length); else spansRejected.add(spans.length);
}

function otlpBatch() {
  const traceId = hex(16);
  const rootId = hex(8);
  // OTLP timestamps are nanoseconds since the epoch, as strings — int64 does
  // not survive a JSON number.
  const startNano = `${Date.now()}000000`;
  const spans = [
    {
      traceId,
      spanId: rootId,
      name: 'answer_question',
      startTimeUnixNano: startNano,
      endTimeUnixNano: `${Date.now() + 1200}000000`,
      status: { code: 1 },
    },
  ];
  for (let i = 1; i < BATCH; i++) {
    const llm = i % 3 === 0;
    spans.push({
      traceId,
      spanId: hex(8),
      parentSpanId: rootId,
      name: llm ? 'chat' : 'retrieve',
      startTimeUnixNano: startNano,
      endTimeUnixNano: `${Date.now() + 40}000000`,
      status: { code: 1 },
      // Exercise the GenAI mapping, which is the work the native path skips.
      attributes: llm
        ? [
            { key: 'gen_ai.system', value: { stringValue: 'anthropic' } },
            { key: 'gen_ai.operation.name', value: { stringValue: 'chat' } },
            { key: 'gen_ai.request.model', value: { stringValue: 'claude-sonnet-5' } },
            { key: 'gen_ai.usage.input_tokens', value: { intValue: '900' } },
            { key: 'gen_ai.usage.output_tokens', value: { intValue: '150' } },
          ]
        : [{ key: 'db.system', value: { stringValue: 'qdrant' } }],
    });
  }
  return {
    resourceSpans: [
      {
        resource: {
          attributes: [{ key: 'service.name', value: { stringValue: 'bench' } }],
        },
        scopeSpans: [{ spans }],
      },
    ],
  };
}

export function otlpIngest(data) {
  const payload = otlpBatch();
  const count = payload.resourceSpans[0].scopeSpans[0].spans.length;
  const res = http.post(`${BASE}/otlp/v1/traces`, JSON.stringify(payload), {
    headers: {
      Authorization: `Bearer ${data.otlp}`,
      'Content-Type': 'application/json',
    },
    tags: { name: 'POST /otlp/v1/traces' },
  });

  otlpLatency.add(res.timings.duration);
  const ok = check(res, { 'otlp 200': (r) => r.status === 200 });
  if (ok) spansIngested.add(count); else spansRejected.add(count);
}
