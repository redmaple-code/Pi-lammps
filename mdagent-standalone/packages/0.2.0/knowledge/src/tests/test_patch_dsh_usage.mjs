// scripts/patch_dsh_usage.mjs regression: patch once, stay idempotent.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = dirname(HERE);
const SCRIPT = join(REPO, 'scripts', 'patch_dsh_usage.mjs');
const ORIGINAL = 'function mapUsage(usage) {\n\treturn {\n\t\tinputTokens: usage.input,\n'
    + '\t\toutputTokens: usage.output\n\t};\n}\n';

test('patches once and is idempotent', () => {
    const root = mkdtempSync(join(tmpdir(), 'patch-usage-'));
    const path = join(root, 'lib', 'index.js');
    try {
        mkdirSync(dirname(path), { recursive: true });
        writeFileSync(path, ORIGINAL, 'utf-8');
        execFileSync(process.execPath, [SCRIPT, '--path', path, '--apply'], { stdio: 'pipe' });
        const patched = readFileSync(path, 'utf-8');
        assert.match(patched, /inputTokens: Math\.max\(0, \(usage\.input \?\? 0\) - \(usage\.cacheRead \?\? 0\)\)/);
        assert.ok(existsSync(path + '.bak-20261116'), 'backup missing');
        execFileSync(process.execPath, [SCRIPT, '--path', path, '--apply'], { stdio: 'pipe' });
        assert.equal(readFileSync(path, 'utf-8'), patched, 'second apply changed the file');
    } finally {
        rmSync(root, { recursive: true, force: true });
    }
});

test('check mode fails on an unpatched file', () => {
    const root = mkdtempSync(join(tmpdir(), 'patch-usage-check-'));
    const path = join(root, 'lib', 'index.js');
    try {
        mkdirSync(dirname(path), { recursive: true });
        writeFileSync(path, ORIGINAL, 'utf-8');
        assert.throws(() => execFileSync(process.execPath, [SCRIPT, '--path', path, '--check'],
                                         { stdio: 'pipe' }));
    } finally {
        rmSync(root, { recursive: true, force: true });
    }
});
