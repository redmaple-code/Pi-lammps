import { id, utc, requireCondition } from '../contracts/index.js';

export function recordAuthorization(store, { source, actions, caseId, candidateDigest, resources = {}, expiresAt }, authenticatedActor) {
  requireCondition(authenticatedActor?.kind === 'human' && source && actions?.length, 'AUTHORIZATION_REQUIRED', 'A user decision with explicit scope is required');
  const receipt = { id: id('authorization'), source, actions, caseId: caseId || null, candidateDigest: candidateDigest || null,
    resources, expiresAt: expiresAt || null, actor: authenticatedActor, createdAt: utc() };
  store.put('authorizations', receipt.id, receipt, 0);
  return receipt;
}
export function authorize(store, authorizationRef, action, context = {}) {
  const receipt = store.get('authorizations', authorizationRef);
  requireCondition(receipt?.actions.includes(action), 'AUTHORIZATION_REQUIRED', `Action ${action} has no user authorization`);
  requireCondition(!receipt.caseId || receipt.caseId === context.caseId, 'AUTHORIZATION_REQUIRED', 'Authorization is for another case');
  requireCondition(!receipt.candidateDigest || receipt.candidateDigest === context.candidateDigest, 'AUTHORIZATION_REQUIRED', 'Candidate differs from authorized version');
  requireCondition(!receipt.expiresAt || Date.parse(receipt.expiresAt) > Date.now(), 'AUTHORIZATION_REQUIRED', 'Authorization expired');
  for (const [name, value] of Object.entries(context.resources || {})) {
    requireCondition(Number.isFinite(value) && value <= (receipt.resources[name] ?? 0), 'RESOURCE_LIMIT', `Resource ${name} exceeds grant`);
  }
  return receipt;
}
