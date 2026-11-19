// Build four self-contained local distributions from the reviewed source modules.
// Never delete or overwrite an existing destination: output is immutable per build.
import { mkdir, copyFile, writeFile } from 'node:fs/promises';
import { resolve, join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const destination = resolve(process.argv[2] || join(root, 'dist', 'v0.1.0'));
await mkdir(destination, { recursive: false });
for (const name of ['knowledge','evidence','gate','consistency']) {
  const dir = join(destination, name);
  await mkdir(join(dir, 'core'), { recursive: true });
  await mkdir(join(dir, name));
  for (const file of ['index.js', 'audit-sqlite.js']) await copyFile(join(root, 'core', file), join(dir, 'core', file));
  await copyFile(join(root, name, 'index.js'), join(dir, name, 'index.js'));
  const pkg = { name: `@redmaple/agent-${name}`, version: '0.1.0', private: true, type: 'module',
    engines: { node: '>=22.19.0' }, files: ['core', name, 'README.md'],
    exports: { '.': `./${name}/index.js`, './core': './core/index.js', './audit-sqlite': './core/audit-sqlite.js' } };
  await writeFile(join(dir, 'package.json'), JSON.stringify(pkg, null, 2) + '\n', { flag: 'wx' });
  await copyFile(join(root, 'README.md'), join(dir, 'README.md'));
}
console.log(`Built four independent packages in ${destination}`);
