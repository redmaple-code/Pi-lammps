import { defineModule, requireThat } from '../core/index.js';

// Inspection is not an authorization or a scientific validation.
export function createConsistencyModule({ inspect, checkSetVersion, requiredCheckIds }) {
  requireThat(typeof inspect === 'function' && typeof checkSetVersion === 'string' && checkSetVersion.length > 0
    && Array.isArray(requiredCheckIds) && requiredCheckIds.length > 0
    && requiredCheckIds.every(x => typeof x === 'string' && x.length > 0)
    && new Set(requiredCheckIds).size === requiredCheckIds.length, 'CONFIG_INVALID', 'Frozen nonempty check set required');
  const required = [...requiredCheckIds];
  return defineModule({ id: 'consistency', operations: {
    inspect: { effect: 'read', capability: 'consistency.inspect', async invoke(input) {
      requireThat(typeof input.text === 'string' && input.text.length > 0 && input.text.length <= 2_000_000,
        'SCHEMA_INVALID', 'Bounded input text required');
      const checks = await inspect(input.text, input.options || {});
      requireThat(Array.isArray(checks), 'SCHEMA_INVALID', 'Inspector must return checks');
      const ids = checks.map(c => c?.checkId);
      requireThat(ids.every(x => typeof x === 'string' && x.length > 0) && new Set(ids).size === ids.length,
        'SCHEMA_INVALID', 'Invalid or duplicate check IDs');
      for (const c of checks) {
        requireThat([true, false, 'unknown'].includes(c.applicability)
          && ['passed','failed','insufficient','notApplicable'].includes(c.result)
          && (c.applicability === true ? ['passed','failed','insufficient'].includes(c.result)
            : c.applicability === false ? c.result === 'notApplicable' : c.result === 'insufficient'),
        'SCHEMA_INVALID', 'Invalid check result');
      }
      const missing = required.filter(id => !ids.includes(id));
      const incomplete = checks.length === 0 || missing.length > 0;
      const failed = checks.some(c => c.applicability === true && c.result === 'failed');
      const uncertain = checks.some(c => c.applicability === 'unknown' || c.result === 'insufficient');
      return { checkSetVersion, requiredCheckIds: [...required], checks, missingCheckIds: missing,
        verdict: failed ? 'FAIL' : incomplete || uncertain ? 'REVIEW' : 'PASS', coverageComplete: !incomplete,
        limitations: ['Static inspection only; not scientific validation or execution authorization'] };
    } }
  } });
}
