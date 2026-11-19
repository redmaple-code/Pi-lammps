import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdir, copyFile, readFile, writeFile, access } from 'node:fs/promises';
import { join } from 'node:path';
import { requireCondition, utc } from '../contracts/index.js';

const execute = promisify(execFile);
export function modellingRoute(spec) {
  if (spec.source?.toLowerCase().endsWith('.cif')) return { route: 'cif', reason: 'Crystallographic source supplied' };
  if (spec.source && spec.editOperations) return { route: 'edit', reason: 'Source plus explicitly requested structure edit' };
  if (spec.source) return { route: 'reuse', reason: 'Existing structure baseline' };
  if (spec.grains > 1) return { route: 'polycrystal', reason: 'Multiple grains explicitly requested' };
  return { route: 'crystal', reason: 'Crystal lattice and cell are explicitly specified' };
}
export async function buildStructure(spec, { atomsk, directory, artifacts }) {
  await mkdir(directory, { recursive: true });
  const selected = modellingRoute(spec), output = join(directory, 'structure.lmp');
  try { await access(output); throw new Error('Structure target already exists. Use a new attempt directory.'); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  const calls = [];
  async function run(args) {
    const result = await execute(atomsk, args, { cwd: directory, windowsHide: true, timeout: 120000, maxBuffer: 4 * 1024 * 1024 });
    calls.push({ argv: [atomsk, ...args], stdout: result.stdout, stderr: result.stderr, completedAt: utc() });
  }
  if (selected.route === 'reuse') await copyFile(spec.source, output, 1);
  else if (selected.route === 'cif') {
    await run([spec.source, '-orthogonal-cell', 'converted.xsf']);
    await run(['converted.xsf', '-alignx', '-unskew', 'structure.lmp']);
  } else if (selected.route === 'edit') {
    requireCondition(spec.editOperations.every(value => typeof value === 'string') && spec.editOperations.length > 0, 'SCHEMA_INVALID', 'Edit arguments must be explicit');
    await run([spec.source, ...spec.editOperations, 'structure.lmp']);
  } else {
    requireCondition(['fcc', 'bcc', 'hcp'].includes(spec.lattice) && /^[A-Z][a-z]?$/.test(spec.element), 'SCHEMA_INVALID', 'Specify supported lattice and element');
    requireCondition(Number.isFinite(spec.latticeAngstrom) && spec.latticeAngstrom > 0, 'SCHEMA_INVALID', 'Positive lattice parameter required');
    if (spec.lattice === 'hcp') requireCondition(Number.isFinite(spec.cAngstrom) && spec.cAngstrom > 0, 'SCHEMA_INVALID', 'HCP construction requires c lattice parameter');
    const create = ['--create', spec.lattice, String(spec.latticeAngstrom), ...(spec.lattice === 'hcp' ? [String(spec.cAngstrom)] : []), spec.element];
    if (selected.route === 'polycrystal') {
      requireCondition(spec.boxAngstrom?.length === 3 && spec.boxAngstrom.every(value => value > 0) && Number.isInteger(spec.grains) && spec.grains > 1, 'SCHEMA_INVALID', 'Polycrystal needs box and grain count');
      await run([...create, 'seed.xsf']);
      await writeFile(join(directory, 'poly.txt'), `box ${spec.boxAngstrom.join(' ')}\nrandom ${spec.grains}\n`);
      await run(['--polycrystal', 'seed.xsf', 'poly.txt', 'polycrystal.cfg', 'lmp', '-wrap']);
      await copyFile(join(directory, 'polycrystal.lmp'), output, 1);
    } else {
      requireCondition(spec.cells?.length === 3 && spec.cells.every(value => Number.isInteger(value) && value > 0), 'SCHEMA_INVALID', 'Positive integer cell counts required');
      await run([...create, '-duplicate', ...spec.cells.map(String), 'structure.lmp']);
    }
  }
  const text = await readFile(output, 'utf8');
  const atomCount = Number(text.match(/(\d+)\s+atoms/)?.[1]);
  requireCondition(atomCount > 0 && /Atoms/.test(text), 'SCIENTIFIC_CHECK_FAILED', 'Builder did not produce valid atomic data');
  const ref = await artifacts.publish(output, { type: 'structure', route: selected.route });
  return { ...selected, atomCount, outputRef: ref, calls, verificationScope: 'format and count; critical features require independent domain review' };
}
