#!/usr/bin/env python3

import argparse
import csv
import glob
import mne
from mne.io.constants import FIFF
from mne.transforms import get_ras_to_neuromag_trans, Transform, apply_trans
try:
    from mne._fiff._digitization import DigPoint
except:
    from mne.io._digitization import DigPoint
import numpy as np
import os
import sys
import json
import subprocess
from datetime import datetime
from collections import namedtuple

ChannelLocation = namedtuple('ChannelLocation', ['channel', 'name', 'loc'])


def get_locations(locations_file, verbose=False):

    required_cols = ['Channel', 'Coil Name', 'sensor_x', 'sensor_y', 'sensor_z',
            'ex_i', 'ex_j', 'ex_k', 'ey_i', 'ey_j', 'ey_k', 'ez_i', 'ez_j', 'ez_k']

    location_dict = {}

    with open(locations_file, newline='') as csvfile:
        reader = csv.DictReader(csvfile)

        for row in reader:
            for col in required_cols:
                if col not in row:
                    print(f'ERROR: {locations_file} missing required column {col}')
                    return None

            if verbose:
                print(f'{row["Channel"]}: {row["Coil Name"]}')

            chan_loc = ChannelLocation(row['Channel'], row['Coil Name'],
                            np.array([row['sensor_x'], row['sensor_y'], row['sensor_z'],
                             row['ex_i'], row['ex_j'], row['ex_k'],
                             row['ey_i'], row['ey_j'], row['ey_k'],
                             row['ez_i'], row['ez_j'], row['ez_k']]))

            location_dict[chan_loc.channel] = chan_loc

    print(f'Sensor locations imported from {locations_file}.')

    return location_dict


def get_hpi_info(hpi_results, gof_thresh=0.98, verbose=False):

    required_cols = ['ch_name', 'ident', 'status', 'segment',
                     'gof',
                     'loc_x', 'loc_y', 'loc_z',
                     'moment_x', 'moment_y', 'moment_z']
    hpi_info = { 'scan': {} }

    with open(hpi_results, newline='') as csvfile:
        reader = csv.DictReader(csvfile)

        for row in reader:
            for col in required_cols:
                if col not in row:
                    print(f'ERROR: {hpi_results} missing required column {col}')
                    return None

            try:
                gof = float(row['gof'])
                loc = (float(row['loc_x']), float(row['loc_y']), float(row['loc_z']))
                moment = (float(row['moment_x']), float(row['moment_y']), float(row['moment_z']))
            except:
                continue

            if verbose:
                print(f'{row["ch_name"]} ({row["ident"]}): ({row["loc_x"]}, {row["loc_y"]}, {row["loc_z"]})')

            ch = row['ch_name']
            if ch not in hpi_info['scan']:
                hpi_info['scan'][ch] = {
                    'ident': row['ident'],
                    'status': row['status'],
                    'segments': []
                }

            hpi_info['scan'][ch]['segments'].append({
                'gof': gof,
                'loc': loc,
                'moment': moment
            })

    hpi_info['loc'] = {}

    for k,v in hpi_info['scan'].items():
        if v['status'] != 'PASS':
            print(f'WARN: ignoring failed coil {k} ({v["ident"]})')
            continue

        # compute aggregate location (average of all segments)
        r = []
        for i,s in enumerate(v['segments']):
            if s['gof'] < gof_thresh:
                print(f'WARN: ignoring {k} segment {i}, GOF {s["gof"]} < {gof_thresh}')
            else:
                r.append(s['loc'])
        if len(r) < 1:
            print(f'WARN: ignoring coil {k} ({v["ident"]}) with no valid locations')
            continue

        hpi_info['loc'][v['ident']] = np.mean(r, axis=0)

    if verbose:
        print('HPI Locations:')
        for k,v in hpi_info['loc'].items():
            print(f'    {k}: {v}')

    # must have nasion, lpa, rpa for transform
    hpi_loc = hpi_info['loc']
    if 'NASION' not in hpi_loc or 'LPA' not in hpi_loc or 'RPA' not in hpi_loc:
        print(f'ERROR: missing at least one required HPI channel (NASION,LPA,RPA)')
        return None

    dev_head_t = Transform("meg", "head", trans=None)
    dev_head_t['trans'] = get_ras_to_neuromag_trans(hpi_loc['NASION'], hpi_loc['LPA'], hpi_loc['RPA'])
    hpi_info['dev_head_t'] = dev_head_t

    if verbose:
        print(f'dev_head_t: {dev_head_t}')

    # make dig points for cardinal and hpi coils
    nasion = apply_trans(dev_head_t, hpi_loc['NASION'])
    lpa = apply_trans(dev_head_t, hpi_loc['LPA'])
    rpa = apply_trans(dev_head_t, hpi_loc['RPA'])
    hpi_pts = [nasion, lpa, rpa]
    for k, v in hpi_loc.items():
        if k not in ['NASION', 'LPA', 'RPA']:
            hpi_pts.append(apply_trans(dev_head_t, v))

    dig = []
    # add cardinal points
    dig.append({
        'r': lpa,
        'ident': FIFF.FIFFV_POINT_LPA,
        'kind': FIFF.FIFFV_POINT_CARDINAL,
        'coord_frame': FIFF.FIFFV_COORD_HEAD
    })
    dig.append({
        'r': nasion,
        'ident': FIFF.FIFFV_POINT_NASION,
        'kind': FIFF.FIFFV_POINT_CARDINAL,
        'coord_frame': FIFF.FIFFV_COORD_HEAD
    })
    dig.append({
        'r': rpa,
        'ident': FIFF.FIFFV_POINT_RPA,
        'kind': FIFF.FIFFV_POINT_CARDINAL,
        'coord_frame': FIFF.FIFFV_COORD_HEAD
    })

    # add HPI points
    for i, pt in enumerate(hpi_pts):
        dig.append({
            'r': pt,
            'ident': i + 1,
            'kind': FIFF.FIFFV_POINT_HPI,
            'coord_frame': FIFF.FIFFV_COORD_HEAD
        })

    hpi_info['dig'] = [ DigPoint(d) for d in dig ]

    if verbose:
        print(f'Digitization Points:')
        for d in hpi_info['dig']:
            print(f'  {d}')

    print(f'HPI results imported from {hpi_results}.')

    return hpi_info


