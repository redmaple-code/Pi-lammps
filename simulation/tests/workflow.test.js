import test from 'node:test';
import assert from 'node:assert/strict';
import { fixture } from './fixtures.js';
import { WorkflowRunner } from '../packages/application/workflow.js';
import { serviceActor } from '../packages/contracts/index.js';

test('generic runner executes different domain graphs and frozen slots without domain branching', async t => {
  const stages = [{ id: 'mesh', role: 'builder', needs: [], consumes: [], produces: ['mesh'], reviewRequired: false },
    { id: 'field', role: 'solver', needs: ['mesh'], consumes: ['mesh'], produces: ['field'], reviewRequired: false }];
  const f = await fixture(t, { stages });
  const handler = { actor: serviceActor('program'), execute: async ({ stage, inputs }) => {
    const ref = await f.artifacts.json({ stage: stage.id, inputCount: Object.keys(inputs).length });
    return { outputSlots: { [stage.produces[0]]: [ref] } };
  } };
  const runner = new WorkflowRunner({ app: f, handlers: { builder: handler, solver: handler } });
  assert.equal((await runner.run(f.state.id)).status, 'completed');
  const count = f.store.events(f.state.id).length;
  assert.equal((await runner.run(f.state.id)).status, 'completed');
  assert.equal(f.store.events(f.state.id).length, count);
});
test('unknown branch is blocked and false skipped output is never supplied to its consumer', async t => {
  const stages = [{ id: 'optional', role: 'builder', needs: [], consumes: [], produces: ['value'], reviewRequired: false,
    when: { fact: 'enabled', op: 'eq', value: true } }, { id: 'consumer', role: 'builder', needs: ['optional'], consumes: ['value'], produces: ['end'], reviewRequired: false }];
  const f = await fixture(t, { stages });
  const runner = new WorkflowRunner({ app: f, handlers: {} });
  assert.equal((await runner.run(f.state.id)).stages[0].status, 'blocked');
  runner.facts = async () => ({ enabled: false });
  const state = await runner.run(f.state.id);
  assert.equal(state.stages[0].status, 'skipped');
  assert.equal(state.stages[1].status, 'blocked');
});

test('a skipped optional stage allows a successor with no missing inputs', async t => {
  const stages = [{ id: 'optional', needs: [], produces: [], when: { fact: 'enabled', op: 'eq', value: true } },
    { id: 'end', role: 'builder', needs: ['optional'], consumes: [], produces: ['end'], reviewRequired: false }];
  const f = await fixture(t, { stages });
  const runner = new WorkflowRunner({ app: f, facts: async () => ({ enabled: false }), handlers: {
    builder: { actor: serviceActor('program'), execute: async () => ({ outputSlots: { end: [await f.artifacts.text('done')] } }) }
  } });
  assert.equal((await runner.run(f.state.id)).status, 'completed');
});
