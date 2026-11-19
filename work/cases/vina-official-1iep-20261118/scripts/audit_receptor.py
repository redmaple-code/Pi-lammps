"""Check the geometry-checking receptor against the actual prepared solver receptor."""

from collections import Counter
import json
from pathlib import Path
from rdkit import Chem

CASE = Path(__file__).resolve().parents[1]
solver = CASE / 'inputs/official/1iep_receptor.pdbqt'
conditional = CASE / 'reference/receptorH.pdb'


def heavy_records(file, pdbqt):
    records = []
    for line in file.read_text().splitlines():
        if not line.startswith(('ATOM  ', 'HETATM')):
            continue
        kind = line.split()[-1] if pdbqt else line[76:78].strip()
        if kind in ['H', 'HD', 'HS', 'D']:
            continue
        key = (line[21:22].strip(), line[22:27].strip(), line[17:20].strip(), line[12:16].strip(),
            float(line[30:38]), float(line[38:46]), float(line[46:54]))
        records.append(key)
    return Counter(records)


a, b = heavy_records(solver, True), heavy_records(conditional, False)
loaded = Chem.MolFromPDBFile(str(conditional), removeHs=False, sanitize=False, proximityBonding=False)
result = {'solverReceptor': solver.relative_to(CASE).as_posix(),
    'geometryCheckReceptor': conditional.relative_to(CASE).as_posix(),
    'solverHeavyAtoms': sum(a.values()), 'geometryCheckHeavyAtoms': sum(b.values()),
    'heavyAtomIdentityAndCoordinatesExactMatch': a == b,
    'missingFromGeometryInput': [list(key) for key in a - b],
    'extraInGeometryInput': [list(key) for key in b - a],
    'rdkitParsingSucceeded': loaded is not None,
    'rdkitHeavyAtomCount': loaded.GetNumHeavyAtoms() if loaded is not None else None,
    'note': 'Residue/atom identity plus fixed-frame coordinates checked at stored precision; hydrogen preparations can differ.'}
(CASE / 'evidence/receptor-analysis-input-audit.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
print(json.dumps(result), flush=True)
if not result['heavyAtomIdentityAndCoordinatesExactMatch'] or loaded is None:
    raise RuntimeError('Geometry-analysis receptor differs from actual solver receptor; investigate before interpretation.')