def update_bz_polarity(fif_raw, verbose=False):
    desc = fif_get_description(fif_raw)
    if 'gui' in desc:
        return fif_raw
    else:
        print('Inverting Bz channel data')

        data = fif_raw.get_data()

        for i, ch_name in enumerate(fif_raw.info['ch_names']):
            if 'bz' in ch_name:
                if verbose:
                    print(f'  {ch_name}')
                data[i] = -data[i]

        return mne.io.RawArray(data, info=fif_raw.info)


def orig_ch_name(ch_name):
    tok = ch_name.split('_')
    if len(tok) != 2:
        return ''
    tok = tok[1].split('-')
    if len(tok) != 2:
        return ''
    return f'{tok[1]}_{tok[0]}'


def update_fif_locations(fif_raw, location_dict, verbose=False):
    for i, ch_name in enumerate(fif_raw.info['ch_names']):
        if 'bz' in ch_name:
            old_name = ""
            if not ch_name.endswith('bz'):
                old_name = ch_name
                ch_name = orig_ch_name(ch_name)

            if ch_name in location_dict:
                ch_location = location_dict[ch_name]
                if verbose:
                    print(f'{ch_name}: got location {ch_location}')

                ch_name_tok = ch_name.split('_')
                new_ch_name = f'{location_dict[ch_name].name}_{ch_name_tok[1]}-{ch_name_tok[0]}'
                fif_raw.info['chs'][i]['loc'] = location_dict[ch_name].loc
                fif_raw.info['chs'][i]['ch_name'] = new_ch_name
                fif_raw.info['ch_names'][i] = new_ch_name

                # Check to see if a corresponding by or bx channel exists
                for b in ['by', 'bx']:
                    if len(old_name) > 0:
                        target = old_name.replace('bz', b)
                    else:
                        target = ch_name.replace('bz', b)
                        
                    if target in fif_raw.info['ch_names']:
                        idx = fif_raw.info['ch_names'].index(target)
                        
                        pos = location_dict[ch_name].loc[0:3]
                        ex = location_dict[ch_name].loc[3:6]
                        ey = location_dict[ch_name].loc[6:9]
                        ez = location_dict[ch_name].loc[9:12]
                        if b == 'by':
                            tmp_pos = [*pos, *ez, *ex, *ey]
                        else:
                            tmp_pos = [*pos, *ey, *ez, *ex]
                        new_ch_name = f'{location_dict[ch_name].name}_{b}-{ch_name_tok[0]}'
                        fif_raw.info['chs'][idx]['loc'] = np.array(tmp_pos)
                        fif_raw.info['chs'][idx]['ch_name'] = new_ch_name
                        fif_raw.info['ch_names'][idx] = new_ch_name

            else:
                print(f'{ch_name}: no location found')

                fif_raw.info['chs'][i]['loc'] = np.array([0] * 12)
                fif_raw.info['chs'][i]['ch_name'] = ch_name
                fif_raw.info['ch_names'][i] = ch_name

    return fif_raw


