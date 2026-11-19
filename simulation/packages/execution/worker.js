import { spawn } from 'node:child_process';
import { createWriteStream } from 'node:fs';
import { readFile, writeFile, rename, access } from 'node:fs/promises';
import { join } from 'node:path';
import { utc } from '../contracts/index.js';

const directory = process.argv[2];
const spec = JSON.parse(await readFile(join(directory, 'request.json'), 'utf8'));
async function save(value) {
  const temporary = join(directory, `receipt-${process.pid}.tmp`);
  await writeFile(temporary, JSON.stringify({ ...value, operationId: spec.operationId, workerPid: process.pid,
    workerToken: spec.workerToken, observedAt: utc() }, null, 2));
  await rename(temporary, join(directory, 'receipt.json'));
}
const startedAt = utc();
await save({ status: 'running', startedAt });
const stdout = createWriteStream(join(directory, 'stdout.log'), { flags: 'wx' });
const stderr = createWriteStream(join(directory, 'stderr.log'), { flags: 'wx' });
let child, timer, cancelRequested = false, timedOut = false;
async function stop() {
  if (!child?.pid) return;
  if (process.platform === 'win32') {
    await new Promise(done => { const killer = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' }); killer.on('error', done); killer.on('close', done); });
  } else {
    try { process.kill(-child.pid, 'SIGTERM'); } catch {}
    const force = setTimeout(() => { try { process.kill(-child.pid, 'SIGKILL'); } catch {} }, 2000);
    force.unref();
  }
}
try {
  child = spawn(spec.argv[0], spec.argv.slice(1), { cwd: join(directory, 'work'), shell: false,
    detached: process.platform !== 'win32', windowsHide: true,
    env: { ...process.env, ...spec.environment }, stdio: ['ignore', 'pipe', 'pipe'] });
  child.stdout.pipe(stdout); child.stderr.pipe(stderr);
  const cancellation = setInterval(async () => {
    try { await access(join(directory, 'cancel')); cancelRequested = true; await stop(); } catch {}
  }, 250);
  timer = setTimeout(() => { timedOut = true; void stop(); }, spec.timeoutMs);
  const outcome = await new Promise(resolve => {
    child.on('error', error => resolve({ code: null, error: error.message }));
    child.on('close', (code, signal) => resolve({ code, signal }));
  });
  clearInterval(cancellation); clearTimeout(timer);
  await Promise.all([new Promise(done => stdout.end(done)), new Promise(done => stderr.end(done))]);
  await save({ status: cancelRequested ? 'cancelled' : outcome.code === 0 ? 'succeeded' : 'failed', startedAt,
    endedAt: utc(), exitCode: outcome.code, signal: outcome.signal || null, error: outcome.error || null,
    timedOut, argv: spec.argv, workdir: join(directory, 'work') });
} catch (error) {
  await save({ status: 'unknown', startedAt, error: error.message });
  process.exitCode = 1;
}
