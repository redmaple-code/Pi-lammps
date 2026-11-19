"""Cross-check original PDB coordinates, native outputs, geometry and seed consistency."""

from datetime import datetime, timezone
import hashlib
import itertools
import json
from pathlib import Path

import gemmi
import numpy as np
from rdkit import Chem
from rdkit.Chem import rdMolAlign
from scipy.optimize import linear_sum_assignment

CASE = Path(__file__).resolve().parents[1]
OUT = CASE / 'validation-review'
OUT.mkdir(exist_ok=True)


def sha(path):
    return 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()


def pdb_atoms(path):
    records = []
    for line in path.read_text().splitlines():
        if line.startswith(('ATOM  ', 'HETATM')) and line[76:78].strip() not in ['H', 'D']:
            records.append({'key': (line[21], line[22:27].strip(), line[17:20].strip(), line[12:16].strip()),
                'element': line[76:78].strip(), 'xyz': [float(line[start:start+8]) for start in [30, 38, 46]]})
    return records


def assignment_distance(xyz_a, elements_a, xyz_b, elements_b):
    distances = np.linalg.norm(xyz_a[:, None] - xyz_b[None, :], axis=2)
    mismatch = np.array([[a != b for b in elements_b] for a in elements_a])
    a_idx, b_idx = linear_sum_assignment(distances ** 2 + mismatch * 1e6)
    assert not np.any(mismatch[a_idx, b_idx])
    return float(np.sqrt(np.mean(distances[a_idx, b_idx] ** 2))), list(zip(a_idx.tolist(), b_idx.tolist()))


structure = gemmi.read_structure(str(OUT / '1iep.cif'))
entry = json.loads((OUT / '1iep-entry.json').read_text(encoding='utf-8'))
ref = Chem.SDMolSupplier(str(CASE / 'inputs/raw/1iep_ligand.sdf'), removeHs=True)[0]
ref_xyz = ref.GetConformer().GetPositions()
ref_elements = [a.GetSymbol() for a in ref.GetAtoms()]
crystals = []
selected_crystal = None
for chain in structure[0]:
    for residue in chain:
        if residue.name != 'STI':
            continue
        atoms = [a for a in residue if a.element.name not in ['H', 'D']]
        xyz = np.array([[a.pos.x, a.pos.y, a.pos.z] for a in atoms])
        elements = [a.element.name for a in atoms]
        rmsd, mapping = assignment_distance(ref_xyz, ref_elements, xyz, elements)
        record = {'authChain': chain.name, 'residue': str(residue.seqid), 'component': 'STI',
                  'heavyAtoms': len(atoms), 'tutorialToOriginalCrystalRMSD_A': rmsd}
        crystals.append(record)
        if selected_crystal is None or rmsd < selected_crystal['rmsd']:
            selected_crystal = {'rmsd': rmsd, 'chain': chain.name, 'residue': str(residue.seqid),
                                'atoms': atoms, 'xyz': xyz, 'elements': elements, 'mapping': mapping}

protein = pdb_atoms(CASE / 'inputs/prepared/receptor-prepared.pdb')
protein_xyz = np.array([a['xyz'] for a in protein])
protein_elements = [a['element'] for a in protein]
periodic = Chem.GetPeriodicTable()
protein_vdw = np.array([periodic.GetRvdw(periodic.GetAtomicNumber(e)) for e in protein_elements])
native_protein = {}
for line in (CASE / 'inputs/prepared/receptor.pdbqt').read_text().splitlines():
    if line.startswith(('ATOM  ', 'HETATM')) and line.split()[-1] not in ['HD', 'H']:
        key = (line[21], line[22:27].strip(), line[17:20].strip(), line[12:16].strip())
        native_protein[key] = np.array([float(line[start:start+8]) for start in [30, 38, 46]])
assert set(native_protein) == {atom['key'] for atom in protein}
max_native_protein_error = max(float(np.linalg.norm(native_protein[atom['key']] - atom['xyz'])) for atom in protein)
assert max_native_protein_error == 0
experimental_atoms = {}
for chain in structure[0]:
    for residue in chain:
        for atom in residue:
            if atom.element.name not in ['H', 'D']:
                key = (chain.name, str(residue.seqid), residue.name, atom.name)
                experimental_atoms.setdefault(key, []).append(np.array([atom.pos.x, atom.pos.y, atom.pos.z]))
missing_experimental = [atom['key'] for atom in protein if atom['key'] not in experimental_atoms]
assert not missing_experimental
max_experimental_error = max(min(float(np.linalg.norm(np.asarray(atom['xyz']) - xyz))
    for xyz in experimental_atoms[atom['key']]) for atom in protein)
assert max_experimental_error < .002


def contacts(xyz):
    distances = np.linalg.norm(xyz[:, None] - protein_xyz[None, :], axis=2)
    return {f'{a["key"][0]}:{a["key"][2]}{a["key"][1]}'
            for a, column in zip(protein, distances.T) if float(column.min()) <= 4}