def update_fif_hpi_info(fif_raw, hpi_info, args):

    fif_dig_pts = hpi_info['dig'][:]

    if args.extra_digpts:
        offset_m = args.digpt_offset / 1000
        extra_dig = []
        for ch in fif_raw.info['chs']:
            if not np.all(ch['loc'] == 0) and 'bz' in ch['ch_name']:
                # offset the loc points to account for distance from scalp to sensor
                ex = ch['loc'][3:6]
                ey = ch['loc'][6:9]
                ez = ch['loc'][9:12]
                R = np.vstack((ex, ey, ez))
                move = np.dot((0,0,offset_m),R)

                extra_dig.append(ch['loc'][0:3]-move)

        for i, pt in enumerate(extra_dig):
            fif_dig_pts.append(DigPoint({
                'r': apply_trans(hpi_info['dev_head_t'], pt),
                'ident': i + 1,
                'kind': FIFF.FIFFV_POINT_EXTRA,
                'coord_frame': FIFF.FIFFV_COORD_HEAD
            }))

    if args.verbose:
        print(f'Digitization Points:')
        for d in fif_dig_pts:
            print(f'  {d}')

    fif_raw.info.update(dev_head_t = hpi_info['dev_head_t'])
    with fif_raw.info._unlock():
        fif_raw.info['dig'] = fif_dig_pts

    return fif_raw


def fif_read(filename):
    return mne.io.read_raw_fif(filename, preload=True, verbose=None)


def fif_save(fif_raw, output_dir, basename):
    new_file_path = os.path.join(output_dir, basename)
    fif_raw.save(new_file_path, buffer_size_sec=1, overwrite=True)
    print(f'Saved {new_file_path}')


def fif_get_description(fif_raw):
    try:
        return json.loads(fif_raw.info['description'])
    except:
        return dict()


def fif_set_description(fif_raw, desc_dict):
    fif_raw.info['description'] = json.dumps(desc_dict)


def fif_has_been_updated(fif_raw):
    desc = fif_get_description(fif_raw)
    return 'updated' in desc


def fif_set_updated(fif_raw):
    now = datetime.now()
    now_str = now.strftime('%Y%m%d_%H%M%S')

    desc = fif_get_description(fif_raw)
    desc['updated'] = now_str
    fif_set_description(fif_raw, desc)


def fif_filter_channels(fif_raw, pattern_list):
    if pattern_list is None:
        return
    channel_names = fif_raw.info['ch_names']

    channels_to_keep = [ch for ch in channel_names if any(pattern in ch for pattern in pattern_list)]
    print(f"Keeping channels {channels_to_keep}")
    fif_raw.pick(channels_to_keep)


def fif_set_helmetscan(fif_raw, scan_datetime):
    desc = fif_get_description(fif_raw)
    desc['helmetscan'] = scan_datetime
    fif_set_description(fif_raw, desc)


def fif_set_hpi_result(fif_raw, hpi_result):
    desc = fif_get_description(fif_raw)
    desc['hpi_result'] = hpi_result
    fif_set_description(fif_raw, desc)


