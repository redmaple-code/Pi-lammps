"""Restore real Vina poses and compare them with the fixed crystallographic reference."""

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
from meeko import PDBQTMolecule, RDKitMolCreate

CASE = Path(__file__).resolve().parents[1]
PLAN = json.loads((CASE / 'evidence/run-plan.json').read_text(encoding='utf-8'))
REFERENCE = Chem.SDMolSupplier(str(CASE / 'inputs/prepared/reference-heavy.sdf'), removeHs=True)[0]
REF_SMILES = Chem.MolToSmiles(REFERENCE)


def sha(path):
    return 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()


def protein_atoms(path):
    atoms = {}
    for line in path.read_text().splitlines():
        if line.startswith(('ATOM  ', 'HETATM')):
            element = line[76:78].strip()
            if element.upper() in ['H', 'D']:
                continue
            key = (line[21], line[22:27].strip(), line[17:20], line[12:16].strip())
            atoms[key] = np.array([float(line[30:38]), float(line[38:46]), float(line[46:54])])
    return atoms


raw_atoms = protein_atoms(CASE / 'inputs/raw/1iep_receptorH.pdb')
prepared_atoms = protein_atoms(CASE / 'inputs/prepared/receptor-prepared.pdb')
common_keys = raw_atoms.keys() & prepared_atoms.keys()
if not common_keys:
    raise RuntimeError('Receptor coordinates have no common atom identities.')
max_change = max(float(np.linalg.norm(raw_atoms[key] - prepared_atoms[key])) for key in common_keys)
if max_change > .002:
    raise RuntimeError(f'Receptor coordinate frame changed unexpectedly: {max_change}')
