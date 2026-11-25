import { createServer } from 'node:http';
import { randomUUID, randomBytes } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { operationSchemas } from '../../agent-modules/domain-initialization/index.js';
import { extractLibraryMaterial, materialByteLimit } from '../../agent-modules/domain-initialization/materials.js';
import { requireThat } from '../../agent-modules/core/index.js';
import { openSuggestionModel } from './suggestion.js';

const assets = {
  '/': ['index.html', 'text/html'],
  '/style.css': ['style.css', 'text/css'],
  '/app.js': ['app.js', 'text/javascript']
};
const errors = {
  PERSONAL_DATA_RESTRICTED: '请去除资料中的个人联系方式和凭据后再提交。',
  SCHEMA_INVALID: '请核对必填项、字数、文件大小和原文行号。',
  EXTRACTION_UNAVAILABLE: '文件无法提取文字。请核对格式和 Python / PyMuPDF 环境，或直接粘贴可读摘录。',
  REVISION_CONFLICT: '领域库已更新。请刷新工作台后重新核对当前版本。',
  DECISION_SCOPE_MISMATCH: '候选已变化。请刷新并重新核对原文和采用用途。',
  REFERENCE_MISSING: '当前记录已变化。请刷新并选择仍有效的记录。',
  SESSION_PAUSED: '这个领域库已暂停。恢复后可以继续编辑。',
  LOCK_RECOVERY_REQUIRED: '上次写入留下了锁，请按使用指南处理后重试。',
  MODEL_FAILED: '模型未返回可用的草稿。可以重试或手动整理。',
  MODEL_UNAVAILABLE: '网页端未配置模型连接。可以手动整理，或用已注入凭据的进程重新启动。'
};
const baseHeaders = {
  'Cache-Control': 'no-store',
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'Content-Security-Policy': "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
};

function json(response, status, value) {
  response.writeHead(status, { ...baseHeaders, 'Content-Type': 'application/json; charset=utf-8' });
  response.end(JSON.stringify(value));
}

async function readJson(request, limit) {
  const blocks = [];
  let size = 0;
  for await (const block of request) {
    size += block.length;
    if (size <= limit) blocks.push(block);
  }
  requireThat(size <= limit, 'SCHEMA_INVALID', 'Request size exceeds the limit');
  let input;
  try { input = JSON.parse(Buffer.concat(blocks).toString('utf8')); } catch {}
  requireThat(input && typeof input === 'object' && !Array.isArray(input), 'SCHEMA_INVALID', 'Expected an object');
  return input;
}

export async function openDomainWeb({ host, port = 4176, configPath, python = 'python' }) {
  let suggest;
  if (configPath) {
    try { suggest = await openSuggestionModel(configPath); }
    catch (error) {
      if (error.code !== 'AUTHORIZATION_REQUIRED') throw error;
    }
  }
  const sessionToken = randomBytes(32).toString('hex');
  const actor = { actorId: 'domain-library-web-user', actorInstanceId: randomUUID(), kind: 'human' };
  const invoke = (operation, input) => host.call('domain-initialization', operation, input, { actor, requestId: randomUUID() });
  let origin;
  const server = createServer(async (request, response) => {
    const path = request.url;
    if (request.headers.host !== new URL(origin).host || request.headers['sec-fetch-site'] === 'cross-site'
      || (request.headers.origin && request.headers.origin !== origin)) {
      json(response, 403, { message: '该工作台仅接受本地页面发出的请求。' });
      return;
    }
    try {
      if (request.method === 'GET' && Object.hasOwn(assets, path)) {
        const [file, type] = assets[path];
        const content = await readFile(new URL(`./public/${file}`, import.meta.url));
        response.writeHead(200, { ...baseHeaders, 'Content-Type': `${type}; charset=utf-8` });
        response.end(content);
        return;
      }
      if (request.method === 'GET' && path === '/api/session') {
        json(response, 200, { token: sessionToken, modelAvailable: Boolean(suggest) });
        return;
      }
      if (request.method !== 'POST' || !path.startsWith('/api/')) {
        json(response, 404, { message: '找不到该页面。' });
        return;
      }
      if (request.headers['x-domain-session'] !== sessionToken || request.headers['content-type'] !== 'application/json') {
        json(response, 403, { message: '请从工作台重新发起操作。' });
        return;
      }
      const operation = path.slice(5);
      const input = await readJson(request, operation === 'upload' ? 12 * 1024 * 1024 : 2 * 1024 * 1024);
      if (operation === 'upload') {
        const { name, base64, libraryId, title, sourceType, expectedRevision, operationId } = input;
        requireThat(typeof name === 'string' && /^[^/\\\x00-\x1f]{1,255}$/.test(name)
          && typeof base64 === 'string' && base64.length <= Math.ceil(materialByteLimit / 3) * 4
          && /^[A-Za-z0-9+/]+={0,2}$/.test(base64), 'SCHEMA_INVALID', 'Invalid file upload');
        const material = await extractLibraryMaterial(Buffer.from(base64, 'base64'), { locator: name, python });
        json(response, 200, await invoke('source', { libraryId, title, sourceType, expectedRevision, operationId, ...material }));
        return;
      }
      if (operation === 'suggest') {
        requireThat(suggest, 'MODEL_UNAVAILABLE', 'Model is not configured');
        const { libraryId, sourceId, startLine, endLine } = input;
        requireThat(Number.isSafeInteger(startLine) && Number.isSafeInteger(endLine) && startLine <= endLine,
          'SCHEMA_INVALID', 'Choose an evidence line range first');
        const excerpt = await invoke('readSource', { libraryId, sourceId, startLine, endLine });
        requireThat(excerpt.endLine === endLine && !excerpt.truncated && excerpt.inlineText.trim()
          && excerpt.inlineText.length <= 8000, 'SCHEMA_INVALID', 'Choose a nonempty source excerpt of at most 8000 characters');
        let draft;
        try { draft = await suggest({ excerpt: excerpt.inlineText }); }
        catch (error) {
          if (error.code === 'PERSONAL_DATA_RESTRICTED') throw error;
          requireThat(false, 'MODEL_FAILED', 'The model did not return a draft');
        }
        json(response, 200, draft);
        return;
      }
      if (!Object.hasOwn(operationSchemas, operation)) {
        json(response, 404, { message: '找不到该操作。' });
        return;
      }
      json(response, 200, await invoke(operation, input));
    } catch (error) {
      const code = error.code || 'OPERATION_FAILED';
      json(response, code === 'REVISION_CONFLICT' ? 409 : errors[code] ? 400 : 500,
        { code, message: errors[code] || '操作未完成，请刷新工作台后重试。' });
    }
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '127.0.0.1', () => {
      origin = `http://127.0.0.1:${server.address().port}`;
      resolve();
    });
  });
  return { server, origin };
}
