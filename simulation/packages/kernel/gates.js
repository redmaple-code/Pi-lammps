import { id, digest, utc, requireCondition } from '../contracts/index.js';
import { validateRecord } from '../contracts/records.js';

export class GateService {
  constructor(store, artifacts, evidence) { Object.assign(this, { store, artifacts, evidence }); }
  async submit({ caseId, stageId, subjectRefs, producerInstanceId, checks, evidenceRefs = [], policy = {}, issues = [] }, authenticatedReviewer) {
    checks = structuredClone(checks);
    requireCondition(authenticatedReviewer?.capabilities?.includes('review.submit'), 'PERMISSION_DENIED', 'Actor cannot review');
    requireCondition(authenticatedReviewer.actorInstanceId !== producerInstanceId, 'REVIEW_REQUIRED', 'Producer cannot independently review its own work');
    const state = this.store.getCase(caseId), stage = state.stages.find(item => item.id === stageId);
    requireCondition(stage && stage.status === 'awaitingReview', 'STATE_CONFLICT', 'Stage is not awaiting review');
    requireCondition(producerInstanceId === stage.producerInstanceId
      && authenticatedReviewer.actorInstanceId !== stage.producerInstanceId, 'REVIEW_REQUIRED', 'Review producer identity differs from the stage');
    requireCondition(digest(subjectRefs) === digest(stage.outputRefs), 'ARTIFACT_CHANGED', 'Review must cover the current complete stage output');
    requireCondition(subjectRefs.length > 0 && checks.length > 0, 'EVIDENCE_INSUFFICIENT', 'Review requires subjects and explicit checks');
    requireCondition(new Set(checks.map(check => check.checkId)).size === checks.length, 'SCHEMA_INVALID', 'Review check IDs must be unique');
    for (const ref of subjectRefs) await this.artifacts.verify(ref);
    const requiredPolicy = { ...policy };
    for (const [key, value] of Object.entries(stage.evidencePolicy || {})) requiredPolicy[key] = value || requiredPolicy[key];
    const claimEvidence = checks.flatMap(check => check.extensions?.claimEvidence || []);
    const evidence = await this.evidence.assess(evidenceRefs, requiredPolicy, authenticatedReviewer.actorInstanceId, claimEvidence);
    const mustBlock = checks.some(check => check.applicability === 'unknown' || check.result === 'insufficient'
      || (check.applicability === true && check.result === 'failed' && check.action === 'blocked'));
    const bad = checks.some(check => check.applicability === true && check.result !== 'passed');
    const requiredIds = stage.requiredCheckIds || [];
    const missingChecks = requiredIds.filter(checkId => !checks.some(check => check.checkId === checkId));
    const inapplicableChecks = (stage.requiredApplicableCheckIds || []).filter(checkId => !checks.some(check => check.checkId === checkId && check.applicability === true));
    const verdict = mustBlock || !evidence.passed || missingChecks.length || inapplicableChecks.length ? 'BLOCKED' : bad ? 'REVISE' : 'PASS';
    for (const check of checks) {
      check.evidenceRefs ||= evidence.readRefs;
      requireCondition(check.evidenceRefs.every(ref => evidence.readRefs.includes(typeof ref === 'string' ? ref : ref?.id)),
        'EVIDENCE_INSUFFICIENT', 'Check evidence is outside the actual review reads');
      validateRecord('CheckResult', check);
      requireCondition([true, false, 'unknown'].includes(check.applicability), 'SCHEMA_INVALID', 'Invalid applicability');
      requireCondition(check.applicability !== false || check.result === 'notApplicable', 'SCHEMA_INVALID', 'False applicability needs notApplicable result');
      requireCondition(check.applicability !== 'unknown' || check.result !== 'passed', 'SCHEMA_INVALID', 'Unknown cannot pass');
      requireCondition(check.observations?.length, 'EVIDENCE_INSUFFICIENT', 'Each check needs observations or inapplicability evidence');
    }
    const receipt = { id: id('review'), caseId, revisionId: state.revisionId, stageId, attempt: stage.attempt,
      subjectRefs, subjectDigest: digest(subjectRefs.map(ref => ref.digest)), checkSetRef: digest(requiredIds),
      evidencePolicyDigest: digest(stage.evidencePolicy || {}),
      applicabilityPolicyDigest: digest(stage.requiredApplicableCheckIds || []),
      producerInstanceId, reviewer: authenticatedReviewer, checks, evidence, issues, missingChecks, inapplicableChecks, verdict, createdAt: utc() };
    receipt.schemaVersion = '1.0.0';
    receipt.producer = authenticatedReviewer;
    receipt.context = { caseId, revisionId: state.revisionId, stageInstanceId: stageId };
    receipt.reviewerInstanceId = authenticatedReviewer.actorInstanceId;
    receipt.results = checks;
    receipt.issueRefs = issues;
    validateRecord('ReviewReceipt', receipt);
    this.store.put('reviews', receipt.id, receipt, 0);
    return receipt;
  }
  async validate(receiptRef, state, stage) {
    const receipt = this.store.get('reviews', receiptRef);
    requireCondition(receipt?.caseId === state.id && receipt.revisionId === state.revisionId && receipt.stageId === stage.id
      && receipt.attempt === stage.attempt, 'REVIEW_REQUIRED', 'Review is for another subject or attempt');
    requireCondition(receipt.checkSetRef === digest(stage.requiredCheckIds || []), 'REVIEW_REQUIRED', 'Check policy changed');
    if (receipt.evidencePolicyDigest) requireCondition(receipt.evidencePolicyDigest === digest(stage.evidencePolicy || {}), 'REVIEW_REQUIRED', 'Evidence policy changed');
    if (receipt.applicabilityPolicyDigest) requireCondition(receipt.applicabilityPolicyDigest === digest(stage.requiredApplicableCheckIds || []), 'REVIEW_REQUIRED', 'Applicability policy changed');
    requireCondition((stage.requiredApplicableCheckIds || []).every(checkId => receipt.checks.some(check => check.checkId === checkId && check.applicability === true)),
      'REVIEW_REQUIRED', 'A required review check is inapplicable');
    requireCondition(receipt.subjectDigest === digest(stage.outputRefs.map(ref => ref.digest)), 'ARTIFACT_CHANGED', 'Reviewed subject differs');
    for (const ref of receipt.subjectRefs) await this.artifacts.verify(ref);
    const evidence = await this.evidence.assess(receipt.evidence.readRefs, stage.evidencePolicy || {}, receipt.reviewerInstanceId,
      receipt.checks.flatMap(check => check.extensions?.claimEvidence || []));
    if (receipt.verdict === 'PASS') requireCondition(evidence.passed, 'EVIDENCE_INSUFFICIENT', 'Reviewed evidence is no longer sufficient');
    return receipt;
  }
}
