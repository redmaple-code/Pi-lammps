import { join } from 'node:path';
import { id, digest, requireCondition } from '../contracts/index.js';
import { hashFile } from '../artifacts/index.js';

export const conditionalRoles = {
  research: ['lammps-case-librarian', 'lammps-paper-researcher'],
  failure: ['lammps-analyst', 'lammps-simulation-reasoner'],
  knowledge: ['lammps-kb-coordinator', 'lammps-kb-curator', 'lammps-kb-reviewer']
};

export async function supportRole(app, plan, runtime, { role, key, context, sources = [], prompt, signal }) {
  const ref = await app.artifacts.json(context, { type: 'support-context', caseId: plan.caseId });
  const task = { role, caseId: plan.caseId, revisionId: plan.revisionId, stageId: key, publicationType: 'stage-packet',
    sources: [{ sourceId: 'support-context', locator: ref.locator, digest: ref.digest, sourceKind: 'task', originGroupId: plan.caseId }, ...sources],
    prompt: `${prompt}\nRead support-context and relevant registered sources with simulation_source_read. Publish exactly one stage-packet JSON with decision=proceed|blocked|revise, summary, limitations array, and any requested structured fields. Preserve actual evidence and missing-source limitations. Return its artifact reference.` };
  const actor = { actorId: role, actorInstanceId: id('role'), kind: 'agent' };
  const receipt = await runtime.executeRole(task, `support-${plan.revisionId}:${key}:${role}`, signal, { actor });
  requireCondition(receipt.status === 'succeeded', receipt.status === 'unknown' ? 'RESULT_UNKNOWN' : 'ROLE_FAILED',
    `Support role ${role} ${receipt.status}`, { sideEffectState: receipt.status === 'unknown' ? 'unknown' : 'none' });
  const refs = receipt.outputRefs.filter(item => ['stage-packet', 'application/json', 'json'].includes(item.type));
  requireCondition(refs.length === 1, 'SCHEMA_INVALID', 'Support role must publish one stage packet');
  const packet = JSON.parse((await app.artifacts.read(refs[0])).toString('utf8'));
  requireCondition(packet && typeof packet === 'object' && ['proceed', 'blocked', 'revise'].includes(packet.decision) && packet.summary?.trim()
    && Array.isArray(packet.limitations), 'SCHEMA_INVALID', 'Support role packet is incomplete');
  return { packet, ref: refs[0], receipt };
}

export async function researchRoles(app, plan, runtime, root, { attempt, signal }) {
  const definitions = [
    { role: conditionalRoles.research[0], file: 'references/in.indent.lmp', sourceKind: 'case',
      prompt: 'Select the relevant baseline from the bundled indentation case. Explain reusable geometry/force-output mechanisms and differences for 3D multi-component grinding. Do not claim that this baseline validates material physics.' },
    { role: conditionalRoles.research[1], file: 'models.json', sourceKind: 'metadata',
      prompt: 'Assess the registered potential catalogue metadata. Extract DOI, elements, model scope and available limitations. This source is catalogue metadata, so explicitly identify full-text and quantitative validation gaps. Missing experimental evidence is compatible with the approved exploratory scope.' }
  ];
  const result = [];
  for (const definition of definitions) {
    const path = join(root, 'domains/lammps', definition.file);
    const source = { sourceId: 'research-source', locator: path, digest: await hashFile(path),
      sourceKind: definition.sourceKind, originGroupId: definition.sourceKind === 'case' ? 'lammps-example' : 'potential-catalogue' };
    const existing = app.store.list('role-operations').filter(operation => operation.status === 'succeeded'
      && operation.caseId === plan.caseId && operation.revisionId === plan.revisionId && operation.actor.actorId === definition.role).reverse();
    let reusable;
    for (const operation of existing) {
      const reads = (operation.evidenceRefs || []).map(ref => app.store.get('reads', ref)).filter(Boolean);
      const contextRead = reads.find(read => read.sourceId === 'support-context');
      const researchRead = reads.find(read => read.sourceId === 'research-source' && read.digest === source.digest);
      if (!contextRead || !researchRead) continue;
      await app.evidence.assess(reads.map(read => read.id), {}, operation.actor.actorInstanceId);
      let context;
      try { context = JSON.parse(contextRead.excerpt); } catch { continue; }
      const candidates = operation.outputRefs.filter(ref => ['stage-packet', 'application/json', 'json'].includes(ref.type));
      if (digest(context.scheme || null) !== digest(plan.scheme) || candidates.length !== 1) continue;
      const packet = JSON.parse((await app.artifacts.read(candidates[0])).toString('utf8'));
      if (packet.decision === 'proceed' && packet.summary?.trim() && Array.isArray(packet.limitations)) { reusable = candidates[0]; break; }
    }
    if (reusable) { result.push(reusable); continue; }
    const selected = await supportRole(app, plan, runtime, { role: definition.role, key: `research:${attempt}`,
      context: { scheme: plan.scheme }, sources: [source], prompt: definition.prompt, signal });
    requireCondition(selected.packet.decision === 'proceed', 'EVIDENCE_INSUFFICIENT', selected.packet.summary);
    result.push(selected.ref);
  }
  return result;
}

