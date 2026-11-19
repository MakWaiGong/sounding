#!/usr/bin/env python3
"""Check the poster's illustrative bathymetry (relative units, not real measurements).

Run: python assets/readme/verify_sounding_bathymetry.py
Optional artwork-only dependencies: numpy and scipy.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET

try:
    import numpy as np
    from scipy.interpolate import PchipInterpolator, RegularGridInterpolator
except ImportError as exc:
    raise SystemExit('The optional poster verifier needs numpy and scipy.') from exc

NS = {'s': 'http://www.w3.org/2000/svg'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def file_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('svg', nargs='?', type=Path,
                        default=Path(__file__).with_name('sounding-poster-source.svg'))
    args = parser.parse_args()
    root = ET.parse(args.svg).getroot()
    model_tag = root.find('s:metadata', NS)
    require(model_tag is not None and model_tag.get('id') == 'illustrative-bathymetry-model',
            'Missing illustrative heightfield metadata')
    model = json.loads(model_tag.text)
    require(model['type'] == 'illustrative_bathymetry' and 'not actual metres' in model['units'],
            'Depth units must be explicitly illustrative')
    for name, key in (('sounding-poster-background.png', 'background_sha256'),
                      ('sounding-bathymetry-mask.png', 'mask_sha256'),
                      ('sounding-poster.png', 'poster_png_sha256')):
        require(file_sha256(args.svg.with_name(name)) == model[key], f'Artwork hash mismatch: {name}')

    anchors = np.asarray(model['anchors'], dtype=float)
    xs = np.linspace(model['x_start'], model['x_end'], model['x_samples'])
    vs = np.linspace(model['near_min'], model['near_max'], model['near_samples'])
    X, V = np.meshgrid(xs, vs)
    ridge = PchipInterpolator(anchors[:, 0], anchors[:, 1])(X)
    h0 = (740 - ridge) / 175
    height = (h0 * (1 - .60 * V)
              + (.75 * np.exp(-((X - 1400) / 170) ** 2)
                 + .55 * np.exp(-((X - 1110) / 150) ** 2)
                 + .32 * np.exp(-((X - 1530) / 130) ** 2 - ((V - .55) / .08) ** 2))
              * np.sin(np.pi * V))
    depth = model['sea_level_height'] - height
    screen_y = 740 + 320 * V - 175 * height
    interpolate = RegularGridInterpolator((vs, xs), depth, bounds_error=False, fill_value=None)
    require(float(depth.min()) > 0, 'Terrain crosses the illustrative water level')

    group = next(g for g in root.findall('s:g', NS) if g.get('id') == 'bathymetric-contours')
    paths = group.findall('s:path', NS)
    require(len(paths) == model['contour_segments'], 'Unexpected contour segment count')
    levels = set()
    max_depth_error = 0.0
    sample_count = 0
    foreground_conflicts = 0
    for path in paths:
        level = float(path.get('data-relative-depth'))
        levels.add(round(level, 2))
        values = list(map(float, re.findall(r'-?\d+(?:\.\d+)?', path.get('d'))))
        require(len(values) >= 8 and len(values) % 2 == 0, 'Malformed contour path')
        coords = np.asarray(values).reshape(-1, 2)
        samples = np.concatenate((coords, coords[:-1] * .75 + coords[1:] * .25,
                                  (coords[:-1] + coords[1:]) / 2,
                                  coords[:-1] * .25 + coords[1:] * .75))
        v = (samples[:, 1] - 740 + 175 * (3 - level)) / 320
        require(v.min() >= vs[0] - .00001 and v.max() <= vs[-1] + .00001,
                'Contour leaves the model domain')
        x = np.clip(samples[:, 0], xs[0], xs[-1])
        v = np.clip(v, vs[0], vs[-1])
        max_depth_error = max(max_depth_error, float(np.max(np.abs(
            interpolate(np.column_stack((v, x))) - level))))
        sample_count += len(samples)
        for offset in range(0, len(samples), 300):
            xx = x[offset:offset + 300]
            vv = v[offset:offset + 300]
            yy = samples[offset:offset + 300, 1]
            col = np.clip(np.searchsorted(xs, xx, side='right') - 1, 0, len(xs) - 2)
            weight = (xx - xs[col]) / (xs[col + 1] - xs[col])
            curve = screen_y[:, col] * (1 - weight)[None, :] + screen_y[:, col + 1] * weight[None, :]
            dy = curve[1:, :] - curve[:-1, :]
            intersects = ((yy[None, :] >= np.minimum(curve[:-1, :], curve[1:, :]) - 1e-9)
                          & (yy[None, :] <= np.maximum(curve[:-1, :], curve[1:, :]) + 1e-9)
                          & (np.abs(dy) > 1e-12))
            t = np.divide(yy[None, :] - curve[:-1, :], dy,
                          out=np.zeros_like(dy), where=np.abs(dy) > 1e-12)
            roots = vs[:-1, None] + t * (vs[1] - vs[0])
            foreground_conflicts += int(np.any(
                intersects & (t >= -1e-9) & (t <= 1 + 1e-9)
                & (roots > vv[None, :] + .0001), axis=0).sum())
    levels = np.asarray(sorted(levels))
    require(len(levels) == model['depth_levels'], 'Unexpected depth level count')
    require(np.max(np.abs(np.diff(levels) - model['depth_interval'])) < 1e-8,
            'Depth levels are not equally spaced')
    require(max_depth_error < .0001, f'Contour depth error: {max_depth_error}')
    require(foreground_conflicts == 0, f'Foreground occlusion conflicts: {foreground_conflicts}')

    gradients = [g for g in root.findall('.//s:linearGradient', NS)
                 if g.get('id', '').startswith('bathymetry-fade-')]
    require(len(gradients) == len(levels), 'Missing distance fade gradients')
    for gradient in gradients:
        alphas = np.asarray([float(s.get('stop-opacity')) for s in gradient.findall('s:stop', NS)])
        require(len(alphas) >= 2 and alphas[0] == 0 and np.min(np.diff(alphas)) >= -1e-8
                and alphas[-1] <= .175, 'Invalid distance fade')

    point_group = next(g for g in root.findall('s:g', NS) if g.get('id') == 'seabed-soundings')
    soundings = point_group.findall('s:g', NS)
    require(len(soundings) == len(model['soundings']) == 29, 'Unexpected sounding count')
    max_point_error = 0.0
    for element, point in zip(soundings, model['soundings']):
        line = element.find('s:path', NS)
        match = re.fullmatch(r'M (\d+) (\d+) V (\d+)', line.get('d'))
        require(match is not None, 'Malformed sounding line')
        x, start, end = map(int, match.groups())
        circles = element.findall('s:circle', NS)
        require(len(circles) == 2 and float(circles[1].get('cx')) == x
                and float(circles[1].get('cy')) == end, 'Sounding and glow do not match')
        require(x == point['x'] and start == point['start_y'] and end == point['end_y'],
                'Sounding position changed')
        v = point['near_fraction']
        d = float(interpolate([[v, x]])[0])
        projected_y = 740 + 320 * v - 175 * (3 - d)
        max_point_error = max(max_point_error, abs(projected_y - end),
                              abs(d - point['relative_depth']))
    require(max_point_error < .002, f'Sounding projection error: {max_point_error}')
    require([t.text for t in root.findall('s:text', NS)] ==
            ['观潮', '/', 'SOUNDING', '深潜一篇，看清一片', '多次测深，勾勒浪潮下的洋底'],
            'Poster wording changed')
    print(f'PASS: {len(paths)} contour segments, {len(levels)} equally spaced relative depths')
    print(f'PASS: {sample_count} projected samples, max depth error {max_depth_error:.8g}')
    print('PASS: no foreground intersection beyond the 0.0001 near-coordinate tolerance')
    print(f'PASS: 29 modeled soundings, max projection error {max_point_error:.8g} px')
    print('PASS: background, distance mask, poster PNG and wording match')
    print('NOTE: the heightfield is illustrative, not measured bathymetry.')


if __name__ == '__main__':
    main()