receptor_positions = np.array(list(prepared_atoms.values()))
rows = []
representative = None
summaries = []
atom_maps = []
for seed in PLAN['seeds']:
    directory = CASE / 'runs' / f'seed-{seed}'
    receipt = json.loads((directory / 'run.json').read_text(encoding='utf-8'))
    if receipt['status'] != 'completed' or receipt['exitCode'] != 0:
        raise RuntimeError(f'Run {seed} is incomplete.')
    pdbqt_file = directory / 'poses.pdbqt'
    if sha(pdbqt_file) != receipt['outputDigest']:
        raise RuntimeError('Pose output changed after run receipt.')
    energies = [float(match.group(1)) for match in re.finditer(r'REMARK VINA RESULT:\s+([-\d.]+)', pdbqt_file.read_text())]
    pdbqt = PDBQTMolecule.from_file(str(pdbqt_file), skip_typing=True)
    molecules = [m for m in RDKitMolCreate.from_pdbqt_mol(pdbqt) if m is not None]
    if len(molecules) != 1:
        raise RuntimeError(f'Expected one restored ligand, got {len(molecules)}')
    molecule = molecules[0]
    heavy = Chem.RemoveHs(molecule)
    if Chem.MolToSmiles(heavy) != REF_SMILES:
        raise RuntimeError('Restored ligand chemical identity differs from reference.')
    if len(energies) != heavy.GetNumConformers():
        raise RuntimeError('Score count and restored pose count disagree.')
    matches = heavy.GetSubstructMatches(REFERENCE, uniquify=False, useChirality=True, maxMatches=1000000)
    if not matches:
        raise RuntimeError('No chemical atom correspondence to reference.')
    with Chem.SDWriter(str(directory / 'poses.sdf')) as writer:
        for idx, conformer in enumerate(heavy.GetConformers()):
            rmsd = rdMolAlign.CalcRMS(heavy, REFERENCE, prbId=conformer.GetId(), refId=0,
                maxMatches=1000000, symmetrizeConjugatedTerminalGroups=True)
            coordinates = conformer.GetPositions()
            if idx == 0:
                reference_coordinates = REFERENCE.GetConformer().GetPositions()
                explicit_values = [float(np.sqrt(np.mean(np.sum((coordinates[list(match)] - reference_coordinates) ** 2, axis=1))))
                                   for match in matches]
                best_index = int(np.argmin(explicit_values))
                if abs(explicit_values[best_index] - rmsd) > 1e-6:
                    raise RuntimeError('Direct atom-mapped RMSD and CalcRMS disagree.')
                atom_maps.append({'seed': seed, 'poseRank': 1, 'indexBase': 0,
                    'query': 'reference', 'target': 'restoredPose',
                    'referenceIndexToPoseIndex': list(matches[best_index]),
                    'chemicalMatchCount': len(matches), 'explicitRMSD_A': explicit_values[best_index],
                    'CalcRMS_A': rmsd, 'coordinateRealignmentApplied': False})
            distances = np.linalg.norm(coordinates[:, None, :] - receptor_positions[None, :, :], axis=2)
            minimum_distance = float(distances.min())
            contacts = sorted({f'{key[0]}:{key[2].strip()}{key[1]}' for key, column in zip(prepared_atoms, distances.T)
                               if float(column.min()) <= 4.0})
            heavy.SetDoubleProp('vina_score_kcal_per_mol', energies[idx])
            heavy.SetDoubleProp('reference_RMSD_A', rmsd)
            heavy.SetIntProp('seed', seed)
            heavy.SetIntProp('pose_rank', idx + 1)
            writer.write(heavy, confId=conformer.GetId())
            row = {'seed': seed, 'poseRank': idx + 1, 'score_kcal_per_mol': energies[idx],
                   'referenceHeavyAtomRMSD_A': rmsd, 'within2A': rmsd <= 2,
                   'minimumReceptorHeavyAtomDistance_A': minimum_distance,
                   'severeOverlapBelow1_2A': minimum_distance < 1.2,
                   'contactResiduesWithin4A': '|'.join(contacts),
                   'source': f'runs/seed-{seed}/poses.pdbqt'}
            rows.append(row)
            if representative is None and idx == 0:
                representative = Chem.Mol(heavy)
                representative_conf = conformer.GetId()
    seed_rows = [row for row in rows if row['seed'] == seed]
    summaries.append({'seed': seed, 'modeCount': len(seed_rows),
        'top1Score_kcal_per_mol': seed_rows[0]['score_kcal_per_mol'],
        'top1ReferenceRMSD_A': seed_rows[0]['referenceHeavyAtomRMSD_A'],
        'bestOf5ReferenceRMSD_A': min(row['referenceHeavyAtomRMSD_A'] for row in seed_rows[:5]),
        'durationSeconds': receipt['durationSeconds']})
with (CASE / 'analysis/all-poses.csv').open('w', encoding='utf-8-sig', newline='') as stream:
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
with (CASE / 'analysis/run-metrics.csv').open('w', encoding='utf-8-sig', newline='') as stream:
    writer = csv.DictWriter(stream, fieldnames=list(summaries[0]))
    writer.writeheader()
    writer.writerows(summaries)
top1_hits = sum(row['top1ReferenceRMSD_A'] <= 2 for row in summaries)
best5_hits = sum(row['bestOf5ReferenceRMSD_A'] <= 2 for row in summaries)
result = {'dataOrigin': 'realVinaRuns', 'poseCount': len(rows), 'seeds': summaries,
    'referenceHeavyAtoms': REFERENCE.GetNumAtoms(), 'referenceSmiles': REF_SMILES,
    'metricMethod': 'RDKit CalcRMS with symmetry matching and no ligand realignment; fixed receptor frame.',
    'coordinateFrameCheck': {'commonReceptorHeavyAtoms': len(common_keys),
        'originalHeavyAtoms': len(raw_atoms), 'preparedHeavyAtoms': len(prepared_atoms),
        'maxSharedHeavyAtomDisplacement_A': max_change,
        'missingPreparedAtomKeys': [list(key) for key in raw_atoms.keys() - prepared_atoms.keys()],
        'addedPreparedAtomKeys': [list(key) for key in prepared_atoms.keys() - raw_atoms.keys()]},
    'top1Recovered': top1_hits, 'bestOf5Recovered': best5_hits, 'denominator': len(summaries),
    'predeclaredPointTargetMet': top1_hits >= 2,
    'severeOverlapPoseCount': sum(row['severeOverlapBelow1_2A'] for row in rows),
    'limitations': ['One tutorial receptor and one ligand charge state.',
        'Three seeds describe a small method-verification sample.',
        'RMSD and scores do not provide experimental affinity measurements.']}
