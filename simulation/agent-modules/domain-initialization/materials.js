import { readFile, realpath, stat } from 'node:fs/promises';
import { extname, resolve } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createHash } from 'node:crypto';
import { requireThat } from '../core/index.js';
import { checkLibraryContent, checkMaterialPath } from './privacy.js';

const run = promisify(execFile);
const textTypes = new Set(['.txt', '.md', '.csv', '.json', '.jsonl', '.yaml', '.yml', '.log', '.inp']);

export async function readLibraryMaterial(file, { python = 'python' } = {}) {
  const path = resolve(file);
  checkMaterialPath(path);
  const actual = await realpath(path);
  checkMaterialPath(actual);
  const info = await stat(actual);
  requireThat(info.isFile() && info.size <= 8 * 1024 * 1024, 'SCHEMA_INVALID', 'Material must be a file of at most 8 MiB');
  const bytes = await readFile(actual);
  const digest = `sha256:${createHash('sha256').update(bytes).digest('hex')}`;
  let content, coverage;
  if (extname(actual).toLowerCase() === '.pdf') {
    const script = 'import fitz,json,sys; d=fitz.open(sys.argv[1]); pages=[p.get_text() for p in d]; print(json.dumps({"pages":pages},ensure_ascii=False))';
    let result;
    try { result = await run(python, ['-c', script, actual], { windowsHide: true, maxBuffer: 4 * 1024 * 1024, timeout: 60000 }); }
    catch { requireThat(false, 'EXTRACTION_UNAVAILABLE', 'PDF text extraction needs Python and PyMuPDF; an excerpt can be entered directly'); }
    const pages = JSON.parse(result.stdout).pages;
    const readablePages = pages.map((text, index) => text.trim() ? index + 1 : null).filter(Boolean);
    requireThat(readablePages.length, 'EXTRACTION_UNAVAILABLE', 'PDF has no readable text; provide a verified transcript');
    content = pages.map((text, index) => `--- Page ${index + 1} ---\n${text}`).join('\n');
    coverage = { representation: readablePages.length === pages.length ? 'fullText' : 'section',
      totalPages: pages.length, readablePages, limitations: readablePages.length < pages.length ? ['Some pages have no extracted text.'] : [] };
  } else {
    requireThat(textTypes.has(extname(actual).toLowerCase()), 'EXTRACTION_UNAVAILABLE', 'Provide a text, CSV, JSON, YAML, log or PDF material');
    try { content = new TextDecoder('utf-8', { fatal: true }).decode(bytes); }
    catch { requireThat(false, 'EXTRACTION_UNAVAILABLE', 'Convert this material to UTF-8 before importing'); }
    coverage = { representation: 'fullText', limitations: [] };
  }
  requireThat(content.trim().length > 0 && content.length <= 262144, 'SCHEMA_INVALID', 'Import an excerpt of at most 262144 characters');
  checkLibraryContent(content);
  checkLibraryContent(path);
  return { content, locator: path, sourceMetadata: { originalDigest: digest, byteLength: info.size, extractionCoverage: coverage } };
}
