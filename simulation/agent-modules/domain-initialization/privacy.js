import { requireThat } from '../core/index.js';

// Reject rather than echo or persist matches. This covers recognizable input,
// while source owners remain responsible for preparing de-identified documents.
export function checkLibraryContent(value) {
  const raw = typeof value === 'string' ? value : JSON.stringify(value);
  const text = raw.replace(/sha256:[a-f0-9]{64}/g, '[CONTENT_DIGEST]')
    .replace(/\b[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\b/g, '[RECORD_ID]');
  const restricted = [
    /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i,
    /(?:^|[^\d])(?:\+?86[ -]?)?1[3-9]\d{9}(?:[^\d]|$)/,
    /(?:微信|wechat|QQ|学号|student\s*id)\s*[:：=]\s*[A-Z0-9_-]{5,}/i,
    /-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----/,
    /\b(?:sk|ghp|gho|github_pat)-?[A-Za-z0-9_]{20,}\b/,
    /(?:api[_ -]?key|password|passwd|secret|access[_ -]?token)\s*[:=]\s*["']?[A-Za-z0-9_+/.=-]{12,}/i
  ];
  requireThat(!restricted.some(pattern => pattern.test(text)), 'PERSONAL_DATA_RESTRICTED',
    'Input includes recognizable personal identifiers or credentials; submit a de-identified excerpt');
}

export function checkMaterialPath(path) {
  const normalized = path.replaceAll('\\', '/').toLowerCase();
  const protectedRepository = /(?:^|\/)personal-twin(?:\/|$)/.test(normalized)
    && !normalized.endsWith('/personal-twin/profile.md');
  requireThat(!protectedRepository
    && !/(?:^|\/)(?:\.env(?:\.[^/]*)?|secrets\.kdbx|contacts\.md)$/.test(normalized),
  'PERSONAL_DATA_RESTRICTED', 'Protected personal-data material cannot be imported into a domain library');
}
