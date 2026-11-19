"""Compare every official output pose with fresh benchmark runs and crystallography."""

from datetime import datetime, timezone
import csv
import hashlib
import json
from pathlib import Path
import re

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from rdkit import Chem
from rdkit.Chem import rdMolAlign
from scipy.optimize import linear_sum_assignment
from meeko import PDBQTMolecule, RDKitMolCreate
from posebusters import PoseBusters

CASE = Path(__file__).resolve().parents[1]


def dump(relative, value):
    (CASE / relative).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def table(relative, rows):
    with (CASE / relative).open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def read_poses(relative):
    file = CASE / relative
    scores = [float(value) for value in re.findall(r'REMARK VINA RESULT:\s+([-\d.]+)', file.read_text())]
    restored = [m for m in RDKitMolCreate.from_pdbqt_mol(PDBQTMolecule.from_file(str(file), skip_typing=True)) if m is not None]
    if len(restored) != 1:
        raise RuntimeError(f'Expected one ligand: {relative}')
    molecule = Chem.RemoveHs(restored[0])
    if len(scores) != molecule.GetNumConformers():
        raise RuntimeError('Pose/score count mismatch.')
    poses = []
    for conformer, score in zip(molecule.GetConformers(), scores):
        pose = Chem.Mol(molecule)
        pose.RemoveAllConformers()
        pose.AddConformer(Chem.Conformer(conformer), assignId=True)
        pose.SetDoubleProp('VinaScore_kcal_per_mol', score)
        poses.append(pose)
    return poses, scores


def rmsd(a, b):
    return rdMolAlign.CalcRMS(a, b, maxMatches=1000000, symmetrizeConjugatedTerminalGroups=True)


plan = json.loads((CASE / 'evidence/run-plan.json').read_text(encoding='utf-8'))
reference, reference_scores = read_poses('reference/official-vina-output.pdbqt')
crystal = Chem.SDMolSupplier(str(CASE / 'reference/crystal-ligand.sdf'), removeHs=True)[0]
chemical_identity = Chem.MolToSmiles(crystal)
assert len(reference) == 4 and reference_scores[0] == -13.234
assert all(Chem.MolToSmiles(molecule) == chemical_identity for molecule in reference)
with Chem.SDWriter(str(CASE / 'reference/official-output.sdf')) as writer:
    for rank, molecule in enumerate(reference, 1):
        molecule.SetIntProp('officialPoseRank', rank)
        writer.write(molecule)

pose_rows = []
budget_rows = []
runs = {}
for effort in [8, 16, 32]:
    name = f'e{effort}'
    receipt = json.loads((CASE / 'runs' / name / 'run.json').read_text(encoding='utf-8'))
    if receipt['status'] != 'completed' or receipt['exitCode'] != 0:
        raise RuntimeError(f'Run is incomplete: {name}')
    molecules, scores = read_poses(f'runs/{name}/poses.pdbqt')
    assert all(Chem.MolToSmiles(molecule) == chemical_identity for molecule in molecules)
    with Chem.SDWriter(str(CASE / 'runs' / name / 'poses.sdf')) as writer:
        for rank, molecule in enumerate(molecules, 1):
            molecule.SetIntProp('poseRank', rank)
            writer.write(molecule)
            pose_rows.append({'runId': name, 'exhaustiveness': effort, 'poseRank': rank,
                'score_kcal_per_mol': scores[rank - 1], 'crystalRMSD_A': rmsd(molecule, crystal),
                'officialTop1RMSD_A': rmsd(molecule, reference[0])})
    runs[name] = molecules
    budget_rows.append({'runId': name, 'exhaustiveness': effort, 'seed': plan['seed'],
        'top1Score_kcal_per_mol': scores[0], 'officialTop1Score_kcal_per_mol': reference_scores[0],
        'scoreDifference_kcal_per_mol': scores[0] - reference_scores[0],
        'top1CrystalRMSD_A': rmsd(molecules[0], crystal),
        'top1OfficialPoseRMSD_A': rmsd(molecules[0], reference[0]),
        'poseCount': len(molecules), 'durationSeconds': receipt['durationSeconds']})
