import { loadSimulationRuntimeConfig } from '../packages/runtime-redmaple/configuration.js';

const config = await loadSimulationRuntimeConfig(new URL('../runtime.example.json', import.meta.url));
const [provider, model] = config.model.split('/');
const base = config.providers[provider].baseUrl.replace(/\/$/, '');
const redact = value => String(value).replaceAll(config.apiKey, '[REDACTED]');
try {
  const response = await fetch(`${base}/models`, { headers: { Authorization: `Bearer ${config.apiKey}` }, signal: AbortSignal.timeout(30000) });
  const body = await response.json();
  console.log(JSON.stringify({ status: response.status, provider, requestedModel: model,
    modelIds: Array.isArray(body.data) ? body.data.map(item => item.id).filter(id => /dsv|deepseek|4\.1/i.test(id)) : [],
    error: body.error ? redact(body.error.message || body.error.code || 'Provider error') : null }));
  if (!response.ok) process.exitCode = 1;
} catch (error) {
  console.log(JSON.stringify({ status: 'connectionFailed', provider, error: redact(error.message),
    cause: error.cause ? redact(error.cause.code || error.cause.message) : null }));
  process.exitCode = 1;
}
