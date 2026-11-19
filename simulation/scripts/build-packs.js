import { readFile, writeFile, mkdir, readdir } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { hashFile } from '../packages/artifacts/index.js';

export const simulationRoot = fileURLToPath(new URL('..', import.meta.url));
export async function buildPacks() {
  const stages = [
    ['wf-00', 'architect', [], true], ['wf-r', 'reasoner', ['wf-00'], false],
    ['wf-01', 'writer', ['wf-r'], true], ['wf-02', 'writer', ['wf-01'], true],
    ['wf-03a', 'writer', ['wf-02'], true], ['execute', 'executor', ['wf-03a'], false],
    ['wf-04', 'analyst', ['execute'], false], ['wf-05', 'post-processor', ['wf-04'], false]
  ];
  const basicChecks = stages.map(([stage]) => ({ id: stage, stage, check: 'artifact-and-evidence' }));
  const root = join(simulationRoot, 'domains', 'lammps');
  await mkdir(root, { recursive: true });
  const elements = ['Fe', 'Ni', 'Cr', 'Co', 'Cu'];
  const contents = {
    problem: { type: 'object', required: ['goal', 'features', 'criteria'], properties: { goal: { type: 'string' }, features: { type: 'array', minItems: 1 }, criteria: { type: 'array', minItems: 1 } }, additionalProperties: true },
    knowledge: { items: [{ id: 'local-indent', path: 'references/in.indent.lmp', kind: 'case' }, { id: 'model-source', doi: '10.1557/jmr.2018.245', kind: 'literature', representation: 'metadata-and-repository-description' }] },
    models: { items: [{ id: 'farkas-caro-2018', elements, pairStyle: 'eam/alloy', file: 'assets/FeNiCrCoCu-heafixed.setfl',
      source: 'https://www.ctcms.nist.gov/potentials/entry/2018--Farkas-D-Caro-A--Fe-Ni-Cr-Co-Cu/', limitations: ['Idealized FCC model alloy, not quantitatively fitted to all real elemental properties'] }] },
    vocabulary: { aliases: { grinding: ['磨削', 'scratch', '划擦'], HEA: ['高熵合金', 'FeNiCrCoCu'] } },
    checks: { items: [...basicChecks, ...Array.from({ length: 9 }, (_, i) => ({ id: `mb-${String(i + 1).padStart(3, '0')}`, action: 'blocked' }))] },
    metrics: { items: [{ id: 'integration-validation', requiredOutputs: ['log.lammps', 'grinding.csv', 'trajectory.lammpstrj', 'initial.lammpstrj', 'final.lammpstrj'] }] },
    failures: { items: [{ id: 'lost-atoms', pattern: 'Lost atoms', actor: 'analyst', rootCause: 'undetermined' }] },
    templates: { items: [{ id: 'grinding', implementation: 'science/grinding.py', reference: 'references/in.indent.lmp' }] },
    roles: { items: [...new Set(stages.map(stage => stage[1])), 'librarian', 'paper-researcher', 'kb-coordinator', 'kb-curator', 'kb-reviewer'].map(name => ({ id: name })) },
    validation: { items: [{ id: 'grinding', status: 'candidateValidation', method: 'finite-output-contact-fixed-layer' }] },
    artifacts: { items: stages.map(([stage]) => ({ id: stage.toLowerCase(), workflowId: 'grinding', slotName: `${stage}-output`, artifactType: 'json', schemaRef: 'problem', cardinality: 'one', semanticConstraints: {} })) }
  };
  const kinds = { problem: 'problemSchema', knowledge: 'knowledge', models: 'model', vocabulary: 'vocabulary', checks: 'check', metrics: 'metric', failures: 'failure', templates: 'template', roles: 'role', validation: 'validation', artifacts: 'artifactSchema' };
  const resources = [];
  for (const [name, content] of Object.entries(contents)) {
    const path = `${name}.json`;
    await writeFile(join(root, path), JSON.stringify(content, null, 2));
    resources.push({ id: name, kind: kinds[name], path, availability: 'available', digest: await hashFile(join(root, path)) });
  }
  for (const path of ['assets/FeNiCrCoCu-heafixed.setfl', ...(await readdir(join(root, 'references'))).map(name => `references/${name}`)]) {
    resources.push({ id: `asset-${resources.length}`, kind: path.startsWith('assets/') ? 'model' : 'knowledge', path,
      availability: 'available', digest: await hashFile(join(root, path)) });
  }
  const manifest = {
    schemaVersion: '1.0.0', id: 'lammps-hea', version: '0.1.0', publicationStatus: 'draft',
    scope: { domains: ['molecular-dynamics'], scales: ['microscopic'], tasks: ['grinding'], exclusions: ['quantitative-production-grinding-prediction'] },
    kernelCompatibility: '>=1.0.0 <2.0.0', dependencies: [], resources,
    entrypoints: { problemSchema: 'problem', knowledge: ['knowledge'], models: ['models'], vocabulary: 'vocabulary', checks: ['checks'],
      metrics: ['metrics'], failures: 'failures', templates: ['templates'], roles: 'roles', validation: ['validation'], artifactSchemas: ['artifacts'] },
    requiredCapabilities: ['molecular.dynamics.solve'], defaultToolProfile: 'local-validation',
    toolProfiles: [{ id: 'local-validation', bindings: [{ capability: 'molecular.dynamics.solve', adapterId: 'local-lammps',
      adapterVersionRange: '>=0.1.0 <1.0.0', toolVersionRange: '*', validationStatus: 'unverified' }] }],
    defaultWorkflow: 'grinding', workflows: [{ id: 'grinding', taskType: 'grinding', inputs: ['request'], outputs: ['wf-05-output'],
      stages: stages.map(([stage, role, needs, reviewRequired]) => ({ id: stage, kind: 'action', role, needs,
        consumes: needs.length ? needs.map(key => `${key}-output`) : ['request'], produces: [`${stage}-output`],
        checkRefs: [`checks#${stage}`], reviewRequired,
        capability: 'molecular.dynamics.solve' })) }],
    policies: { referencePolicy: 'localCaseRequired', independentReviewRequired: true, maxConsecutiveRevisions: 3, maxConsecutiveRepairFailures: 3 }
  };
  await writeFile(join(root, 'manifest.json'), JSON.stringify(manifest, null, 2));
  return manifest;
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) console.log((await buildPacks()).id);
