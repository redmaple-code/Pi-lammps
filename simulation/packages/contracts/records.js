import { validateSchema } from './validation.js';
import { requireCondition } from './index.js';

const string = { type: 'string', minLength: 1 };
const array = { type: 'array' };
const fields = {
  TaskSpec: { intent: string, scope: { enum: ['new', 'modify', 'execute', 'analyze', 'visualize', 'query', 'diagnose', 'resume', 'reproduce'] }, problem: { type: 'object' }, constraints: { type: 'object' }, requestedDeliverables: array },
  ResolvedLock: { packs: array, workflowRef: string, resources: array, adapterBindings: array, policies: { type: 'object' }, digest: string },
  ArtifactRef: { artifactId: string, type: string, locator: string, digest: string, byteLength: { type: 'integer', minimum: 0 }, dependencies: array, status: { enum: ['available', 'missing', 'quarantined'] } },
  EvidenceRef: { sourceId: string, locator: string, sourceKind: string, originGroupId: string, readReceiptRef: string },
  SourceReadReceipt: { id: string, sourceId: string, locator: string, digest: string, representation: { enum: ['fullText', 'section', 'abstract', 'metadata'] }, startLine: { type: 'integer', minimum: 1 }, endLine: { type: 'integer', minimum: 1 }, reader: { type: 'object' } },
  StagePacket: { inputRefs: array, outputRefs: array, decisionRefs: array, selfChecks: array, risks: array },
  ReviewReceipt: { subjectDigest: string, checkSetRef: string, producerInstanceId: string, reviewerInstanceId: string, results: array,
    verdict: { enum: ['PASS', 'REVISE', 'BLOCKED'] }, issueRefs: array },
  CheckResult: { checkId: string, applicability: { enum: [true, false, 'unknown'] }, result: { enum: ['passed', 'failed', 'insufficient', 'notApplicable'] }, evidenceRefs: array, observations: array, action: { enum: ['revise', 'blocked'] } },
  RunSpec: { argv: { type: 'array', minItems: 1, items: string }, inputClosure: { type: 'object' }, permitRef: string, timeoutMs: { type: 'number', exclusiveMinimum: 0 } },
  MetricDefinition: { id: string, quantity: string, unit: string, methodRef: string, selection: string, reference: string, sampling: { type: 'object' }, acceptanceSpec: { type: 'object' } },
  MetricResult: { definitionRef: string, dataRefs: array, verdict: { enum: ['met', 'notMet', 'cannotCompute', 'borderline'] }, limitations: array },
  FigureRecord: { dataRefs: array, analysisRefs: array, outputRefs: array, status: { enum: ['generated', 'scriptOnly', 'skipped', 'failed'] } },
  ValidationReport: { dimension: { enum: ['numerical', 'model', 'task'] }, methodRef: string, referenceRefs: array, dataRefs: array,
    outcome: { enum: ['passed', 'failed', 'insufficient', 'notApplicable'] }, limitations: array }
};
export const recordSchemas = Object.fromEntries(Object.entries(fields).map(([name, properties]) => [name, {
  $id: `https://redmaple.local/simulation/1/${name}`, type: 'object', properties,
  required: Object.keys(properties), additionalProperties: true
}]));
export function validateRecord(type, value) {
  requireCondition(recordSchemas[type], 'SCHEMA_INVALID', `Unknown record type ${type}`);
  validateSchema(recordSchemas[type], value);
  const extraFields = {
    CheckResult: [],
    RunSpec: ['environment', 'executionMode', 'resources', 'outputs', 'authorizationRef'],
    MetricDefinition: ['field', 'valueType', 'component', 'direction', 'sign', 'statistics', 'outputRequirements', 'requiredOutputs']
  };
  if (extraFields[type]) {
    const allowed = new Set([...Object.keys(fields[type]), ...extraFields[type], 'schemaVersion', 'createdAt', 'producer', 'context', 'extensions']);
    requireCondition(Object.keys(value).every(key => allowed.has(key)), 'SCHEMA_INVALID', `Unexpected core field in ${type}`);
  }
  if (type === 'CheckResult') {
    requireCondition(value.applicability !== 'unknown' || value.result === 'insufficient', 'SCHEMA_INVALID', 'Unknown applicability requires insufficient result');
    requireCondition(value.applicability !== false || value.result === 'notApplicable', 'SCHEMA_INVALID', 'False applicability requires notApplicable');
  }
  if (type === 'FigureRecord' && value.status === 'generated') requireCondition(value.inspection?.opened && value.outputRefs.length, 'EVIDENCE_INSUFFICIENT', 'Generated image requires actual inspection');
  if (type === 'MetricResult' && value.verdict === 'cannotCompute') requireCondition(value.limitations.length, 'SCHEMA_INVALID', 'Missing-data result requires a reason');
  return value;
}
