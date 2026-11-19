import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createCurlFetch } from '../packages/runtime-redmaple/curl-fetch.js';

async function server(t, handler) {
  const instance = createServer(handler);
  await new Promise(resolve => instance.listen(0, '127.0.0.1', resolve));
  t.after(() => { instance.closeAllConnections(); return new Promise(resolve => instance.close(resolve)); });
  return `http://127.0.0.1:${instance.address().port}`;
}

test('cURL transport preserves JSON Unicode, quoting, headers and incremental SSE content', async t => {
  let received;
  const url = await server(t, async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    received = { body: JSON.parse(Buffer.concat(chunks).toString('utf8')), authorization: request.headers.authorization };
    response.writeHead(200, { 'Content-Type': 'text/event-stream', 'X-Transport-Test': 'curl' });
    response.write('data: {"text":"第一段"}\n\n');
    setTimeout(() => response.end('data: [DONE]\n\n'), 20);
  });
  const body = { text: '中文\n"quoted" \\ literal' };
  const response = await createCurlFetch()(`${url}/chat/completions`, { method: 'POST',
    headers: { Authorization: 'Bearer test-fixture-only', 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('x-transport-test'), 'curl');
  const text = await response.text();
  assert.ok(text.includes('第一段') && text.includes('[DONE]'));
  assert.deepEqual(received.body, body);
  assert.equal(received.authorization, 'Bearer test-fixture-only');
});

test('cURL transport preserves HTTP error bodies and aborts streaming requests', async t => {
  const url = await server(t, (request, response) => {
    if (request.url === '/error') { response.writeHead(429); response.end('{"error":"fixture rate limit"}'); return; }
    response.writeHead(200, { 'Content-Type': 'text/event-stream' });
    response.write('data: first\n\n');
  });
  const fetch = createCurlFetch();
  const failure = await fetch(`${url}/error`);
  assert.equal(failure.status, 429);
  assert.equal((await failure.json()).error, 'fixture rate limit');
  const abort = new AbortController();
  const streaming = await fetch(`${url}/stream`, { signal: abort.signal });
  const reading = streaming.text();
  abort.abort(new DOMException('Fixture cancellation', 'AbortError'));
  await assert.rejects(reading, /Fixture cancellation/);
});
