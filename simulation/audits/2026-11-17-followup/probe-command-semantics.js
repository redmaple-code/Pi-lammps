import { readFile, writeFile } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';
import { checkLammpsInput } from '../../domains/lammps/checks.js';

const directory = fileURLToPath(new URL('.', import.meta.url));
const input = await readFile(new URL('./command-semantics.in', import.meta.url), 'utf8');
const executable = 'E:/LAMMPS 64-bit 2Aug2023-MSMPI/bin/lmp.exe';
const { stdout, stderr } = await promisify(execFile)(executable, ['-in', 'command-semantics.in', '-log', 'log.lammps'],
  { cwd: directory, timeout: 30000, windowsHide: true });
const checks = checkLammpsInput(input);
const samples = (await readFile(new URL('./variable-print.csv', import.meta.url), 'utf8')).trim().split(/\r?\n/)
  .slice(1).map(line => line.split(/\s+/).map(Number));
const thermo = stdout.split(/\r?\n/).filter(line => /^\s*\d+\s+/.test(line)).map(line => line.trim().split(/\s+/).map(Number))
  .filter(row => row.length === 4);
const result = { solverExitCode: 0, stderr, completion: stdout.includes('COMMAND_SEMANTICS_COMPLETE'),
  printSamples: samples, finalStrainRecord: thermo.at(-1),
  MB001: checks.find(item => item.checkId === 'MB-001'), MB005: checks.find(item => item.checkId === 'MB-005'),
  printVariableEvaluationCorrect: samples.length === 11 && samples.every(([step, time], index) => step === index && Math.abs(time - index * .001) < 1e-12),
  frozenReferenceCorrect: Math.abs(thermo.at(-1)[2] - 7.12) < 1e-12 && Math.abs(thermo.at(-1)[3] - .0001) < 1e-10 };
await writeFile(new URL('./command-semantics-result.json', import.meta.url), JSON.stringify(result, null, 2));
console.log(JSON.stringify(result, null, 2));
