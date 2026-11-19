import { readdir } from 'node:fs/promises';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { simulationRoot } from './build-packs.js';
const files = [];
async function walk(path) {
  for (const item of await readdir(path, { withFileTypes: true })) {
    if (['node_modules', '.local', '__pycache__', 'assets', 'legacy-assets'].includes(item.name)) continue;
    const full = join(path, item.name);
    if (item.isDirectory()) await walk(full);
    else if (item.name.endsWith('.js')) files.push(full);
  }
}
await walk(simulationRoot);
for (const file of files) {
  const checked = spawnSync(process.execPath, ['--check', file], { encoding: 'utf8', windowsHide: true });
  if (checked.status !== 0) { process.stderr.write(checked.stderr); process.exitCode = 1; }
}
console.log(`Checked ${files.length} JavaScript files`);
