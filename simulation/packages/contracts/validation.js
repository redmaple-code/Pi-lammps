import Ajv from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import { readFileSync } from 'node:fs';
import { requireCondition } from './index.js';

const ajv = new Ajv({ allErrors: true, strict: false });
addFormats(ajv);
const packSchema = JSON.parse(readFileSync(new URL('../../schemas/domain-pack.schema.json', import.meta.url), 'utf8'));
export const validatePackStructure = value => {
  const validate = ajv.getSchema('domain-pack') || ajv.addSchema(packSchema, 'domain-pack').getSchema('domain-pack');
  requireCondition(validate(value), 'SCHEMA_INVALID', 'Invalid domain package', { context: { errors: validate.errors } });
  return value;
};
export function validateSchema(schema, value) {
  const validate = schema.$id && ajv.getSchema(schema.$id) || ajv.compile(schema);
  requireCondition(validate(value), 'SCHEMA_INVALID', 'Record does not match schema', { context: { errors: validate.errors } });
  return value;
}
