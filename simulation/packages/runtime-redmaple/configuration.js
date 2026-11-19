import { readFile } from 'node:fs/promises';
import { AgentBaseError } from '@paperpilot/agent-base';
import { requireCondition } from '../contracts/index.js';
import { createCurlFetch } from './curl-fetch.js';

export function withModelSelectionPolicy(options) {
  if (!options.allowedModels) return options;
  requireCondition(Array.isArray(options.allowedModels) && options.allowedModels.length > 0
    && options.allowedModels.every(value => typeof value === 'string' && value.includes('/')),
  'SCHEMA_INVALID', 'allowedModels must contain provider/model identities');
  const allowed = new Set(options.allowedModels);
  const beforeModelCall = options.beforeModelCall;
  return { ...options, beforeModelCall: async (request, signal) => {
    const identity = `${request.model.provider}/${request.model.id}`;
    if (!allowed.has(identity)) throw new AgentBaseError('model_restricted', `Simulation model ${identity} is outside the configured selection`, { knownOutcome: true });
    await beforeModelCall?.(request, signal);
  } };
}

export async function loadSimulationRuntimeConfig(path, { environment = process.env } = {}) {
  const config = JSON.parse((await readFile(path, 'utf8')).replace(/^\uFEFF/, ''));
  requireCondition(!config.apiKey && Object.values(config.providers || {}).every(provider => !provider.apiKey),
    'SCHEMA_INVALID', 'Runtime configuration must reference injected credentials instead of storing API keys');
  const connection = config.providerConnection;
  if (connection) {
    const baseUrl = environment[connection.baseUrlEnv] || connection.baseUrl;
    requireCondition(baseUrl, 'CAPABILITY_UNAVAILABLE', `Configure ${connection.baseUrlEnv || 'providerConnection.baseUrl'}`);
    const parsed = new URL(baseUrl);
    requireCondition(['http:', 'https:'].includes(parsed.protocol) && !parsed.username && !parsed.password,
      'SCHEMA_INVALID', 'Provider endpoint must be an HTTP(S) URL without embedded credentials');
    requireCondition(connection.provider && connection.modelId, 'SCHEMA_INVALID', 'Provider and model ID are required');
    requireCondition(config.model === `${connection.provider}/${connection.modelId}`, 'SCHEMA_INVALID', 'Provider connection differs from the selected model');
    const apiKeyEnv = connection.apiKeyEnv || config.apiKeyEnv;
    requireCondition(!connection.apiKeyFile, 'SCHEMA_INVALID', 'Inject provider credentials through twin inject into the configured environment variable');
    const apiKey = environment[apiKeyEnv];
    requireCondition(apiKey, 'AUTHORIZATION_REQUIRED', `Inject provider credentials through twin inject into ${apiKeyEnv}`);
    config.apiKey = apiKey;
    config.providers = { ...config.providers, [connection.provider]: {
      baseUrl, api: connection.api || 'openai-completions',
      models: [{ id: connection.modelId, name: connection.displayName || connection.modelId, reasoning: connection.reasoning ?? false,
        input: ['text'], contextWindow: connection.contextWindow || 32768, maxTokens: connection.maxTokens || 4096,
        cost: connection.cost || { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        ...(connection.thinkingLevelMap ? { thinkingLevelMap: connection.thinkingLevelMap } : {}),
        ...(connection.compat ? { compat: connection.compat } : {}) }]
    } };
    if (connection.transport === 'curl') {
      const proxy = environment[connection.proxyEnv || 'HTTPS_PROXY'] || environment.https_proxy;
      config.fetch = createCurlFetch({ executable: connection.curlExecutable, proxy });
      config.transportProfile = { kind: 'curl', proxyConfigured: !!proxy };
    }
    delete config.providerConnection;
  }
  return withModelSelectionPolicy(config);
}