table('analysis/all-poses.csv', pose_rows)
table('analysis/search-budget-comparison.csv', budget_rows)

matrix = np.array([[rmsd(a, b) for b in runs['e32']] for a in reference])
reference_indices, predicted_indices = linear_sum_assignment(matrix)
assignment = dict(zip(reference_indices.tolist(), predicted_indices.tolist()))
comparison = []
for idx, source_pose in enumerate(reference):
    match = assignment.get(idx)
    predicted_score = next((row['score_kcal_per_mol'] for row in pose_rows if row['runId'] == 'e32' and row['poseRank'] == match + 1), None) if match is not None else None
    comparison.append({'officialPoseRank': idx + 1, 'officialScore_kcal_per_mol': reference_scores[idx],
        'officialCrystalRMSD_A': rmsd(source_pose, crystal),
        'matchedComputedRank': match + 1 if match is not None else '',
        'computedScore_kcal_per_mol': predicted_score if match is not None else '',
        'scoreDifference_kcal_per_mol': predicted_score - reference_scores[idx] if match is not None else '',
        'fixedFramePairRMSD_A': float(matrix[idx, match]) if match is not None else '',
        'nearestComputedRank': int(np.argmin(matrix[idx])) + 1,
        'nearestComputedRMSD_A': float(np.min(matrix[idx])),
        'poseMatchWithin2A': bool(matrix[idx, match] <= 2) if match is not None else False})
table('analysis/official-output-comparison.csv', comparison)
table('analysis/pose-rmsd-matrix.csv', [dict(officialPoseRank=i + 1,
    **{f'computedRank{j + 1}': float(value) for j, value in enumerate(row)}) for i, row in enumerate(matrix)])

fixed_log = (CASE / 'runs/fixed-score/vina.log').read_text()
fixed_score = float(re.search(r'Estimated Free Energy of Binding\s*:\s*([-\d.]+)', fixed_log).group(1))
main = next(row for row in budget_rows if row['runId'] == 'e32')
main_rows = [row for row in pose_rows if row['runId'] == 'e32']
best5 = min(main_rows[:5], key=lambda row: row['crystalRMSD_A'])
# Geometric proximity only; chemical interaction types require further assignment.
contacts = []
xyz = runs['e32'][0].GetConformer().GetPositions()
for line in (CASE / 'inputs/official/1iep_receptor.pdbqt').read_text().splitlines():
    if not line.startswith(('ATOM  ', 'HETATM')) or line.split()[-1] in ['H', 'HD', 'HS']:
        continue
    point = np.array([float(line[30:38]), float(line[38:46]), float(line[46:54])])
    distances = np.linalg.norm(xyz - point, axis=1)
    for atom_idx in np.where(distances <= 4)[0]:
        contacts.append({'ligandHeavyAtomIndex0': int(atom_idx), 'ligandElement': runs['e32'][0].GetAtomWithIdx(int(atom_idx)).GetSymbol(),
            'receptorAtom': line[12:16].strip(), 'residue': line[17:20].strip(), 'chain': line[21:22].strip(),
            'residueNumber': line[22:26].strip(), 'distance_A': float(distances[atom_idx]),
            'interpretation': 'heavy-atom proximity <=4 A; not a typed bond or interaction'})
table('analysis/main-top1-contacts.csv', sorted(contacts, key=lambda row: row['distance_A']))
with Chem.SDWriter(str(CASE / 'analysis/main-top1.sdf')) as writer:
    writer.write(runs['e32'][0])
buster = PoseBusters(config='redock', max_workers=0)
dump('evidence/posebusters-config.json', buster.config)
pb = buster.bust(mol_pred=CASE / 'runs/e32/poses.sdf', mol_true=CASE / 'reference/crystal-ligand.sdf',
                 mol_cond=CASE / 'reference/receptorH.pdb', full_report=False)
