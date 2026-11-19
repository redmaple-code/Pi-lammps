"""Read-only, independent checks of archived grinding input, log and trajectories."""
import argparse
import hashlib
import json
import re
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
AUDIT = Path(__file__).resolve().parent
MAIN = ROOT / 'work/jobs/lammps-f36c8580-d0a7-4e3b-9068-3d190415964d/work'


def frames(path):
    with path.open(encoding='utf-8') as stream:
        while stream.readline():
            step = int(stream.readline())
            assert stream.readline().strip() == 'ITEM: NUMBER OF ATOMS'
            count = int(stream.readline())
            boundary = stream.readline().strip()
            bounds = np.array([list(map(float, stream.readline().split())) for _ in range(3)])
            columns = stream.readline().split()[2:]
            data = np.array([list(map(float, stream.readline().split())) for _ in range(count)])
            data = data[np.argsort(data[:, columns.index('id')])]
            yield step, columns, data, bounds, boundary


def summarize_window(data):
    force = data[:, 4:7] * 1.602176634
    return {'firstStep': int(data[0, 0]), 'lastStep': int(data[-1, 0]), 'samples': len(data),
            'meanDragNn': float(-force[:, 0].mean()), 'meanNormalNn': float(force[:, 2].mean()),
            'meanTemperatureK': float(data[:, 7].mean()),
            'temperatureSlopeKPerPs': float(np.polyfit(data[:, 1], data[:, 7], 1)[0])}


