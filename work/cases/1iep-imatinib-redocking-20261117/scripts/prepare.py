"""Prepare a public crystallographic redocking reference and a new search conformer."""

from datetime import datetime, timezone
from collections import Counter
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import numpy as np
from rdkit import Chem
from rdkit.Chem import AllChem, rdMolAlign
from meeko import MoleculePreparation, PDBQTWriterLegacy

CASE = Path(__file__).resolve().parents[1]
PREPARED = CASE / 'inputs/prepared'


def sha(path):
    return 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


reference = Chem.SDMolSupplier(str(CASE / 'inputs/raw/1iep_ligand.sdf'), removeHs=False)[0]
if reference is None:
    raise RuntimeError('Reference ligand did not parse.')
reference_heavy = Chem.RemoveHs(reference)
graph = Chem.Mol(reference_heavy)
graph.RemoveAllConformers()
initial = Chem.AddHs(graph)
parameters = AllChem.ETKDGv3()
parameters.randomSeed = 20261117
if AllChem.EmbedMolecule(initial, parameters) != 0:
    raise RuntimeError('ETKDG embedding failed.')
if not AllChem.MMFFHasAllMoleculeParams(initial):
    raise RuntimeError('MMFF parameters incomplete for this ligand.')
optimization_code = AllChem.MMFFOptimizeMolecule(initial, maxIters=2000)
if optimization_code != 0:
    raise RuntimeError(f'MMFF initial-conformer optimization did not converge: {optimization_code}')
with Chem.SDWriter(str(PREPARED / 'ligand-initial.sdf')) as writer:
    writer.write(initial)
with Chem.SDWriter(str(PREPARED / 'reference-heavy.sdf')) as writer:
    writer.write(reference_heavy)
initial_heavy = Chem.RemoveHs(initial)
initial_rmsd = rdMolAlign.CalcRMS(initial_heavy, reference_heavy)
preparation = MoleculePreparation()
setups = preparation.prepare(initial)
if len(setups) != 1:
    raise RuntimeError(f'Unexpected number of ligand setups: {len(setups)}')
pdbqt, success, error = PDBQTWriterLegacy.write_string(setups[0])
if not success:
    raise RuntimeError(error)
(PREPARED / 'ligand.pdbqt').write_text(pdbqt, encoding='utf-8')
# The tutorial PDB starts with an A:438 hydrogen before residue A:225.
# Group its existing ATOM records so Meeko can parse contiguous residues.
original_lines = (CASE / 'inputs/raw/1iep_receptorH.pdb').read_text().splitlines()
atom_records = [line for line in original_lines if line.startswith('ATOM')]
ordered_atoms = sorted(atom_records, key=lambda line: (line[21], int(line[22:26]), line[26], int(line[6:11])))
iterator = iter(ordered_atoms)
normalized_lines = [next(iterator) if line.startswith('ATOM') else line for line in original_lines]
if Counter(atom_records) != Counter(line for line in normalized_lines if line.startswith('ATOM')):
    raise RuntimeError('Receptor atom records changed while grouping residues.')
normalized_receptor = PREPARED / 'receptor-grouped.pdb'
normalized_receptor.write_text('\n'.join(normalized_lines) + '\n', encoding='utf-8')
write_json(CASE / 'evidence/receptor-record-grouping.json', {
    'source': 'inputs/raw/1iep_receptorH.pdb', 'output': 'inputs/prepared/receptor-grouped.pdb',
    'operation': 'Sort existing ATOM records by chain/residue/insertion code/serial.',
    'atomRecordCount': len(atom_records), 'atomRecordMultisetUnchanged': True,
    'coordinatesUnchanged': True, 'atomsDeleted': 0, 'atomsAdded': 0})
command = [sys.executable, '-m', 'meeko.cli.mk_prepare_receptor',
           '--read_pdb', str(normalized_receptor),
           '-o', str(PREPARED / 'receptor'), '-p', '-v', '-j',
           '--write_pdb', str(PREPARED / 'receptor-prepared.pdb'),
           '--box_size', '20', '20', '20', '--box_center', '15.190', '53.903', '16.917']
