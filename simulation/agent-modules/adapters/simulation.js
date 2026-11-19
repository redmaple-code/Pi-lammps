import { createKnowledgeModule } from '../knowledge/index.js';
import { createEvidenceModule } from '../evidence/index.js';
import { createGateModule } from '../gate/index.js';
import { createConsistencyModule } from '../consistency/index.js';
import { createDomainInitializationModule } from '../domain-initialization/index.js';
import { checkLammpsInput } from '../../domains/lammps/checks.js';

export const LAMMPS_CHECKS = Object.freeze(['MB-001','MB-002','MB-003','MB-004','MB-005',
  'MB-006','MB-007','MB-008','MB-009','OUTPUT-CLOSURE']);

// Register only capabilities whose initialized backends are supplied by the host.
export function installSimulationModules(host, { knowledge, evidence, resolveEvidenceSource, gate, domainInitialization, lammps = true } = {}) {
  if (knowledge) host.register(createKnowledgeModule({ backend: knowledge }));
  if (evidence) host.register(createEvidenceModule({ backend: evidence, resolveSource: resolveEvidenceSource }));
  if (gate) host.register(createGateModule({ backend: gate }));
  if (domainInitialization) host.register(createDomainInitializationModule({ service: domainInitialization.service || domainInitialization }));
  if (lammps) host.register(createConsistencyModule({ inspect: checkLammpsInput,
    checkSetVersion: 'simulation-lammps-mb/1', requiredCheckIds: LAMMPS_CHECKS }));
  return host;
}
