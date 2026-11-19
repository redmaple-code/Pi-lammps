import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { id, utc, requireCondition } from '../contracts/index.js';

export async function registerFigure(path, { artifacts, dataRefs, analysisRefs, tool, selection, inspection }) {
  requireCondition(dataRefs?.length && analysisRefs?.length && inspection?.opened === true && inspection.checked?.length,
    'EVIDENCE_INSUFFICIENT', 'Generated figures require actual image inspection');
  const ref = await artifacts.publish(path, { type: 'image', tool, selection, dataRefs, analysisRefs });
  return { id: id('figure'), status: 'generated', dataRefs, analysisRefs, tool, selection, outputRefs: [ref], inspection, createdAt: utc() };
}
export async function writeReport(directory, { state, scheme, run, analysis, figures = [], knowledge = [], improvements = [] }) {
  await mkdir(directory, { recursive: true });
  const lines = [`# ${state.title}`, '', `Case ${state.id}`, '', `Revision ${state.revisionId}`, '',
    `Workflow ${state.status}`, '',
    `Execution ${run?.status || 'notRun'}`, '', `Task criteria ${analysis?.acceptance?.task || 'notAssessed'}`, '',
    `Integration criteria ${analysis?.acceptance?.integration || analysis?.acceptance?.task || 'notAssessed'}`, '',
    `Numerical validation ${analysis?.acceptance?.numerical || 'notAssessed'}`, '', `Model validation ${analysis?.acceptance?.model || 'notAssessed'}`, '',
    '## Stages', '', '| Stage | Status | Attempt |', '|---|---|---|',
    ...state.stages.map(stage => `| ${stage.id} | ${stage.status} | ${stage.attempt} |`), '',
    '## Measurements', '', '| Quantity | Value | Unit | Verdict |', '|---|---|---|---|',
    ...(analysis?.criteria || []).map(item => `| ${item.quantity} | ${item.value ?? 'unavailable'} | ${item.unit} | ${item.verdict} |`), '',
    '## Scientific comparisons', '', '| Criterion | Dimension | Verdict | Limitation |', '|---|---|---|---|',
    ...(analysis?.validation?.comparisons || []).map(item => `| ${item.definitionRef} | ${item.dimension} | ${item.verdict} | ${item.limitations.join(' / ')} |`), '',
    '## Figures', '', ...figures.map(item => `- ${item.outputRefs?.[0]?.locator || item.id} (${item.status})`), '',
    '## Conditions and limitations', '', ...(scheme?.limitations || []).map(value => `- ${value}`),
    ...(analysis?.limitations || []).map(value => `- ${value}`), '', '## Knowledge', '', ...knowledge.map(item => `- ${item.id} (${item.status})`), '',
    '## Improvement proposals', '', ...improvements.map(item => `- ${item.id} (${item.status})`), ''];
  const path = join(directory, 'REPORT.md');
  await writeFile(path, lines.join('\n'), 'utf8');
  await writeFile(join(directory, 'delivery.json'), JSON.stringify({ caseId: state.id, revisionId: state.revisionId,
    run, analysis, figures, knowledge, improvements, generatedAt: utc() }, null, 2));
  return path;
}