def audit(directory, parameters):
    text = (directory / 'log.lammps').read_text(encoding='utf-8')
    lines = text.splitlines()
    script = (directory / 'in.grinding').read_text(encoding='utf-8')
    initial_step, columns0, initial, bounds, boundary = next(frames(directory / 'initial.lammpstrj'))
    final_step, columns, final, _, _ = next(frames(directory / 'final.lammpstrj'))
    assert np.array_equal(initial[:, 0], final[:, 0])
    xyz0 = initial[:, [columns0.index(axis) for axis in ['x', 'y', 'z']]]
    xyz = final[:, [columns.index(axis) for axis in ['x', 'y', 'z']]]
    delta = xyz - xyz0
    ly = bounds[1, 1] - bounds[1, 0]
    delta[:, 1] -= np.rint(delta[:, 1] / ly) * ly
    displacement = np.linalg.norm(delta, axis=1)
    fixed = xyz0[:, 2] <= parameters['latticeAngstrom']
    data = np.loadtxt(directory / 'grinding.csv', skiprows=1)
    approach = np.loadtxt(directory / 'approach.csv', skiprows=1)
    fby_step = {int(row[0]): row for row in data}
    radius, k = parameters['radiusAngstrom'], parameters['stiffnessEVPerAngstromCubed']
    comparisons = []
    for step, cols, atoms, _, _ in frames(directory / 'trajectory.lammpstrj'):
        if step not in fby_step:
            continue
        recorded = fby_step[step]
        position = atoms[:, [cols.index(axis) for axis in ['x', 'y', 'z']]]
        vector = position - [recorded[2], ly / 2, recorded[3]]
        vector[:, 1] -= np.rint(vector[:, 1] / ly) * ly
        distances = np.linalg.norm(vector, axis=1)
        contact = (distances < radius) & ~fixed
        penetration = radius - distances[contact]
        atom_forces = k * penetration[:, None] ** 2 * vector[contact] / distances[contact, None]
        calculated = -atom_forces.sum(axis=0)
        error = calculated - recorded[4:7]
        comparisons.append({'step': step, 'contactAtoms': int(contact.sum()),
                            'maxPenetrationAngstrom': float(penetration.max()) if contact.any() else 0,
                            'recalculatedForceEVPerA': calculated.tolist(), 'csvForceEVPerA': recorded[4:7].tolist(),
                            'errorNn': (error * 1.602176634).tolist(),
                            'toolPotentialEnergyEV': float(k / 3 * np.sum(penetration ** 3))})
    thermo = []
    for line in lines:
        fields = line.split()
        if len(fields) == 6:
            try:
                row = list(map(float, fields))
                if row[1] == len(initial):
                    thermo.append(row)
            except ValueError:
                pass
    thermo = np.array(list({row[0]: row for row in thermo}.values()))
    late_equil = thermo[(thermo[:, 0] >= 1000) & (thermo[:, 0] <= parameters['equilibrationSteps'])]
    half = len(data) // 2
    nominal_contact_radius = np.sqrt(2 * radius * parameters['depthAngstrom'] - parameters['depthAngstrom'] ** 2)
    signature = hashlib.sha256((directory / 'in.grinding').read_bytes()).hexdigest()
    return {
        'directory': str(directory), 'inputSha256': signature,
        'solverErrors': [{'line': i + 1, 'text': line} for i, line in enumerate(lines) if re.match(r'^ERROR\b', line)],
        'solverWarnings': [{'line': i + 1, 'text': line} for i, line in enumerate(lines) if re.match(r'^WARNING\b', line)],
        'dangerousBuilds': [int(v) for v in re.findall(r'Dangerous builds = (\d+)', text)],
        'completionMarker': 'REDMAPLE_GRINDING_COMPLETE' in text,
        'minimization': {'stoppingCriterion': re.search(r'Stopping criterion = (.+)', text).group(1),
                         'finalForceMaxEVPerA': float(re.search(r'Force max component initial, final =\s+\S+\s+(\S+)', text).group(1)),
                         'requestedForceToleranceEVPerA': 1e-6},
        'identity': {'initialAtoms': len(initial), 'finalAtoms': len(final),
                     'idsMatch': bool(np.array_equal(initial[:, 0], final[:, 0])),
                     'typesMatch': bool(np.array_equal(initial[:, 1], final[:, 1])),
                     'compositionCountsByType': {str(int(i)): int(np.sum(initial[:, 1] == i)) for i in np.unique(initial[:, 1])},
                     'fixedAtoms': int(fixed.sum()), 'fixedMaxDisplacementAngstrom': float(displacement[fixed].max())},
        'kinematics': {'initialStep': initial_step, 'finalStep': final_step,
                       'scratchTimePs': float(data[-1, 1] - data[0, 1]), 'travelAngstrom': float(data[-1, 2] - data[0, 2]),
                       'speedMPerS': float((data[-1, 2] - data[0, 2]) / (data[-1, 1] - data[0, 1]) * 100),
                       'approachSpeedMPerS': float(abs(approach[-1, 3] - approach[0, 3]) / (approach[-1, 1] - approach[0, 1]) * 100),
                       'trajectoryFrameCount': sum(1 for _ in frames(directory / 'trajectory.lammpstrj'))},
        'statistics': {'all': summarize_window(data), 'firstHalf': summarize_window(data[:half]),
                       'secondHalf': summarize_window(data[half:]),
                       'quarters': [summarize_window(part) for part in np.array_split(data, 4)],
                       'dragWorkEV': float(np.trapz(-data[:, 4], data[:, 2])),
                       'apparentForceRatioSecondHalf': float((-data[half:, 4].mean()) / data[half:, 6].mean()),
                       'temperatureMinK': float(data[:, 7].min()), 'temperatureMaxK': float(data[:, 7].max())},
        'lateEquilibration': {'samples': len(late_equil), 'temperatureMeanK': float(late_equil[:, 2].mean()),
                             'temperatureMinK': float(late_equil[:, 2].min()), 'temperatureMaxK': float(late_equil[:, 2].max())},
        'geometry': {'substrateNm': (np.array(parameters['cells']) * parameters['latticeAngstrom'] / 10).tolist(),
                     'sphereRadiusNm': radius / 10, 'nominalDepthNm': parameters['depthAngstrom'] / 10,
                     'nominalContactRadiusNm': float(nominal_contact_radius / 10),
                     'widthToContactDiameter': float(ly / (2 * nominal_contact_radius)),
                     'nominalMeanPressureGPa': float(data[half:, 6].mean() * 1.602176634 / (np.pi * (nominal_contact_radius / 10) ** 2)),
                     'pressureCaveat': 'Geometrical reference area, actual atomic contact area and hardness are unvalidated'},
        'displacement': {'maxAngstrom': float(displacement.max()), 'countOverOneAngstrom': int(np.sum(displacement > 1)),
                         'quantilesAngstrom': np.quantile(displacement[~fixed], [.5, .9, .99]).tolist()},
        'forceReconstruction': {'frames': comparisons,
                                'maxComponentAbsoluteErrorNn': float(np.max(np.abs([item['errorNn'] for item in comparisons])))},
        'energyAccounting': {'toolEnergyAddedToThermo': bool(re.search(r'^fix_modify\s+tool\s+energy\s+yes', script, re.M)),
                            'thermostatEnergyTallied': bool(re.search(r'langevin.*tally\s+yes', script)),
                            'velocityInDump': all(axis in columns for axis in ['vx', 'vy', 'vz'])}
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, default=MAIN)
    parser.add_argument('--parameters', type=Path, default=ROOT / 'domains/lammps/grinding.parameters.json')
    parser.add_argument('--output', type=Path, default=AUDIT / 'main-case-audit.json')
    args = parser.parse_args()
    result = audit(args.directory, json.loads(args.parameters.read_text(encoding='utf-8')))
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    summary = {key: value for key, value in result.items() if key not in ['forceReconstruction']}
    summary['forceReconstruction'] = {'frames': len(result['forceReconstruction']['frames']), 'maxComponentAbsoluteErrorNn': result['forceReconstruction']['maxComponentAbsoluteErrorNn']}
    print(json.dumps(summary, indent=2))