reference_contacts = contacts(ref_xyz)
seed_records = []
atom_error_rows = []
top_molecules = {}
pose_files = []
for seed in [104729, 130363, 155921]:
    output = CASE / 'runs' / f'seed-{seed}'
    molecule = Chem.SDMolSupplier(str(output / 'poses.sdf'), removeHs=True)[0]
    assert molecule is not None
    xyz = molecule.GetConformer().GetPositions()
    assert Chem.MolToSmiles(molecule) == Chem.MolToSmiles(ref)
    maps = molecule.GetSubstructMatches(ref, uniquify=False, useChirality=True, maxMatches=1000000)
    values = [float(np.sqrt(np.mean(np.sum((xyz[list(mapping)] - ref_xyz) ** 2, axis=1)))) for mapping in maps]
    best_map = maps[int(np.argmin(values))]
    atom_errors = np.linalg.norm(xyz[list(best_map)] - ref_xyz, axis=1)
    crystal_atom_by_reference = {a: selected_crystal['atoms'][b].name for a, b in selected_crystal['mapping']}
    for ref_index, error in enumerate(atom_errors):
        atom_error_rows.append({'seed': seed, 'referenceAtomIndex': ref_index,
            'crystalAtomName': crystal_atom_by_reference[ref_index], 'element': ref_elements[ref_index],
            'displacement_A': float(error)})
    predicted_contacts = contacts(xyz)
    distances = np.linalg.norm(xyz[:, None] - protein_xyz[None, :], axis=2)
    ligand_vdw = np.array([periodic.GetRvdw(a.GetAtomicNum()) for a in molecule.GetAtoms()])
    ratios = distances / (ligand_vdw[:, None] + protein_vdw[None, :])
    # Parse the first native PDBQT model independently of Meeko restoration.
    native_xyz, native_elements = [], []
    type_elements = {'A': 'C', 'C': 'C', 'N': 'N', 'NA': 'N', 'NS': 'N',
                     'OA': 'O', 'OS': 'O', 'O': 'O', 'S': 'S', 'SA': 'S'}
    for line in (output / 'poses.pdbqt').read_text().splitlines():
        if line.startswith('ENDMDL'):
            break
        if line.startswith(('ATOM  ', 'HETATM')):
            atom_type = line.split()[-1]
            if atom_type in ['HD', 'H']:
                continue
            native_xyz.append([float(line[start:start+8]) for start in [30, 38, 46]])
            native_elements.append(type_elements.get(atom_type, atom_type))
    native_xyz = np.array(native_xyz)
    restored_distance, _ = assignment_distance(xyz, [a.GetSymbol() for a in molecule.GetAtoms()], native_xyz, native_elements)
    crystal_distance, _ = assignment_distance(native_xyz, native_elements, selected_crystal['xyz'], selected_crystal['elements'])
    assert restored_distance < 1e-4
    sdf_file = OUT / f'top1-seed-{seed}.sdf'
    with Chem.SDWriter(str(sdf_file)) as writer:
        writer.write(molecule)
    pose_files.append(sdf_file.name)
    seed_records.append({'seed': seed, 'heavyAtoms': molecule.GetNumAtoms(),
        'fixedFrameChemicalRMSD_A': min(values), 'maxMappedAtomDisplacement_A': float(atom_errors.max()),
        'nativePDBQTvsRestoredSDF_RMSD_A': restored_distance,
        'nativeToOriginalCrystalElementAssignmentRMSD_A': crystal_distance,
        'minimumHeavyAtomDistance_A': float(distances.min()),
        'minimumVDWDistanceRatio': float(ratios.min()),
        'atomPairsBelowVDWRatio0_75': int((ratios < .75).sum()),
        'referenceContactResidues': sorted(reference_contacts),
        'predictedContactResidues': sorted(predicted_contacts),
        'contactJaccard4A': len(reference_contacts & predicted_contacts) / len(reference_contacts | predicted_contacts),
        'recoveredReferenceContactFraction': len(reference_contacts & predicted_contacts) / len(reference_contacts),
        'contactDefinition': 'Any heavy atom within4A; geometric proximity only.'})
    top_molecules[seed] = molecule
pairs = [{'seeds': [a, b], 'fixedFrameSymmetricRMSD_A': rdMolAlign.CalcRMS(top_molecules[a], top_molecules[b])}
         for a, b in itertools.combinations(top_molecules, 2)]
provenance = []
for name, url in [('1iep.cif', 'https://files.rcsb.org/download/1IEP.cif'),
                  ('1iep-entry.json', 'https://data.rcsb.org/rest/v1/core/entry/1IEP')]:
    provenance.append({'file': name, 'url': url, 'digest': sha(OUT / name)})
result = {'reviewedAt': datetime.now(timezone.utc).isoformat(),
    'scope': 'Existing outputs, original crystallography and supplementary physical plausibility.',
    'experimentalEntry': {'id': '1IEP', 'title': entry['struct']['title'],
        'method': entry['exptl'][0]['method'], 'resolution_A': entry['rcsb_entry_info']['resolution_combined'],
        'crystalLigands': crystals, 'chosenAuthChain': selected_crystal['chain'],
        'chosenResidue': selected_crystal['residue']}, 'sources': provenance,
    'seedResults': seed_records, 'seedPairs': pairs, 'poseBustersInputs': pose_files,
    'nativeReceptorCheck': {'solverInputHeavyAtoms': len(native_protein),
        'maxHeavyAtomDisplacementVsPreparedPDB_A': max_native_protein_error,
        'maxPreparedHeavyAtomDisplacementVsOriginalCrystal_A': max_experimental_error,
        'missingExperimentalAtomKeys': missing_experimental},
    'perAtomDisplacements': atom_error_rows,
    'notes': ['Element-only assignment against crystallography is a coordinate-provenance cross-check.',
              'The reported primary RMSD uses chemical-graph-preserving mappings.',
              'Contact4A denotes geometric proximity, without assigning hydrogen bonds.']}
(OUT / 'cross-checks.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'crystalLigands': crystals, 'seedResults': seed_records, 'seedPairs': pairs}, indent=2), flush=True)
