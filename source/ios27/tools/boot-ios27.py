#!/usr/bin/env python3
"""iOS27 RAM boot. No flash, erase, partition or storage commands.

--check validates the matching local profile without contacting the phone.
Pass the phone serial with --serial or ANDROID_SERIAL; it is never hard-coded.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parent
SERIAL = None
OUT = None
receipts = []


def run(args, timeout=15, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    receipts.append({'args': args, 'exit': result.returncode,
                     'output': result.stdout + result.stderr})
    (OUT / 'receipts.json').write_text(json.dumps(receipts, indent=2) + '\n')
    if check:
        result.check_returncode()
    return result.stdout + result.stderr


def fb(*args, serial='xnuloader', timeout=15, check=True):
    return run(['fastboot', '-s', serial, *args], timeout, check)


def wait_product(serial, product):
    for _ in range(40):
        try:
            if product in fb('getvar', 'product', serial=serial, timeout=2, check=False):
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(.5)
    raise RuntimeError('Expected USB product did not respond: ' + product)


def upload_system(system_path):
    system = Path(system_path)
    total = system.stat().st_size
    chunk = OUT / 'system-chunk.tmp'
    try:
        with system.open('rb') as source:
            offset = 0
            while offset < total:
                amount = min(1024**3, total - offset)
                with chunk.open('wb') as dest:
                    left = amount
                    while left:
                        data = source.read(min(left, 8*1024**2))
                        if not data:
                            raise RuntimeError('Unexpected end of system image')
                        dest.write(data)
                        left -= len(data)
                fb('oem', 'ramdisk-chunk', format(offset, 'x'), format(total, 'x'))
                print(f'Uploading system to RAM: {offset/total:.0%} ({amount/1024**2:.0f} MiB)', flush=True)
                fb('stage', str(chunk), timeout=180)
                offset += amount
                (OUT / 'upload-progress.json').write_text(json.dumps({'written': offset, 'total': total})+'\n')
                print(f'System RAM upload: {offset/total:.0%}', flush=True)
    finally:
        chunk.unlink(missing_ok=True)


IMAGE_ROLES = ['uefi', 'payload', 'boot', 'system']


def load_profile():
    """Read BOOT-PREFLIGHT.json and report every missing or mismatched input.

    Returns (images, problems). A source snapshot ships no local build outputs,
    so this path must explain what to supply rather than raise on the first
    absent file.
    """
    manifest = BASE / 'BOOT-PREFLIGHT.json'
    if not manifest.exists():
        return None, [f'{manifest}: missing preflight profile']
    try:
        preflight = json.loads(manifest.read_text())
    except (OSError, ValueError) as error:
        return None, [f'{manifest}: unreadable preflight profile ({error})']
    images = preflight.get('images')
    if not isinstance(images, dict):
        return None, [f'{manifest}: no images section']
    problems = []
    for name in IMAGE_ROLES:
        entry = images.get(name)
        if not isinstance(entry, dict) or 'path' not in entry:
            problems.append(f'images.{name}: not described in the profile')
            continue
        path = Path(entry['path'])
        if not path.is_absolute():
            path = (BASE / path).resolve()
        if not path.exists():
            problems.append(f'{name}: missing {path}')
            continue
        expected = entry.get('bytes')
        if expected is not None and path.stat().st_size != expected:
            problems.append(
                f'{name}: {path} is {path.stat().st_size} bytes, profile says {expected}')
            continue
        if name != 'system':
            expected_hash = entry.get('sha256')
            if not expected_hash:
                problems.append(f'{name}: profile has no sha256 for {path}')
                continue
            with path.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != expected_hash:
                problems.append(f'{name}: {path} sha256 {actual} != {expected_hash}')
    return (None if problems else images), problems


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--serial', default=os.environ.get('ANDROID_SERIAL'),
                        help='adb/fastboot serial of the phone (defaults to ANDROID_SERIAL)')
    parser.add_argument('--check', action='store_true',
                        help='validate the local profile without contacting the phone')
    args = parser.parse_args()

    images, problems = load_profile()
    if problems:
        print('The local iOS27 boot profile is not ready:', file=sys.stderr)
        for problem in problems:
            print('  - ' + problem, file=sys.stderr)
        print('\nSupply the matching local build outputs listed in QUICKSTART.txt.',
              file=sys.stderr)
        raise SystemExit(1)
    if args.check:
        print(json.dumps({'profile_verified': True, 'device_contacted': False,
                          'phone_partition_writes': False}, indent=2))
        return
    if not args.serial:
        parser.error('--serial or ANDROID_SERIAL is required to boot')

    global OUT, SERIAL
    SERIAL = args.serial
    OUT = BASE / ('phone-' + time.strftime('%Y%m%d-%H%M%S'))
    OUT.mkdir()
    print('Waiting for Android startup', flush=True)
    for _ in range(90):
        state = run(['adb', '-s', SERIAL, 'get-state'], check=False)
        if state.strip() == 'device':
            assert run(['adb', '-s', SERIAL, 'shell', 'getprop', 'ro.board.platform']).strip() == 'sun'
            if run(['adb', '-s', SERIAL, 'shell', 'getprop', 'sys.boot_completed']).strip() == '1':
                break
        time.sleep(1)
    else:
        raise RuntimeError('Android did not finish startup')
    print('Starting iOS27 Project Boot UEFI with fastboot boot', flush=True)
    run(['adb', '-s', SERIAL, 'reboot', 'bootloader'])
    wait_product(SERIAL, 'sun')
    fb('boot', images['uefi']['path'], serial=SERIAL, timeout=30)
    wait_product('xnuloader', 'xnuloader')
    fb('stage', images['payload']['path'], timeout=40)
    upload_system(images['system']['path'])
    fb('oem', 'sm8750-touch-arm', timeout=30)
    print('Starting iOS27 kernel', flush=True)
    fb('oem', 'run', '0', timeout=30)
    for n in range(24):
        time.sleep(5)
        try:
            result = fb('getvar', 'result', timeout=5, check=False)
            print(result.strip(), flush=True)
            fb('oem', 'log', timeout=10, check=False)
            fb('get_staged', str(OUT / f'live-{n:02d}.log'), timeout=15, check=False)
            captured = OUT / f'live-{n:02d}.log'
            if captured.exists() and any(token in captured.read_text(errors='replace') for token in ['hakios: early exception', '\n== PANIC:']):
                print('Kernel startup fault captured; stopping diagnostic polling', flush=True)
                break
            if any(word in result.lower() for word in ['early-exception', 'returned', 'panic', 'hung']):
                break
        except subprocess.TimeoutExpired:
            print('USB query timed out; leaving RAM boot untouched', flush=True)
            break
    print('Diagnostic directory: ' + str(OUT), flush=True)


if __name__ == '__main__':
    main()