(CASE / 'analysis/results.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
(CASE / 'analysis/atom-maps.json').write_text(json.dumps(atom_maps, indent=2) + '\n', encoding='utf-8')

fig, axes = plt.subplots(1, 3, figsize=(16, 4.7), layout='constrained')
fig.suptitle('Actual AutoDock Vina 1.2.7 | 1IEP / imatinib redocking', fontsize=15, weight='bold')
labels = [str(row['seed']) for row in summaries]
axes[0].bar(labels, [row['top1Score_kcal_per_mol'] for row in summaries], color='#218c83')
axes[0].set_ylabel('Top-1 Vina score (kcal/mol)')
axes[0].set_xlabel('Predeclared seed')
positions = np.arange(len(summaries))
axes[1].bar(positions - .18, [row['top1ReferenceRMSD_A'] for row in summaries], .36, label='Top 1', color='#218c83')
axes[1].bar(positions + .18, [row['bestOf5ReferenceRMSD_A'] for row in summaries], .36, label='Best of 5', color='#4679ba')
axes[1].axhline(2, color='#c66b28', ls='--', label='Predeclared 2 A')
axes[1].set_xticks(positions, labels)
axes[1].set_ylabel('Reference heavy-atom RMSD (A)')
axes[1].legend(fontsize=8)
for seed in PLAN['seeds']:
    group = [row for row in rows if row['seed'] == seed]
    axes[2].scatter([row['referenceHeavyAtomRMSD_A'] for row in group],
        [row['score_kcal_per_mol'] for row in group], label=str(seed), alpha=.75)
axes[2].axvline(2, color='#c66b28', ls='--')
axes[2].set_xlabel('Reference heavy-atom RMSD (A)')
axes[2].set_ylabel('Vina score (kcal/mol)')
axes[2].legend(fontsize=8)
for axis in axes:
    axis.spines[['top', 'right']].set_visible(False)
fig.savefig(CASE / 'figures/real-redocking-analysis.png', dpi=150)
plt.close(fig)

# Spatial view retains actual receptor coordinates and chemical bonds.
ref_coords = REFERENCE.GetConformer().GetPositions()
pose_coords = representative.GetConformer(representative_conf).GetPositions()
fig = plt.figure(figsize=(9, 7), layout='constrained')
axis = fig.add_subplot(111, projection='3d')
center = ref_coords.mean(axis=0)
pocket = receptor_positions[np.linalg.norm(receptor_positions - center, axis=1) < 12]
axis.scatter(pocket[:, 0], pocket[:, 1], pocket[:, 2], color='#929ca5', alpha=.18, s=5)
for mol, xyz, color, label in [(REFERENCE, ref_coords, '#167f55', 'Reference'),
                               (representative, pose_coords, '#c56b2c', f'Seed {PLAN["seeds"][0]} top 1')]:
    for bond in mol.GetBonds():
        points = xyz[[bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()]]
        axis.plot(points[:, 0], points[:, 1], points[:, 2], color=color, lw=2)
    axis.scatter(xyz[:, 0], xyz[:, 1], xyz[:, 2], color=color, s=12, label=label)
axis.set_xlabel('X (A)')
axis.set_ylabel('Y (A)')
axis.set_zlabel('Z (A)')
axis.set_title('Actual coordinates | reference and docked ligand in fixed receptor frame')
axis.legend()
axis.set_box_aspect((1, 1, 1))
fig.savefig(CASE / 'figures/reference-pose-overlay.png', dpi=150)
plt.close(fig)
print(json.dumps(result, indent=2), flush=True)
