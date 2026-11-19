const expected = { speedMPerS: 1000, fixPrintVariablesValid: true, engineeringStrainValid: true, eamCanSetMasses: true };
const checkNames = { speedMPerS: 'speedCorrect', fixPrintVariablesValid: 'printSemanticsCorrect',
  engineeringStrainValid: 'strainSemanticsCorrect', eamCanSetMasses: 'massSemanticsCorrect' };

export function assessCommandAnswers(findings) {
  const containers = [{ location: 'root', value: findings }, { location: 'answers', value: findings?.answers },
    { location: 'questionAnswers', value: findings?.questionAnswers }]
    .filter(({ value }) => value && typeof value === 'object' && Object.keys(expected).every(key => Object.hasOwn(value, key)));
  const primary = containers[0];
  const conflictingFields = Object.keys(expected).filter(key =>
    new Set(containers.map(({ value }) => JSON.stringify(value[key]))).size > 1);
  return { answerLocation: primary?.location || null, conflictingFields,
    checks: { ...Object.fromEntries(Object.entries(expected).map(([key, value]) => [checkNames[key], primary?.value[key] === value])),
      structuredAnswersConsistent: conflictingFields.length === 0 } };
}