pb.to_csv(CASE / 'analysis/posebusters-main.csv')
top_pb = pb.iloc[0]
top_failures = [name for name in pb.columns if not (top_pb[name] == True)]
pb_summary = {'tool': 'PoseBusters', 'configuration': 'unmodified redock defaults',
    'checksPerPose': len(pb.columns), 'poseCount': len(pb), 'top1FailedChecks': top_failures,
    'rows': [{'index': str(index), 'failedChecks': [name for name in pb.columns if not (row[name] == True)]}
             for index, row in pb.iterrows()]}
dump('analysis/posebusters-summary.json', pb_summary)

metrics = {'caseId': plan['caseId'], 'generatedAt': datetime.now(timezone.utc).isoformat(),
    'reproductionLevel': 'method_reproduction', 'referenceTop1Score_kcal_per_mol': reference_scores[0],
    'referenceTop1CrystalRMSD_A': rmsd(reference[0], crystal),
    'fixedReferencePoseCurrentScore_kcal_per_mol': fixed_score,
    'fixedPoseScoreDifference_kcal_per_mol': fixed_score - reference_scores[0],
    'mainRun': main, 'searchBudgetStudy': budget_rows, 'officialModes': len(reference),
    'bestOfUpTo5CrystalPose': best5, 'availablePosesWithinTop5': min(5, len(main_rows)),
    'top1HeavyAtomContactsWithin4A': len(contacts),
    'mainModes': len(runs['e32']), 'matchedOfficialModesWithin2A': sum(row['poseMatchWithin2A'] for row in comparison),
    'top1PoseBustersChecks': len(pb.columns), 'top1PoseBustersFailures': top_failures,
    'primaryCriteria': {'scoreDifference': abs(main['scoreDifference_kcal_per_mol']) <= .5,
        'officialPoseRMSD': main['top1OfficialPoseRMSD_A'] <= 2,
        'crystalRMSD': main['top1CrystalRMSD_A'] <= 2, 'posePlausibility': not top_failures},
    'limitations': plan['referenceVersionLimitations']}
dump('analysis/metrics.json', metrics)

conclusions = [
    {'id': 'C01', 'claim': 'Official inputs preserved byte-for-byte', 'verdict': 'match', 'evidence': 'sources/source-lock.json; evidence/run-plan.json'},
    {'id': 'C02', 'claim': 'Current scorer recovers reference-pose energy', 'verdict': 'match' if abs(metrics['fixedPoseScoreDifference_kcal_per_mol']) <= .5 else 'contradicted', 'evidence': 'runs/fixed-score/vina.log'},
    {'id': 'C03', 'claim': 'Main top1 energy agrees with official reference', 'verdict': 'match' if metrics['primaryCriteria']['scoreDifference'] else 'contradicted', 'evidence': 'analysis/search-budget-comparison.csv'},
    {'id': 'C04', 'claim': 'Main top1 recovers official and crystal poses', 'verdict': 'match' if metrics['primaryCriteria']['officialPoseRMSD'] and metrics['primaryCriteria']['crystalRMSD'] else 'contradicted', 'evidence': 'analysis/metrics.json'},
    {'id': 'C05', 'claim': 'All four official modes have current counterparts', 'verdict': 'match' if metrics['matchedOfficialModesWithin2A'] == 4 else 'partial', 'evidence': 'analysis/official-output-comparison.csv'},
    {'id': 'C06', 'claim': 'Main top1 passes configured physical plausibility checks', 'verdict': 'match' if not top_failures else 'contradicted', 'evidence': 'analysis/posebusters-main.csv'},
    {'id': 'C07', 'claim': '8/16/32 search-budget study executed with fixed inputs/seed', 'verdict': 'match', 'evidence': 'analysis/search-budget-comparison.csv'},
    {'id': 'C08', 'claim': 'Bitwise reproduction of original hardware/producer version', 'verdict': 'not_tested', 'evidence': 'evidence/run-plan.json: reference provenance gaps'}]
