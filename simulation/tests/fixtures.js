import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { SimulationStore } from '../packages/kernel/state.js';
import { ArtifactStore } from '../packages/artifacts/index.js';
import { EvidenceService } from '../packages/kernel/evidence.js';
import { GateService } from '../packages/kernel/gates.js';
import { Kernel } from '../packages/kernel/index.js';
import { serviceActor, digest } from '../packages/contracts/index.js';
export async function fixture(t, { review = true, stages } = {}) {
  const root = await mkdtemp(join(tmpdir(), 'redmaple-simulation-'));
  const store = new SimulationStore(join(root, 'state.sqlite'));
  t.after(() => store.close());
  const artifacts = new ArtifactStore(join(root, 'artifacts'), store);
  const evidence = new EvidenceService(store), gates = new GateService(store, artifacts, evidence), kernel = new Kernel(store, gates);
  const lock = { digest: digest({ test: 1 }), policies: { maxConsecutiveRevisions: 3, maxConsecutiveRepairFailures: 3 } };
  const state = store.createCase({ title: 'Test case', lock, workflow: { stages: stages || [
    { id: 'produce', needs: [], reviewRequired: review, requiredCheckIds: ['CHECK-1'], requiredForExecution: true },
    { id: 'solve', needs: ['produce'], reviewRequired: false }
  ] } });
  return { root, store, artifacts, evidence, gates, kernel, state,
    writer: serviceActor('writer'), reviewer: { ...serviceActor('reviewer'), capabilities: ['review.submit'] } };
}
