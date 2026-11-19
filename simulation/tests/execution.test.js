import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { fixture } from './fixtures.js';
import { id } from '../packages/contracts/index.js';
import { recordAuthorization } from '../packages/kernel/authorization.js';
import { LocalExecutor } from '../packages/execution/index.js';
import { fileURLToPath } from 'node:url';

async function ready(t, code, timeoutMs = 10000) {
  const f = await fixture(t, { review: false });
  const script = await f.artifacts.text(code, { type: 'script' });
  await f.kernel.command(f.state.id, 'start', { stageId: 'produce', producerInstanceId: f.writer.actorInstanceId });
  f.store.put('completions', 'complete', { id: 'complete' }, 0);
  await f.kernel.command(f.state.id, 'produced', { stageId: 'produce', outputRefs: [script], completionReceiptRef: 'complete' });
  const closure = await f.artifacts.closure([{ path: 'input.cjs', ref: script }]);
  const grant = recordAuthorization(f.store, { source: 'test user request', actions: ['solve'], caseId: f.state.id, resources: { wallSeconds: 20 } },
    { actorId: 'test-user', actorInstanceId: id('human'), kind: 'human' });
  const permit = f.kernel.permit(f.state.id, { closure, authorizationRef: grant.id, resources: { wallSeconds: 20 } });
  const spec = { argv: [process.execPath, 'input.cjs'], timeoutMs, inputClosure: closure, permitRef: permit.id };
  const operationId = id('op');
  await f.kernel.command(f.state.id, 'start', { stageId: 'solve', producerInstanceId: 'executor', request: spec, operationId });
  const executor = new LocalExecutor({ root: join(f.root, 'jobs'), ...f });
  return { ...f, executor, spec, operationId };
}
test('persistent supervisor runs real child, collects bytes and duplicate submit reuses receipt', async t => {
  const f = await ready(t, "require('node:fs').writeFileSync('result.bin',Buffer.from([0,255,17]));console.log('actual subprocess');");
  await f.executor.submit(f.spec, f.operationId);
  const receipt = await f.executor.wait(f.operationId);
  assert.equal(receipt.status, 'succeeded');
  const repeated = await f.executor.submit(f.spec, f.operationId);
  assert.equal(repeated.workerToken, receipt.workerToken);
  const run = await f.executor.collect(f.operationId, [{ path: 'result.bin' }]);
  assert.equal(run.status, 'completed');
  assert.deepEqual(await f.artifacts.read(run.outputRefs[0]), Buffer.from([0,255,17]));
});
test('cancellation and timeout wait for actual process termination', async t => {
  const f = await ready(t, "console.log('started');setInterval(()=>{},1000)");
  await f.executor.submit(f.spec, f.operationId);
  await f.executor.cancel(f.operationId);
  assert.equal((await f.executor.wait(f.operationId)).status, 'cancelled');
  const g = await ready(t, 'setInterval(()=>{},1000)', 200);
  await g.executor.submit(g.spec, g.operationId);
  const timed = await g.executor.wait(g.operationId);
  assert.equal(timed.status, 'failed');
  assert.equal(timed.timedOut, true);
});
test('missing result and altered inputs are refused', async t => {
  const f = await ready(t, 'console.log("no output")');
  await f.executor.submit(f.spec, f.operationId);
  await f.executor.wait(f.operationId);
  await assert.rejects(f.executor.collect(f.operationId, [{ path: 'missing.out' }]), { code: 'OUTPUT_MISSING' });
  const altered = structuredClone(f.spec); altered.inputClosure.digest = 'wrong';
  await assert.rejects(f.executor.preflight(altered), { code: 'ARTIFACT_CHANGED' });
});

test('execution refuses unreviewed closure bytes, stale digests, excess time and cancelled permits', async t => {
  const f = await ready(t, 'console.log("approved")');
  const newRef = await f.artifacts.text('console.log("unreviewed")');
  const changed = await f.artifacts.closure([{ path: 'input.cjs', ref: newRef }]);
  const permit = f.store.get('permits', f.spec.permitRef);
  assert.throws(() => f.kernel.permit(f.state.id, { closure: changed, authorizationRef: permit.authorizationRef, resources: permit.resources }), { code: 'REVIEW_REQUIRED' });
  const forged = structuredClone(f.spec);
  forged.inputClosure.entries[0].ref = newRef;
  await assert.rejects(f.executor.preflight(forged), { code: 'ARTIFACT_CHANGED' });
  await assert.rejects(f.executor.preflight({ ...f.spec, timeoutMs: 21000 }), { code: 'RESOURCE_LIMIT' });
  await f.kernel.command(f.state.id, 'cancel');
  await assert.rejects(f.executor.preflight(f.spec), { code: 'STATE_CONFLICT' });
});
test('worker survives submitting process exit and a reopened executor reconciles the same operation', async t => {
  const f = await ready(t, "setTimeout(()=>require('node:fs').writeFileSync('once.txt','one confirmed effect'),400)");
  const storeModule = new URL('../packages/kernel/state.js', import.meta.url).href;
  const artifactsModule = new URL('../packages/artifacts/index.js', import.meta.url).href;
  const executorModule = new URL('../packages/execution/index.js', import.meta.url).href;
  const code = `import {SimulationStore} from ${JSON.stringify(storeModule)};
    import {ArtifactStore} from ${JSON.stringify(artifactsModule)};
    import {LocalExecutor} from ${JSON.stringify(executorModule)};
    const store=new SimulationStore(${JSON.stringify(f.store.file)});
    const artifacts=new ArtifactStore(${JSON.stringify(join(f.root,'artifacts'))},store);
    const executor=new LocalExecutor({root:${JSON.stringify(f.root)},store,artifacts,kernel:{validatePermit(){return {id:'test'};}}});
    await executor.submit(${JSON.stringify(f.spec)},${JSON.stringify(f.operationId)});store.close();`;
  execFileSync(process.execPath, ['--input-type=module', '-e', code], { windowsHide: true });
  const receipt = await f.executor.wait(f.operationId);
  assert.equal(receipt.status, 'succeeded');
  assert.equal((await f.executor.collect(f.operationId, [{ path: 'once.txt' }])).outputRefs[0].byteLength, 20);
  assert.equal((await f.executor.submit(f.spec, f.operationId)).workerToken, receipt.workerToken);
});
