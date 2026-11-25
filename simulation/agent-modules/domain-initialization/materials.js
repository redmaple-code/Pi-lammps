import { readFile, realpath, stat } from 'node:fs/promises';
import { extname, resolve } from 'node:path';
import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { requireThat } from '../core/index.js';
import { checkLibraryContent, checkMaterialPath } from './privacy.js';

const textTypes = new Set(['.txt', '.md', '.csv', '.json', '.jsonl', '.yaml', '.yml', '.log', '.inp']);
export const materialByteLimit = 8 * 1024 * 1024;

function extractPdf(bytes, python) {
  const script = 'import fitz,json,sys; d=fitz.open(stream=sys.stdin.buffer.read(),filetype="pdf"); print(json.dumps({"pages":[p.get_text() for p in d]}))';
  return new Promise((resolve, reject) => {
    const process = execFile(python, ['-c', script], { windowsHide: true, maxBuffer: 4 * 1024 * 1024, timeout: 60000 },
      (error, stdout) => error ? reject(error) : resolve(stdout));
    process.stdin.on('error', reject);
    process.stdin.end(bytes);
  });
}

export async function readLibraryMaterial(file, { python = 'python' } = {}) {
  const path = resolve(file);
  checkMaterialPath(path);
  const actual = await realpath(path);
  checkMaterialPath(actual);
  const info = await stat(actual);
  requireThat(info.isFile() && info.size <= materialByteLimit, 'SCHEMA_INVALID', 'Material must be a file of at most 8 MiB');
  return extractLibraryMaterial(await readFile(actual), { locator: actual, python });
}

export async function extractLibraryMaterial(bytes, { locator, python = 'python' }) {
  checkMaterialPath(locator);
  requireThat(bytes.length <= materialByteLimit, 'SCHEMA_INVALID', 'Material must be at most 8 MiB');
  const digest = `sha256:${createHash('sha256').update(bytes).digest('hex')}`;
  let content, coverage;
  if (extname(locator).toLowerCase() === '.pdf') {
    let pages;
    try { pages = JSON.parse(await extractPdf(bytes, python)).pages; }
    catch { requireThat(false, 'EXTRACTION_UNAVAILABLE', 'PDF text extraction needs Python and PyMuPDF; an excerpt can be entered directly'); }
    const readablePages = pages.map((text, index) => text.trim() ? index + 1 : null).filter(Boolean);
    requireThat(readablePages.length, 'EXTRACTION_UNAVAILABLE', 'PDF has no readable text; provide a verified transcript');
    content = pages.map((text, index) => `--- Page ${index + 1} ---\n${text}`).join('\n');
    coverage = { representation: readablePages.length === pages.length ? 'fullText' : 'section',
      totalPages: pages.length, readablePages, limitations: readablePages.length < pages.length ? ['Some pages have no extracted text.'] : [] };
  } else {
    requireThat(textTypes.has(extname(locator).toLowerCase()), 'EXTRACTION_UNAVAILABLE', 'Provide a text, CSV, JSON, YAML, log or PDF material');
    try { content = new TextDecoder('utf-8', { fatal: true }).decode(bytes); }
    catch { requireThat(false, 'EXTRACTION_UNAVAILABLE', 'Convert this material to UTF-8 before importing'); }
    coverage = { representation: 'fullText', limitations: [] };
  }
  requireThat(content.trim().length > 0 && content.length <= 262144, 'SCHEMA_INVALID', 'Import an excerpt of at most 262144 characters');
  checkLibraryContent(content);
  checkLibraryContent(locator);
  return { content, locator, sourceMetadata: { originalDigest: digest, byteLength: bytes.length, extractionCoverage: coverage } };
}
