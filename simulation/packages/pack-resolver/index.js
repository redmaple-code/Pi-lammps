import { readFile } from 'node:fs/promises';
import { dirname } from 'node:path';
import { parseDocument } from 'yaml';
import semver from 'semver';
import { validatePackStructure } from '../contracts/validation.js';
import { CONTRACT_VERSION, digest, topological, requireCondition } from '../contracts/index.js';
import { hashFile, scopedPath } from '../artifacts/index.js';
import { scientificContentDigest } from './release.js';

export function parseDocumentText(text) {
  const doc = parseDocument(text, { uniqueKeys: true, customTags: [], maxAliasCount: 0 });
  requireCondition(!doc.errors.length, 'SCHEMA_INVALID', doc.errors.map(error => error.message).join('\n'));
  const result = doc.toJS({ maxAliasCount: 0 });
  digest(result);
  return result;
}
export function validateGraph(workflow) {
  const order = topological(workflow.stages);
  const slots = new Map((workflow.inputs || []).map(slot => [slot, null]));
  for (const stage of order) {
    const ancestors = new Set();
    const visit = key => { if (ancestors.has(key)) return; ancestors.add(key); for (const parent of workflow.stages.find(n => n.id === key).needs || []) visit(parent); };
    for (const parent of stage.needs || []) visit(parent);
    for (const slot of stage.consumes || []) {
      requireCondition(slots.has(slot) && (slots.get(slot) === null || ancestors.has(slots.get(slot))), 'REFERENCE_MISSING', `${stage.id} has no upstream producer for ${slot}`);
    }
    for (const slot of stage.produces || []) {
      requireCondition(!slots.has(slot), 'SCHEMA_INVALID', `Multiple producers for ${slot}`);
      slots.set(slot, stage.id);
    }
    if (stage.kind === 'iteration') requireCondition(Number.isInteger(stage.maxIterations) && stage.maxIterations > 0, 'SCHEMA_INVALID', 'Iterations must be bounded');
  }
  for (const output of workflow.outputs || []) requireCondition(slots.has(output), 'REFERENCE_MISSING', `Output ${output} has no producer`);
  return order;
}
export async function resolvePack(manifestPath, { registry = {}, capabilityRegistry = {}, validation = false, workflowId, hostPolicy = {}, releaseLookup } = {}, stack = []) {
  const pack = validatePackStructure(parseDocumentText(await readFile(manifestPath, 'utf8')));
  requireCondition(!stack.includes(pack.id), 'SCHEMA_INVALID', `Package dependency cycle ${pack.id}`);
  requireCondition(semver.satisfies(CONTRACT_VERSION, pack.kernelCompatibility), 'VERSION_INCOMPATIBLE', 'Kernel version is incompatible');
  const diagnostics = [], root = dirname(manifestPath), resources = new Map(), dependencies = [];
  for (const dependency of pack.dependencies) {
    const candidate = registry[dependency.packId || dependency.id];
    requireCondition(candidate, 'REFERENCE_MISSING', 'Dependency is not installed');
    const resolved = await resolvePack(candidate, { registry, capabilityRegistry, validation, hostPolicy, releaseLookup }, [...stack, pack.id]);
    requireCondition(semver.satisfies(resolved.pack.version, dependency.versionRange), 'VERSION_INCOMPATIBLE', 'Dependency version differs');
    dependencies.push(resolved);
    diagnostics.push(...resolved.diagnostics);
  }
  requireCondition(new Set(pack.resources.map(resource => resource.id)).size === pack.resources.length, 'SCHEMA_INVALID', 'Duplicate resource ID');
  requireCondition(new Set(pack.resources.map(resource => resource.path)).size === pack.resources.length, 'SCHEMA_INVALID', 'Duplicate resource path');
  for (const resource of pack.resources) {
    if (resource.availability === 'missing') { diagnostics.push({ code: 'REFERENCE_MISSING', resourceId: resource.id }); continue; }
    try {
      const path = await scopedPath(root, resource.path);
      const actual = await hashFile(path);
      requireCondition(actual === resource.digest, 'ARTIFACT_CHANGED', `Resource changed ${resource.id}`);
      resources.set(resource.id, { ...resource, locator: path });
    } catch (error) { diagnostics.push({ code: error.code || 'REFERENCE_MISSING', resourceId: resource.id, message: error.message }); }
  }
  async function checkReference(reference, expectedKind) {
    const [resourceId, fragment] = reference.split('#');
    if (resourceId.includes('::')) {
      const [packId, localId] = resourceId.split('::');
      const parent = dependencies.find(item => item.pack.id === packId);
      requireCondition(parent, 'REFERENCE_MISSING', `Dependency reference missing ${reference}`);
      return parent.checkReference(`${localId}${fragment ? `#${fragment}` : ''}`, expectedKind);
    }
    const declared = pack.resources.find(item => item.id === resourceId);
    requireCondition(declared && (!expectedKind || declared.kind === expectedKind), 'REFERENCE_MISSING', `Wrong resource reference ${reference}`);
    if (fragment && resources.has(resourceId)) {
      const data = parseDocumentText(await readFile(resources.get(resourceId).locator, 'utf8'));
      const items = Array.isArray(data) ? data : data.items || Object.values(data);
      requireCondition(items.some(item => item?.id === fragment || item === fragment), 'REFERENCE_MISSING', `Fragment ${reference} not found`);
    }
  }
  const entryKinds = { problemSchema: 'problemSchema', artifactSchemas: 'artifactSchema', knowledge: 'knowledge', models: 'model',
    vocabulary: 'vocabulary', checks: 'check', metrics: 'metric', failures: 'failure', templates: 'template', roles: 'role', validation: 'validation' };
  for (const [name, value] of Object.entries(pack.entrypoints)) for (const reference of Array.isArray(value) ? value : [value]) await checkReference(reference, entryKinds[name]);
  for (const workflow of pack.workflows) {
    validateGraph(workflow);
    for (const stage of workflow.stages) {
      for (const ref of stage.checkRefs) await checkReference(ref, 'check');
      if (stage.kind === 'iteration') {
        const child = pack.workflows.find(item => item.id === stage.bodyWorkflowRef);
        requireCondition(child && child.id !== workflow.id, 'REFERENCE_MISSING', 'Iteration body missing or recursive');
        await checkReference(stage.iterationProtocolRef, 'protocol');
      }
    }
  }
  topological(pack.workflows.map(workflow => ({ id: workflow.id, needs: workflow.stages.filter(stage => stage.kind === 'iteration').map(stage => stage.bodyWorkflowRef) })));
  const workflow = pack.workflows.find(item => item.id === (workflowId || pack.defaultWorkflow));
  requireCondition(workflow, 'REFERENCE_MISSING', 'Workflow selection does not exist');
  const profile = pack.toolProfiles.find(item => item.id === pack.defaultToolProfile);
  requireCondition(profile, 'REFERENCE_MISSING', 'Tool profile missing');
  for (const capability of pack.requiredCapabilities) {
    const binding = profile.bindings.find(item => item.capability === capability);
    const adapter = binding && capabilityRegistry[binding.adapterId];
    if (!adapter?.capabilities.includes(capability) || !semver.satisfies(adapter.version, binding.adapterVersionRange)) {
      diagnostics.push({ code: 'CAPABILITY_UNAVAILABLE', capability, adapterId: binding?.adapterId });
    }
    if (binding?.validationStatus !== 'validated' && !validation) diagnostics.push({ code: 'VALIDATION_REQUIRED', capability });
  }
  if (pack.publicationStatus === 'draft' && !validation) diagnostics.push({ code: 'DRAFT_PACKAGE', message: 'Use candidate validation or publish after verification' });
  if (pack.publicationStatus === 'released' && !validation) {
    const receipt = await releaseLookup?.(scientificContentDigest(pack));
    if (!receipt || receipt.status !== 'released' || receipt.archiveDigest !== digest(pack)) diagnostics.push({ code: 'RELEASE_UNVERIFIED', packId: pack.id });
  }
  for (const [key, value] of Object.entries(hostPolicy)) {
    if (typeof value === 'number' && pack.policies[key] > value) diagnostics.push({ code: 'POLICY_CONFLICT', policy: key });
  }
  const lockData = { contractVersion: CONTRACT_VERSION, packs: [{ id: pack.id, version: pack.version, digest: digest(pack) },
    ...dependencies.flatMap(item => item.lock.packs)], workflowRef: workflow.id, profileRef: profile.id,
    resources: [...resources.values()].map(({ id, digest }) => ({ id, digest })), adapterBindings: profile.bindings,
    policies: pack.policies, mode: validation ? 'candidateValidation' : 'production' };
  return { pack, workflow, resources, root, checkReference, diagnostics, structurallyValid: true,
    semanticallyValid: !diagnostics.some(d => ['REFERENCE_MISSING', 'ARTIFACT_CHANGED'].includes(d.code)),
    executionReady: diagnostics.length === 0, lock: { ...lockData, digest: digest(lockData) } };
}