table('ledgers/conclusion_ledger.csv', conclusions)
fig, axes = plt.subplots(1, 3, figsize=(15, 4.8), layout='constrained')
fig.suptitle('AutoDock Vina official 1IEP benchmark | frozen official inputs and output comparison', weight='bold')
x = [row['exhaustiveness'] for row in budget_rows]
axes[0].plot(x, [row['top1Score_kcal_per_mol'] for row in budget_rows], 'o-', color='#178579', label='Current top 1')
axes[0].axhline(reference_scores[0], color='#c8752e', ls='--', label='Official -13.234')
axes[0].set_xlabel('Exhaustiveness')
axes[0].set_ylabel('Vina score (kcal/mol)')
axes[0].legend(fontsize=8)
axes[1].plot(x, [row['top1OfficialPoseRMSD_A'] for row in budget_rows], 'o-', label='vs official pose')
axes[1].plot(x, [row['top1CrystalRMSD_A'] for row in budget_rows], 's-', label='vs crystal')
axes[1].axhline(2, color='#c8752e', ls='--', label='2 A target')
axes[1].set_xlabel('Exhaustiveness')
axes[1].set_ylabel('Heavy-atom RMSD (A)')
axes[1].legend(fontsize=8)
image = axes[2].imshow(matrix, cmap='Blues', vmin=0)
axes[2].set_xlabel('Current main-run pose rank')
axes[2].set_ylabel('Official pose rank')
axes[2].set_xticks(range(len(runs['e32'])), range(1, len(runs['e32']) + 1))
axes[2].set_yticks(range(4), range(1, 5))
for i, j in zip(reference_indices, predicted_indices):
    axes[2].text(j, i, f'{matrix[i,j]:.2f}', ha='center', va='center',
        color='white' if matrix[i,j] > matrix.max() / 2 else 'black')
fig.colorbar(image, ax=axes[2], label='Pairwise RMSD (A)', shrink=.8)
fig.savefig(CASE / 'figures/reference-comparison.png', dpi=150)
plt.close(fig)

fig = plt.figure(figsize=(9, 7), layout='constrained')
axis = fig.add_subplot(111, projection='3d')
for molecule, color, label in [(crystal, '#16815b', 'Crystal'), (reference[0], '#4a70b4', 'Official output top 1'),
                                (runs['e32'][0], '#c8752e', 'Current top 1')]:
    xyz = molecule.GetConformer().GetPositions()
    for bond in molecule.GetBonds():
        points = xyz[[bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()]]
        axis.plot(points[:,0], points[:,1], points[:,2], color=color, lw=1.8)
    axis.scatter(xyz[:,0], xyz[:,1], xyz[:,2], color=color, s=10, label=label)
axis.set_xlabel('X (A)')
axis.set_ylabel('Y (A)')
axis.set_zlabel('Z (A)')
axis.set_title('Fixed receptor coordinate frame | no ligand realignment')
axis.legend()
axis.set_box_aspect((1, 1, 1))
axis.set_aspect('equal')
fig.savefig(CASE / 'figures/three-way-pose-overlay.png', dpi=150)
plt.close(fig)
table('ledgers/figure_ledger.csv', [
    {'id': 'F01', 'referenceObject': 'Four official output modes and documented best energy',
     'currentArtifact': 'figures/reference-comparison.png', 'alignedData': 'analysis/official-output-comparison.csv',
     'residualData': 'analysis/search-budget-comparison.csv', 'status': 'generated'},
    {'id': 'F02', 'referenceObject': 'Crystallographic and official top1 coordinates',
     'currentArtifact': 'figures/three-way-pose-overlay.png', 'alignedData': 'analysis/pose-rmsd-matrix.csv',
     'residualData': 'analysis/official-output-comparison.csv', 'status': 'generated'}])
print(json.dumps(metrics, indent=2), flush=True)
