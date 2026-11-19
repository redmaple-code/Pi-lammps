"""Paired loaded/no-abrasive comparison without assigning plastic strain labels."""
from pathlib import Path
import hashlib
import json
import numpy as np
from audit_case import frames

ROOT = Path(__file__).resolve().parent
MAIN = ROOT.parents[1] / 'work/jobs/lammps-f36c8580-d0a7-4e3b-9068-3d190415964d/work'
CONTROL = ROOT / 'no-tool-control'


def extract(directory):
    step, cols, initial, bounds, _ = next(frames(directory / 'initial.lammpstrj'))
    final_step, final_cols, final, _, _ = next(frames(directory / 'final.lammpstrj'))
    assert np.array_equal(initial[:, 0], final[:, 0])
    initial_positions = initial[:, [cols.index(c) for c in ['x', 'y', 'z']]]
    final_positions = final[:, [final_cols.index(c) for c in ['x', 'y', 'z']]]
    delta = final_positions - initial_positions
    ly = bounds[1, 1] - bounds[1, 0]
    delta[:, 1] -= np.rint(delta[:, 1] / ly) * ly
    displacement = np.linalg.norm(delta, axis=1)
    thermo = np.loadtxt(directory / 'grinding.csv', skiprows=1)
    cna = final[:, final_cols.index('c_cna')]
    return initial, displacement, {'initialStep': step, 'finalStep': final_step, 'atoms': len(final),
        'displacementMaxAngstrom': float(displacement.max()), 'displacedAtomsAboveOneAngstrom': int(np.sum(displacement > 1)),
        'temperatureMeanK': float(thermo[:, 7].mean()), 'temperatureFinalK': float(thermo[-1, 7]),
        'temperatureMinK': float(thermo[:, 7].min()), 'temperatureMaxK': float(thermo[:, 7].max()),
        'cnaCounts': {str(int(i)): int(np.sum(cna == i)) for i in np.unique(cna)}}


initial, loaded_displacement, loaded = extract(MAIN)
baseline, control_displacement, control = extract(CONTROL)
assert np.array_equal(initial[:, 0], baseline[:, 0])
result = {'initialStateMatches': bool(np.array_equal(initial, baseline)),
          'initialStateComparisonFields': ['id', 'type', 'x', 'y', 'z'],
          'loaded': loaded, 'noToolControl': control,
          'loadedLargeDisplacementIdsAlsoOverThresholdInControl': int(np.sum((loaded_displacement > 1) & (control_displacement > 1))),
          'interpretationLimits': ['One matched seed, individual trajectories become chaotic after perturbation',
                                   'Displacement includes indentation, sliding, thermal vibration and surface relaxation',
                                   'No unloading or retraction was performed, permanent deformation and removal are unverified']}
(ROOT / 'control-comparison.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
integrity = []
for directory in [MAIN, CONTROL]:
    for name in ['in.grinding', 'alloy.data', 'FeNiCrCoCu-heafixed.setfl']:
        path = directory / name
        integrity.append({'path': str(path), 'byteSha256': hashlib.sha256(path.read_bytes()).hexdigest()})
(ROOT / 'control-input-integrity.json').write_text(json.dumps({
    'files': integrity,
    'receiptHashNote': 'run_control.py receipt sourceInputSha256/inputSha256 hash UTF-8 text with normalized LF endings before Windows text-mode output. byteSha256 values here describe actual files.'
}, indent=2), encoding='utf-8')
print(json.dumps(result, indent=2))
