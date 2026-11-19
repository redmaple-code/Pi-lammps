import { spawn } from 'node:child_process';
import { mkdir, readFile, writeFile, access, open } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { hostname } from 'node:os';
import { digest, id, utc, requireCondition } from '../contracts/index.js';
import { hashFile, scopedPath } from '../artifacts/index.js';
import { validateRecord } from '../contracts/records.js';

const terminal = new Set(['succeeded', 'failed', 'cancelled']);
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
export class LocalExecutor {
  constructor({ root, store, artifacts, kernel }) { Object.assign(this, { root: resolve(root), store, artifacts, kernel }); }
  directory(operationId) {
    requireCondition(/^[a-zA-Z0-9._-]+$/.test(operationId), 'SCHEMA_INVALID', 'Invalid operation identity');
    return join(this.root, operationId);
  }
  async preflight(spec) {
    validateRecord('RunSpec', spec);
    requireCondition(Array.isArray(spec.argv) && spec.argv.length > 0 && spec.argv.every(arg => typeof arg === 'string'), 'SCHEMA_INVALID', 'argv must be a string array');
    requireCondition(Number.isFinite(spec.timeoutMs) && spec.timeoutMs > 0, 'RESOURCE_LIMIT', 'Positive timeout is required');
    const permit = this.kernel.validatePermit(spec.permitRef, spec.inputClosure);
    requireCondition(!permit.resources?.wallSeconds || spec.timeoutMs <= permit.resources.wallSeconds * 1000,
      'RESOURCE_LIMIT', 'Execution timeout exceeds the authorized wall time');
    for (const entry of spec.inputClosure.entries) await this.artifacts.verify(entry.ref);
    const executable = resolve(spec.argv[0]);
    await access(executable);
    const executableDigest = await hashFile(executable);
    return { status: 'passed', permitRef: permit.id, executable, executableDigest, inputDigest: spec.inputClosure.digest, checkedAt: utc() };
  }
  async submit(spec, operationId) {
    const operation = this.store.operation(operationId);
    requireCondition(operation && digest(operation.request) === digest(spec), 'IDEMPOTENCY_CONFLICT', 'Operation request differs');
    if (operation.status !== 'intent') return this.reconcile(operationId);
    const preflight = await this.preflight(spec);
    const directory = this.directory(operationId);
    await mkdir(directory, { recursive: true });
    this.store.claim(operationId, { host: hostname(), pid: process.pid, token: id('owner') });
    try {
      await this.artifacts.materialize(spec.inputClosure, join(directory, 'work'));
      const workerToken = id('worker');
      const request = { operationId, workerToken, argv: [preflight.executable, ...spec.argv.slice(1)],
        timeoutMs: spec.timeoutMs, environment: spec.environment || {}, preflight };
      await writeFile(join(directory, 'request.json'), JSON.stringify(request, null, 2), { flag: 'wx' });
      const supervisorLog = await open(join(directory, 'supervisor.log'), 'ax');
      let child;
      try {
        child = spawn(process.execPath, [fileURLToPath(new URL('./worker.js', import.meta.url)), directory],
          { detached: true, windowsHide: true, stdio: ['ignore', supervisorLog.fd, supervisorLog.fd] });
        await new Promise((resolve, reject) => { child.once('spawn', resolve); child.once('error', reject); });
      } finally { await supervisorLog.close(); }
      this.store.put('workers', operationId, { pid: child.pid, host: hostname(), token: workerToken }, 0);
      child.unref();
      return { status: 'running', operationId, directory, observedAt: utc() };
    } catch (error) {
      const receipt = { status: 'unknown', operationId, error: error.message, observedAt: utc() };
      this.store.settle(operationId, receipt);
      return receipt;
    }
  }
  async reconcile(operationId) {
    const operation = this.store.operation(operationId);
    requireCondition(operation, 'REFERENCE_MISSING', 'Unknown operation');
    if (terminal.has(operation.status)) return operation.receipt;
    try {
      const directory = this.directory(operationId);
      const request = JSON.parse(await readFile(join(directory, 'request.json'), 'utf8'));
      const receipt = JSON.parse(await readFile(join(directory, 'receipt.json'), 'utf8'));
      requireCondition(receipt.workerToken === request.workerToken && receipt.operationId === operationId, 'IDENTITY_REQUIRED', 'Worker receipt identity differs');
      if (terminal.has(receipt.status)) return this.store.settle(operationId, receipt).receipt;
      try { process.kill(receipt.workerPid, 0); }
      catch { return { ...receipt, status: 'unknown', error: 'Supervisor is not alive. Inspect external process before retry.' }; }
      return receipt;
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      const worker = this.store.get('workers', operationId);
      if (worker?.host === hostname()) {
        try { process.kill(worker.pid, 0); return { status: 'running', operationId, observedAt: utc(), phase: 'workerStarting' }; }
        catch {}
      }
      return { status: operation.status === 'intent' ? 'intent' : 'unknown', operationId, error: 'No confirmed worker receipt' };
    }
  }
  async wait(operationId, { signal, onUpdate, intervalMs = 300 } = {}) {
    let lastStatus;
    while (true) {
      if (signal?.aborted) await this.cancel(operationId);
      const receipt = await this.reconcile(operationId);
      if (receipt.status !== lastStatus) { onUpdate?.(receipt); lastStatus = receipt.status; }
      if (terminal.has(receipt.status) || receipt.status === 'unknown') return receipt;
      await delay(intervalMs);
    }
  }
  async cancel(operationId) {
    const operation = this.store.operation(operationId);
    requireCondition(operation, 'REFERENCE_MISSING', 'Operation does not exist');
    if (operation.status === 'intent') return this.store.settle(operationId, { status: 'cancelled', operationId, reason: 'Not dispatched', observedAt: utc() }).receipt;
    if (terminal.has(operation.status)) return operation.receipt;
    await writeFile(join(this.directory(operationId), 'cancel'), utc());
    return { status: 'cancelRequested', operationId, observedAt: utc() };
  }
  async collect(operationId, outputs) {
    const receipt = await this.reconcile(operationId);
    requireCondition(receipt.status === 'succeeded', 'TOOL_FAILED', `External operation ${receipt.status}`, { sideEffectState: receipt.status === 'unknown' ? 'unknown' : 'confirmed' });
    const directory = this.directory(operationId), refs = [], missing = [];
    for (const output of outputs) {
      try {
        const path = await scopedPath(join(directory, 'work'), output.path);
        refs.push(await this.artifacts.publish(path, { type: output.type || 'file', operationId, name: output.path }));
      } catch (error) {
        if (error.code !== 'ENOENT') throw error;
        missing.push(output.path);
      }
    }
    requireCondition(missing.length === 0, 'OUTPUT_MISSING', `Missing run outputs ${missing.join(', ')}`);
    for (const name of ['stdout.log', 'stderr.log']) refs.push(await this.artifacts.publish(join(directory, name), { type: 'log', operationId, name }));
    return { ...receipt, id: id('run'), operationId, status: 'completed', executionMode: 'real', outputRefs: refs, collectedAt: utc() };
  }
}