def main(args):

    helmetscan = args.helmetscan
    if helmetscan is None:
        helmetscan_files = glob.glob(args.inputdir + '/*_helmetscan_raw.fif')
        if len(helmetscan_files) > 1:
            print(f'ERROR: more than one helmet scan found, supply with --helmetscan')
            return 1
        if len(helmetscan_files) == 1:
            helmetscan = helmetscan_files[0]

    scan_datetime = None
    locations_file = None
    location_dict = None

    if helmetscan is None:
        if args.locations is not None:
            locations_file = args.locations
        else:
            print('No helmetscan, leaving locations unaltered')
    else:
        print(f'Processing {helmetscan}...')
        scan_datetime = os.path.basename(helmetscan).removesuffix('_helmetscan_raw.fif')

        cp = subprocess.run([args.localize, '-i', helmetscan])
        if cp.returncode != 0:
            print(f'ERROR: localize failed with {cp.returncode}')
            return cp.returncode

        locations_file = helmetscan.replace('_raw.fif', '_locations.csv')
        if not os.path.exists(locations_file):
            print(f'ERROR: no locations found, if this is expected remove {helmetscan}')
            return 1

    if locations_file is not None:
        location_dict = get_locations(locations_file, args.verbose)
        if location_dict is None:
            print(f'ERROR: failed to read helmet scan locations')
            return 1

        if args.verbose:
            for k,v in location_dict.items():
                print(f'{k}: {v}')

    hpi_result = args.hpi
    if hpi_result is None:
        hpi_files = glob.glob(args.inputdir + '/*_hpi_results.csv')
        if len(hpi_files) > 1:
            print(f'ERROR: more than one HPI result found, supply with --hpi')
            return 1
        if len(hpi_files) == 1:
            hpi_result = hpi_files[0]

    if hpi_result is None:
        print('No HPI results found')
        hpi_info = None
    else:
        print(f'Loading HPI results from {hpi_result}...')

        hpi_info = get_hpi_info(hpi_result, args.gof, args.verbose)
        if hpi_info is None:
            print(f'ERROR: failed to read hpi results')
            return 1

    if not os.path.exists(args.outputdir):
        os.mkdir(args.outputdir)
        print(f'Created output directory: {args.outputdir}')

    for file in glob.glob(args.inputdir + '/*.fif'):
        if 'helmetscan' in file:
            continue

        basename = os.path.basename(file)
        print(f'Processing {basename}...')

        fif_raw = fif_read(file)
        if fif_has_been_updated(fif_raw):
            print(f'File has already been updated, skipping')
            continue

        fif_raw = update_bz_polarity(fif_raw, args.verbose)

        if location_dict is not None:
            fif_raw = update_fif_locations(fif_raw, location_dict, args.verbose)
        if scan_datetime is not None:
            fif_set_helmetscan(fif_raw, scan_datetime)

        if hpi_info is not None:
            fif_raw = update_fif_hpi_info(fif_raw, hpi_info, args)
            fif_set_hpi_result(fif_raw, hpi_result)

        fif_set_updated(fif_raw)

        fif_filter_channels(fif_raw, args.pattern)

        fif_save(fif_raw, args.outputdir, basename)

    return 0


def parse_comma_separated_list(s):
    return s.split(',')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Update FIF contents to match the latest HEDscan recording format.')
    parser.add_argument('-l', '--localize', default='/usr/share/hedscan/helmet/localize.py', help='Path to localize.py.')
    parser.add_argument('-s', '--helmetscan', default=None, help='Helmet scan fif file (defaults to helmetscan in .).')
    parser.add_argument('--locations', default=None, help='Sensor locations CSV file (output from helmet scan or manually populated).')
    parser.add_argument('--hpi', default=None, help='HPI scan result file (defaults to hpi_result.csv in .).')
    parser.add_argument('-g', '--gof', type=float, default=0.98, help='HPI localization result Goodness Of Fit (GOF) threshold (default: %(default)s).')
    parser.add_argument('-e', '--extra_digpts', action='store_true', help='Add extra digitization points for each sensor at offset along Z axis.')
    parser.add_argument('-d', '--digpt_offset', type=float, default=6.0, help='Extra digitization point offset from sensor in mm (default: %(default)s).')
    parser.add_argument('-i', '--inputdir', default='.', help='Directory to look for FIF files (default .).')
    parser.add_argument('-o', '--outputdir', default='./out', help='Output directory (default ./out).')
    parser.add_argument('-p', '--pattern', type=parse_comma_separated_list, default=None, help='Comma separated list of patterns to filter channel names on.')
    parser.add_argument('-v', '--verbose', action='store_true', help='Print more information.')

    sys.exit(main(parser.parse_args()))
