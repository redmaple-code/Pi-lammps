import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { readFile, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { id, requireCondition } from '../contracts/index.js';
import { RedMapleRuntimePort } from '../runtime-redmaple/index.js';

export async function auditPaper(app, paperPath, config) {
  requireCondition(config?.model && config?.python, 'CAPABILITY_UNAVAILABLE', 'Paper reproduction audit requires configured Pi model and Python PDF extraction');
  const auditId = id('paper-audit'), directory = join(app.workspace, 'work', 'paper-audits', auditId);
  await mkdir(directory, { recursive: true });
  const root = fileURLToPath(new URL('../..', import.meta.url));
  const output = join(directory, 'full-text.txt');
  const { stdout } = await promisify(execFile)(config.python, [join(root, 'science/extract_paper.py'), paperPath, output],
    { windowsHide: true, timeout: 60000, maxBuffer: 1024 * 1024 });
  const extraction = JSON.parse(stdout);
  requireCondition(extraction.representation === 'fullText', 'EVIDENCE_INSUFFICIENT', 'Paper needs readable full text or OCR');
  const skillPath = join(root, 'domains/lammps/legacy-assets/legacy/skills/lammps-paper-reproduction-auditor/SKILL.md');
  const instructions = await readFile(skillPath, 'utf8');
  const runtime = new RedMapleRuntimePort({ store: app.store, workspace: app.workspace, agentDir: join(app.stateDirectory, 'paper-agents'),
    artifacts: app.artifacts, evidence: app.evidence,
    roles: { paperAuditor: { instructions, tools: ['read', 'grep', 'find', 'ls'], capabilities: ['artifact.write'] } }, options: config });
  const result = await runtime.executeRole({ role: 'paperAuditor', prompt: `Audit simulation reproducibility. Read the registered full text and publish a structured audit.\n${JSON.stringify(extraction)}`,
    sources: [{ sourceId: auditId, locator: output, sourceKind: 'literature', originGroupId: auditId }] }, auditId);
  app.store.put('paper-audits', auditId, { ...result, extraction, paperPath, scope: 'reproduce-audit' });
  let audit;
  for (const ref of result.outputRefs || []) {
    try {
      const candidate = JSON.parse((await app.artifacts.read(ref)).toString('utf8'));
      if (['ready-for-reproduction', 'reproducible-with-assumptions', 'partial-only', 'blocked'].includes(candidate.verdict)) { audit = candidate; break; }
    } catch (error) { if (!(error instanceof SyntaxError)) throw error; }
  }
  requireCondition(audit && result.evidenceRefs.length, 'EVIDENCE_INSUFFICIENT', 'Auditor must read the source and publish one of the four structured reproducibility verdicts');
  return { ...result, extraction, audit, downstreamEligible: ['ready-for-reproduction', 'reproducible-with-assumptions'].includes(audit.verdict) };
}
