import { createRequire } from 'node:module';
import { readFile } from 'node:fs/promises';
const require = createRequire(import.meta.resolve('@paperpilot/agent-base'));
const { EnvHttpProxyAgent, fetch } = require('undici');
const agent = new EnvHttpProxyAgent();
const { providerConnection: connection } = JSON.parse(await readFile('runtime.example.json', 'utf8'));
const key = process.env.AISPOT_DS_API_KEY;
try {
  const response = await fetch(`${connection.baseUrl}/chat/completions`, { dispatcher: agent,
    method: 'POST', signal: AbortSignal.timeout(45000),
    headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ model: connection.modelId, stream: false, max_tokens: 128,
      thinking: { type: 'disabled' }, messages: [{ role: 'user', content: 'Reply with PROVIDER_READY only.' }] }) });
  const data = await response.json();
  console.log(JSON.stringify({ status: response.status, model: data.model, usage: data.usage,
    answer: data.choices?.[0]?.message?.content, error: data.error }));
} catch (error) {
  console.log(JSON.stringify({ error: String(error.message).replaceAll(key || '\u0000', '[REDACTED]'), code: error.cause?.code }));
  process.exitCode = 1;
} finally { await agent.close(); }
