import { requireCondition } from '../contracts/index.js';

export const improvementScope = ['AI002', 'AI003', 'AI004', 'AI006', 'AI007'];

export function deliveryStatus(receipts, { scope = improvementScope, currentSourceDigest } = {}) {
  requireCondition(typeof currentSourceDigest === 'string' && currentSourceDigest.length > 0,
    'SCHEMA_INVALID', 'Delivery requires the current source digest');
  requireCondition(Array.isArray(scope) && scope.length > 0 && new Set(scope).size === scope.length,
    'SCHEMA_INVALID', 'Delivery scope must contain unique issue IDs');
  const items = scope.map(issueId => {
    const matches = receipts.filter(receipt => receipt.issueId === issueId);
    const verified = matches.filter(receipt => receipt.verdict === 'passed' && receipt.executedAt && receipt.evidenceRefs?.length
      && receipt.sourceDigest === currentSourceDigest);
    let status = 'notVerified';
    if (matches.some(receipt => receipt.verdict === 'failed' && receipt.sourceDigest === currentSourceDigest)) status = 'failed';
    else if (verified.length) status = 'verified';
    return { issueId, status, evidenceRefs: verified.flatMap(receipt => receipt.evidenceRefs),
      limitations: matches.flatMap(receipt => receipt.limitations || []) };
  });
  return { scope, status: items.every(item => item.status === 'verified') ? 'verified' : 'partial',
    verified: items.filter(item => item.status === 'verified').length, total: scope.length, items };
}
