import test from 'node:test';
import assert from 'node:assert/strict';
import { assessCommandAnswers } from '../scripts/assess-command-answers.js';

const correct = { speedMPerS: 1000, fixPrintVariablesValid: true, engineeringStrainValid: true, eamCanSetMasses: true };
test('answer assessment accepts an explicit answer envelope without changing its content', () => {
  const result = assessCommandAnswers({ sourceId: 'exercise', answers: correct });
  assert.equal(result.answerLocation, 'answers');
  assert.ok(Object.values(result.checks).every(Boolean));
});
test('answer assessment preserves contradictory primary fields and reports the conflicts', () => {
  const result = assessCommandAnswers({ ...correct, speedMPerS: 10, eamCanSetMasses: false, questionAnswers: correct });
  assert.equal(result.answerLocation, 'root');
  assert.equal(result.checks.speedCorrect, false);
  assert.equal(result.checks.massSemanticsCorrect, false);
  assert.equal(result.checks.structuredAnswersConsistent, false);
  assert.deepEqual(result.conflictingFields, ['speedMPerS', 'eamCanSetMasses']);
});
