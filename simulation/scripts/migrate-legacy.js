import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { openApplication } from '../packages/application/index.js';
import { inventoryAssets, applyImport } from '../packages/migration/index.js';
import { loadOriginalRoles } from '../packages/runtime-redmaple/index.js';
import { simulationRoot } from './build-packs.js';

const sourceRoot = process.argv[2];
if (!sourceRoot) throw new Error('Usage node scripts/migrate-legacy.js SOURCE_ROOT');
const input = JSON.parse(await readFile(join(simulationRoot, '../docs/sources/source-asset-inventory.json'), 'utf8'));
const app = await openApplication({ workspace: simulationRoot });
try {
  const inventory = await inventoryAssets(sourceRoot, input.items.map(item => item.path), input.sourceCommit);
  const targetRoot = join(simulationRoot, 'domains/lammps/legacy-assets');
  const report = await applyImport({ sourceRoot, targetRoot, inventory, store: app.store, dryRun: false });
  const roles = await loadOriginalRoles(join(sourceRoot, '.angsheng/agents'), join(simulationRoot, 'domains/lammps/roles'));
  await writeFile(join(simulationRoot, 'domains/lammps/role-config.json'), JSON.stringify(roles, null, 2));
  await writeFile(join(simulationRoot, 'work/legacy-migration.json'), JSON.stringify({ ...report, roles: Object.keys(roles) }, null, 2));
  console.log(JSON.stringify({ imported: report.imported.length, missing: report.missing.length, roles: Object.keys(roles).length }));
} finally { app.close(); }