started = datetime.now(timezone.utc).isoformat()
timer = time.monotonic()
with (CASE / 'logs/receptor-preparation.log').open('w', encoding='utf-8') as log:
    result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=180)
receipt = {'argv': command, 'startedAt': started,
           'endedAt': datetime.now(timezone.utc).isoformat(),
           'durationSeconds': time.monotonic() - timer, 'exitCode': result.returncode,
           'log': 'logs/receptor-preparation.log'}
write_json(CASE / 'evidence/receptor-command.json', receipt)
if result.returncode:
    print((CASE / 'logs/receptor-preparation.log').read_text(encoding='utf-8'))
    raise RuntimeError('Receptor preparation failed.')
if not (PREPARED / 'receptor.pdbqt').is_file():
    raise RuntimeError('Receptor command did not generate PDBQT.')
coordinates = reference_heavy.GetConformer().GetPositions()
center = np.array([15.190, 53.903, 16.917])
inside = bool(np.all(np.abs(coordinates - center) <= 10))
if not inside:
    raise RuntimeError('Reference heavy atoms extend outside the planned search box.')
protein_lines = (CASE / 'inputs/raw/1iep_receptorH.pdb').read_text().splitlines()
hetero = [line for line in protein_lines if line.startswith('HETATM')]
chains = sorted({line[21] for line in protein_lines if line.startswith('ATOM')})
chemical_smiles = Chem.MolToSmiles(reference_heavy)
if chemical_smiles != Chem.MolToSmiles(initial_heavy):
    raise RuntimeError('Ligand chemical identity changed during initialization.')
record = {'actualExecution': True, 'reference': 'inputs/raw/1iep_ligand.sdf',
          'referenceDigest': sha(CASE / 'inputs/raw/1iep_ligand.sdf'),
          'receptorChains': chains, 'inputReceptorHeteroRecords': len(hetero),
          'ligandHeavyAtoms': reference_heavy.GetNumAtoms(),
          'ligandTotalAtoms': reference.GetNumAtoms(),
          'formalCharge': Chem.GetFormalCharge(reference_heavy), 'canonicalSmiles': chemical_smiles,
          'initialConformer': 'ETKDGv3 from molecular graph, seed20261117, MMFF max2000',
          'MMFFExitCode': optimization_code, 'initialUnalignedReferenceRMSD_A': initial_rmsd,
          'referenceHeavyAtomsInsideBox': inside, 'fixedChemicalState': True,
          'preparedFiles': [{'path': str(file.relative_to(CASE)), 'digest': sha(file)}
                            for file in sorted(PREPARED.iterdir()) if file.is_file()],
          'limitations': ['Tutorial already-hydrogenated receptor and one ligand protomer were used.',
                          'Receptor protonation and alternative water/cofactor models were not enumerated.']}
write_json(CASE / 'evidence/preparation.json', record)

# Copy the installed distributions' license material and retain a version inventory.
inventory = []
for name in ['rdkit', 'meeko', 'gemmi', 'numpy', 'scipy', 'matplotlib']:
    dist = metadata.distribution(name)
    license_files = []
    for relative in dist.files or []:
        lower = str(relative).lower()
        if '.dist-info/' in lower and ('license' in lower or lower.endswith('copying')):
            original = Path(dist.locate_file(relative))
            if original.is_file():
                destination = CASE / 'licenses/installed' / name / Path(*Path(relative).parts[1:])
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, destination)
                license_files.append(str(destination.relative_to(CASE)))
    inventory.append({'name': name, 'version': dist.version,
                      'licenseExpression': dist.metadata.get('License-Expression'),
                      'licenseField': (dist.metadata.get('License') or '')[:200],
                      'licenseFiles': license_files})
write_json(CASE / 'evidence/installed-license-inventory.json', inventory)
print(json.dumps(record, indent=2), flush=True)
