#!/usr/bin/env node
// Patch the dsh pi-ai usage mapping so cached prompt tokens are not counted twice.
//
//   node scripts/patch_dsh_usage.mjs --check         # exit 1 when unpatched
//   node scripts/patch_dsh_usage.mjs --apply         # backup + patch in place
//   node scripts/patch_dsh_usage.mjs --path <lib/index.js> [--apply]
//
// Why: relay endpoints compatible with OpenAI report input_tokens INCLUDING the
// cached part. The harness sums disjoint buckets (input + cacheRead + output),
// so mapping inputTokens: usage.input double-counts and compaction fires every
// few steps. Correct mapping subtracts cacheRead.
import {
    copyFileSync, existsSync, readFileSync, readdirSync, realpathSync, statSync, writeFileSync,
} from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';

const argv = process.argv.slice(2);
const flag = (name) => argv.includes(name);
const value = (name) => {
    const index = argv.indexOf(name);
    return index >= 0 ? argv[index + 1] : null;
};

const PATCHED = /inputTokens:\s*Math\.max\(0,\s*\(usage\.input/;
const TARGET = /\binputTokens:\s*usage\.input\b/;

function findPackages(root, depth) {
    if (depth < 0 || !existsSync(root)) return [];
    const out = [];
    let entries = [];
    try {
        entries = readdirSync(root);
    } catch {
        return out;
    }
    for (const name of entries) {
        const path = join(root, name);
        let info;
        try {
            info = statSync(path);
        } catch {
            continue;
        }
        if (!info.isDirectory()) continue;
        if (name === 'dsh-llm-pi-ai') {
            const candidate = join(path, 'lib', 'index.js');
            if (existsSync(candidate)) out.push(candidate);
        }
        out.push(...findPackages(path, depth - 1));
    }
    return out;
}

function discover() {
    const explicit = value('--path');
    if (explicit) return [resolve(explicit)];
    const roots = [
        join(homedir(), '.dsh', 'profiles'),
        join(process.env.APPDATA || '', 'npm', 'node_modules'),
        join(process.env.LOCALAPPDATA || '', 'dsh'),
        join(process.env.ProgramFiles || '', 'ZCode', 'resources'),
    ].filter(Boolean);
    const found = [];
    for (const root of roots) found.push(...findPackages(root, 7));
    return [...new Set(found.map((path) => {
        try {
            return realpathSync(path);
        } catch {
            return path;
        }
    }))];
}

function patchOne(path, apply) {
    const text = readFileSync(path, 'utf-8');
    if (PATCHED.test(text)) {
        console.log('[skip] already patched:', path);
        return 'patched';
    }
    if (!TARGET.test(text)) {
        console.log('[warn] no inputTokens: usage.input found:', path);
        return 'unknown';
    }
    if (!apply) {
        console.log('[need] unpatched usage mapping:', path);
        return 'unpatched';
    }
    const backup = path + '.bak-20261116';
    if (!existsSync(backup)) copyFileSync(path, backup);
    const next = text.replace(TARGET,
        'inputTokens: Math.max(0, (usage.input ?? 0) - (usage.cacheRead ?? 0))');
    writeFileSync(path, next, 'utf-8');
    console.log('[patched]', path, 'backup:', backup);
    return 'patched';
}

const targets = discover();
if (targets.length === 0) {
    console.error('[error] dsh-llm-pi-ai/lib/index.js not found; pass --path');
    process.exit(2);
}
const results = targets.map((path) => patchOne(path, flag('--apply')));
if (flag('--check') && results.includes('unpatched')) process.exit(1);
