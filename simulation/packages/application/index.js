import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { mkdir, access } from 'node:fs/promises';
import { digest } from '../contracts/index.js';
import { SimulationStore } from '../kernel/state.js';
import { ArtifactStore } from '../artifacts/index.js';
import { EvidenceService } from '../kernel/evidence.js';
import { GateService } from '../kernel/gates.js';
import { Kernel } from '../kernel/index.js';
import { KnowledgeService } from '../knowledge/index.js';
import { ImprovementService } from '../improvement/index.js';
import { LocalExecutor } from '../execution/index.js';
import { LegacyIndexBackend } from '../knowledge/legacy-index.js';
import { openDomainInitialization } from '../../agent-modules/domain-initialization/host.js';

export async function openApplication({ workspace = process.cwd(), stateDirectory, artifactDirectory } = {}) {
  workspace = resolve(workspace);
  const workspaceId = digest(workspace.toLowerCase()).slice(7, 23);
  stateDirectory ||= join(process.env.LOCALAPPDATA || join(homedir(), '.local', 'state'), 'RedMaple', 'simulation', workspaceId);
  artifactDirectory ||= join(workspace, 'work', 'artifacts');
  await mkdir(stateDirectory, { recursive: true });
  const store = new SimulationStore(join(stateDirectory, 'simulation.sqlite'));
  const artifacts = new ArtifactStore(artifactDirectory, store);
  const evidence = new EvidenceService(store);
  const gates = new GateService(store, artifacts, evidence);
  const kernel = new Kernel(store, gates);
  const knowledge = await KnowledgeService.open({ file: join(stateDirectory, 'knowledge.sqlite'), workspace, store, evidence,
    aliases: { grinding: ['磨削', '划擦'], hea: ['高熵合金', 'FeNiCrCoCu'] } });
  const improvement = new ImprovementService({ store, root: join(workspace, 'work', 'improvement') });
  const executor = new LocalExecutor({ root: join(workspace, 'work', 'jobs'), store, artifacts, kernel });
  const domainInitialization = await openDomainInitialization({ directory: join(stateDirectory, 'domain-libraries') });
  let legacy;
  const legacyPath = join(workspace, 'work', 'recovered-source', 'knowledge.sqlite');
  try { await access(legacyPath); legacy = new LegacyIndexBackend(legacyPath); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  return { workspace, workspaceId, stateDirectory, store, artifacts, evidence, gates, kernel, knowledge, legacy, improvement, executor, domainInitialization,
    close() { legacy?.close(); knowledge.close(); store.close(); } };
}
