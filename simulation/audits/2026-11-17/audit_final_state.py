"""Verify actual run masses and final-state kinetic temperatures from write_data."""
from pathlib import Path
import json
import re
import numpy as np

ROOT = Path(__file__).resolve().parent
DIRECTORY = ROOT.parents[1] / 'work/jobs/lammps-f36c8580-d0a7-4e3b-9068-3d190415964d/work'


def data_file(path):
    sections = {}
    section = None
    for line in path.read_text(encoding='utf-8').splitlines():
        if re.match(r'^(Masses|Atoms|Velocities)\b', line):
            section = line.split()[0]
            sections[section] = []
        elif section and line.strip():
            sections[section].append([float(v) for v in line.split('#', 1)[0].split()])
    sections = {key: np.array(value) for key, value in sections.items()}
    for key in ['Atoms', 'Velocities']:
        if key in sections:
            sections[key] = sections[key][np.argsort(sections[key][:, 0])]
    return sections


initial = data_file(DIRECTORY / 'alloy.data')
final = data_file(DIRECTORY / 'final.data')
assert np.array_equal(initial['Atoms'][:, 0], final['Atoms'][:, 0])
assert np.array_equal(final['Atoms'][:, 0], final['Velocities'][:, 0])
types = final['Atoms'][:, 1].astype(int)
input_masses = initial['Masses'][:, 1]
actual_masses = final['Masses'][:, 1]
elements = ['Fe', 'Ni', 'Cr', 'Co', 'Cu']
potential_lines = (DIRECTORY / 'FeNiCrCoCu-heafixed.setfl').read_text(encoding='utf-8').splitlines()
potential_masses = [float(line.split()[1]) for line in potential_lines if re.match(r'^\s+\d+\s+[\d.]+\s+[\d.]+\s+fcc\s*$', line)]
velocities = final['Velocities'][:, 1:4]
mass = actual_masses[types - 1]
positions = final['Atoms'][:, 2:5]
initial_positions = initial['Atoms'][:, 2:5]
fixed = initial_positions[:, 2] <= 3.56
bath = (initial_positions[:, 2] > 3.56001) & (initial_positions[:, 2] <= 10.68)
mobile = ~fixed
tip = np.array([57.8, 21.36, 43.265])
xy = positions[:, :2] - tip[:2]
xy[:, 1] -= np.rint(xy[:, 1] / 42.72) * 42.72
near_contact = mobile & (np.linalg.norm(xy, axis=1) <= 12) & (positions[:, 2] >= 34.265 - 3 - 3.56)


def kinetic(mask):
    m = mass[mask]
    v = velocities[mask]
    center = np.sum(m[:, None] * v, axis=0) / m.sum()
    # Constants used by LAMMPS units metal in the archived executable generation.
    mvv2e, boltz = 1.0364269e-4, 8.617343e-5
    raw_ke = 0.5 * mvv2e * np.sum(m[:, None] * v ** 2)
    thermal_ke = 0.5 * mvv2e * np.sum(m[:, None] * (v - center) ** 2)
    dof = 3 * len(m) - 3
    return {'atoms': len(m), 'centerVelocityAngstromPerPs': center.tolist(),
            'rawKineticTemperatureK': float(2 * raw_ke / (dof * boltz)),
            'centerVelocityRemovedTemperatureK': float(2 * thermal_ke / (dof * boltz))}


result = {'massChecks': [{'element': element, 'declaredInputMassAmu': float(declared), 'potentialMassAmu': expected,
                          'actualRunMassAmu': float(actual), 'changePercent': float((actual / declared - 1) * 100)}
                         for element, declared, expected, actual in zip(elements, input_masses, potential_masses, actual_masses)],
          'potentialMassMatchesFinalData': bool(np.allclose(actual_masses, potential_masses)),
          'declaredMassMatchesFinalData': bool(np.array_equal(input_masses, actual_masses)),
          'kineticAtStep8000': {'mobile': kinetic(mobile), 'thermostat': kinetic(bath),
                               'dynamic': kinetic(mobile & ~bath), 'nearContactTopLayer': kinetic(near_contact)},
          'nearContactSelection': {'xyRadiusAngstrom': 12, 'minimumZAngstrom': 27.705,
                                   'tipCenterAngstrom': tip.tolist()},
          'temperatureCaveat': 'One instantaneous final-state sample with center velocity subtraction, no local-temperature time history or confidence interval'}
(ROOT / 'final-state-audit.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
print(json.dumps(result, indent=2))
