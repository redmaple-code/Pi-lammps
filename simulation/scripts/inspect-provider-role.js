import { readFile } from 'node:fs/promises';
import { openApplication } from '../packages/application/index.js';
import { simulationRoot } from './build-packs.js';

const key = process.env.AISPOT_DS_API_KEY;
const redact = value => String(value || '').replaceAll(key || '\u0000', '[REDACTED]');
const app = await openApplication({ workspace: simulationRoot });
try {
  const record = app.store.list('role-operations').filter(item => item.id.startsWith('provider-role-check-')).at(-1);
  const entries = (await readFile(record.sessionFile, 'utf8')).split(/\r?\n/).filter(Boolean).map(line => JSON.parse(line));
  const messages = entries.filter(item => item.type === 'message' && item.message?.role === 'assistant').map(item => ({
    stopReason: item.message.stopReason, errorMessage: redact(item.message.errorMessage),
    toolCalls: item.message.content.filter(part => part.type === 'toolCall').map(part => part.name)
  }));
  console.log(JSON.stringify({ operationId: record.id, status: record.status, provider: record.provider, model: record.model, messages }, null, 2));
} finally { app.close(); }