export async function knowledgeRoles(app, plan, runtime, { analysisRef, signal }) {
  const prior = app.store.get('case-knowledge', `${plan.revisionId}:${analysisRef.digest}`);
  if (prior) return prior;
  const analysis = JSON.parse((await app.artifacts.read(analysisRef)).toString('utf8'));
  const context = { caseId: plan.caseId, scheme: plan.scheme, analysis };
  const coordinator = await supportRole(app, plan, runtime, { role: conditionalRoles.knowledge[0], key: `knowledge:${analysisRef.digest}`,
    context, prompt: 'Choose a bounded reusable lesson from the actual analysis. Explain scientific scope and uncertainty. Include summary and limitations.', signal });
  requireCondition(coordinator.packet.decision === 'proceed', 'EVIDENCE_INSUFFICIENT', coordinator.packet.summary);
  const curator = await supportRole(app, plan, runtime, { role: conditionalRoles.knowledge[1], key: `knowledge:${analysisRef.digest}`,
    context: { ...context, coordination: coordinator.packet },
    prompt: 'Draft a science-only knowledge candidate. Include lesson as a nonempty string and applicability as an object. Include the single-case, model-alloy and absent convergence/experimental validation limitations. Avoid generalizing its force measurements to experiments.', signal });
  requireCondition(curator.packet.decision === 'proceed' && typeof curator.packet.lesson === 'string' && curator.packet.lesson.trim()
    && curator.packet.applicability && typeof curator.packet.applicability === 'object', 'SCHEMA_INVALID', 'Curator needs a bounded lesson and applicability');
  const meta = await app.knowledge.ingest(curator.ref.locator, { domain: 'molecular-dynamics', type: 'case_note',
    title: `${plan.caseId} bounded grinding lesson`, originGroupId: plan.caseId });
  const proposal = app.knowledge.propose({ sourceId: meta.id, lesson: curator.packet.lesson,
    applicability: curator.packet.applicability }, { ...curator.receipt.actor, capabilities: ['knowledge.curate'] });
  const reviewer = await supportRole(app, plan, runtime, { role: conditionalRoles.knowledge[2], key: `knowledge:${analysisRef.digest}`,
    context: { ...context, candidate: curator.packet, proposal },
    prompt: 'Independently assess this exact knowledge proposal against the measured analysis and limitations. Include verdict=confirmed|candidate|quarantined and reasons as a nonempty string. Confirm only the bounded lesson supported by this run.', signal });
  requireCondition(['confirmed', 'candidate', 'quarantined'].includes(reviewer.packet.verdict)
    && typeof reviewer.packet.reasons === 'string' && reviewer.packet.reasons.trim(), 'SCHEMA_INVALID', 'Knowledge reviewer needs verdict and reasons');
  const review = app.knowledge.review(proposal.id, { verdict: reviewer.packet.verdict, reasons: reviewer.packet.reasons },
    { ...reviewer.receipt.actor, capabilities: ['knowledge.review'] });
  const applied = await app.knowledge.apply(review.id);
  return app.store.put('case-knowledge', `${plan.revisionId}:${analysisRef.digest}`, { proposal, review, applied,
    roleRefs: [coordinator.ref, curator.ref, reviewer.ref] }, 0);
}
