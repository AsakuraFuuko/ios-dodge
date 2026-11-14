#!/usr/bin/env python3
"""Boot the verified Peach Wi-Fi/GLES iOS build entirely in RAM."""
import argparse
import fcntl
import hashlib
import json
import runpy
from pathlib import Path
import struct
import subprocess
import sys
import time

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--output', type=Path)
p.add_argument('--serial', help='Connected Android phone serial')
p.add_argument('--check', action='store_true', help='Validate files without contacting the phone')
p.add_argument('--menu', action='store_true', help='Upload and wait for the phone Boot iOS button')
p.add_argument('--recover-idle-loader', action='store_true')
a = p.parse_args()
root = Path(__file__).resolve().parents[3]
exp = Path(__file__).resolve().parent
build = root / 'experimental/wifi/darwin-station-v14'
loader = exp / 'dodge-ios-uefi.img'
payload = build / 'payload.bin'
system = root / 'experimental/sileo/v1/system-sileo.apfs'
manifest_path = loader.with_suffix('.json')
firmware = root / 'diagnostics/20261029-wifi/native-reference/wifi-qdss-v5.private.bin'
def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        while chunk := f.read(8 << 20): h.update(chunk)
    return h.hexdigest()
# A source snapshot ships no generated images, firmware or private profiles, so
# report every absent input at once instead of failing on the first assert.
missing = [str(p) for p in (build / 'BUILD.json', loader, payload, system,
                            manifest_path, firmware) if not p.exists()]
if missing:
    print('The local iOS16 boot profile is not ready. Missing inputs:', file=sys.stderr)
    for path in missing:
        print('  - ' + path, file=sys.stderr)
    print('\nSupply these local build outputs; see QUICKSTART.txt and',
          'source/wifi-peach/WORKING.md.', file=sys.stderr)
    raise SystemExit(1)
try:
    meta = json.loads((build / 'BUILD.json').read_text())
except (OSError, ValueError) as error:
    print(f'{build / "BUILD.json"}: unreadable build metadata ({error})', file=sys.stderr)
    raise SystemExit(1)
problems = []
def check(condition, message):
    """Record a failed expectation instead of aborting the whole preflight."""
    if not condition: problems.append(message)
check(meta.get('experimental') and meta.get('wifi_station_test'), 'BUILD.json does not mark this an experimental Wi-Fi station test')
check(not meta.get('production_ios_changed') and not meta.get('phone_partition_writes'), 'BUILD.json must keep production_ios_changed and phone_partition_writes false')
check(digest(loader) == meta.get('loader_sha256') == '2ee6c2a0def66dcb9ebe7a33268d3a1372202f1501de7219083fb647ab760955', f'{loader}: sha256 does not match the selected UEFI build')
check(digest(payload) == meta.get('payload_sha256'), f'{payload}: sha256 does not match BUILD.json')
check(digest(system) == meta.get('system_sha256') == 'ece8098938ebbc265c8c782bf705cf2f74747deea002ec3a5548cb4307909ed3', f'{system}: sha256 does not match the selected Sileo system image')
check(all(word in meta.get('cmdline','').split() for word in ['cpus=7','bootcpu=1','ufs=0','ufs_write=0','var_store=0']), 'BUILD.json cmdline is missing a RAM-only or seven-core argument')
check(meta.get('changed_entries') == ['kernel','ramdisk','cmdline'] and meta.get('modified_boot_args')=={'wifi':'1'}, 'BUILD.json changed_entries/modified_boot_args do not describe the Wi-Fi-only payload delta')
check(meta.get('modified_root_files')==['/LaunchDaemons/com.apple.wifid.plist','/LaunchDaemons/com.redwood.wifi.plist'] and meta.get('stock_wifid_restored'), 'BUILD.json does not record the restored stock wifid job')
unpack = runpy.run_path(str(root / 'tools/make-op13-test-payload.py'))['unpack']
try:
    entries = unpack(payload.read_bytes())
except Exception as error:
    print(f'{payload}: not a readable XNUP payload ({error})', file=sys.stderr)
    raise SystemExit(1)
check(hashlib.sha256(entries['kernel']).hexdigest()==meta.get('kernel_sha256'), 'payload.bin kernel does not match BUILD.json')
check(entries['cmdline'].rstrip(b'\0').decode()==meta.get('cmdline') and len(entries['cmdline'])<=416, 'payload.bin cmdline does not match BUILD.json or exceeds 416 bytes')
args=dict(word.split('=',1) for word in meta.get('cmdline','').split())
required=dict(cpus='7',bootcpu='1',op13_usb='1',op13_batt='4',slider='1',slider_power='1',
              gl='1',metal='0',adreno8='23',adreno8_keep='1',adreno8_secure_preflight='4',
              ufs='0',ufs_write='0',var_store='0',cpufreq='0',loader_return_after='0',
              touch_steps='4',buttons_steps='5',wifi='1')
check(all(args.get(k)==v for k,v in required.items()), 'payload.bin cmdline is missing a required boot argument: ' + ', '.join(f'{k}={v}' for k,v in required.items() if args.get(k)!=v))
check('op13_audio' not in args and 'sm8750_test_reset_seconds' not in args, 'payload.bin cmdline enables paused audio diagnostics or a test reset')
check(digest(firmware)=='9f22e9beab0357f652f55e1f2c6ec5c8ed7869c42a03f2d1876ea86d88751ea9', f'{firmware}: sha256 does not match the verified QDSS image')
manifest=json.loads(manifest_path.read_text())
check(manifest.get('image_sha256')==meta.get('loader_sha256'), f'{manifest_path}: image_sha256 does not match BUILD.json')
check(manifest.get('loader_executables_unchanged') and manifest.get('other_inner_ffs_unchanged'), f'{manifest_path}: an unrelated FFS or loader executable was modified')
check(manifest.get('changed_inner_ffs')==['7bb28b99-61bb-11d5-9a5d-0090273fc14d'], f'{manifest_path}: unexpected inner FFS changed')
base=(root/manifest['source']).resolve();check(base.is_relative_to(root.resolve()), f'{manifest_path}: source image escapes the checkout')
if not base.exists():
    print(f'The local iOS16 boot profile is not ready. Missing input:\n  - {base}', file=sys.stderr)
    raise SystemExit(1)
packaging=json.loads(base.with_suffix('.json').read_text())
check(digest(base)==manifest.get('source_sha256')==packaging.get('image_sha256'), f'{base}: base image sha256 does not match its manifests')
check(packaging.get('source_sha256')=='f26410ca510dcb7352f5c0b814ca435aaefe05bdc3ed1697ef5091b79d588cb8', f'{base}: base image is not the documented v3.1 revision')
check(all(packaging.get(k) for k in ['other_ffs_unchanged','bootshim_unchanged','dtb_unchanged','ramdisk_and_signature_bytes_unchanged']), f'{base}: packaging manifest reports an unexpected modification')
check(all(manifest.get(k) for k in ['bootshim_unchanged','dtb_unchanged','ramdisk_and_signature_bytes_unchanged']), f'{manifest_path}: bootshim, DTB or ramdisk bytes changed')
if problems:
    print('The local iOS16 boot profile does not match the selected build:', file=sys.stderr)
    for problem in problems:
        print('  - ' + problem, file=sys.stderr)
    raise SystemExit(1)
if a.check:
    print(json.dumps(dict(package_verified=True,system_verified=True,firmware_verified=True,
                         loader_verified=True,device_contacted=False,phone_partition_writes=False),indent=2))
    raise SystemExit(0)
if not a.serial or not a.output:p.error('--serial and --output are required to boot')
boot_lock=(root/'build/gles-ram-boot.lock').open('a')
fcntl.flock(boot_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
a.output.mkdir(parents=True, exist_ok=False)
a.output.chmod(0o700)
receipts = []
def command(args, timeout=30, check=True):
    argv = list(map(str, args))
    print('Running:', ' '.join(argv), flush=True)
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        receipts.append(dict(args=argv, exit=r.returncode, output=r.stdout+r.stderr))
        print(r.stdout+r.stderr, flush=True)
        if check: r.check_returncode()
        return r
    except subprocess.TimeoutExpired:
        receipts.append(dict(args=argv, timeout=timeout))
        raise
    finally:
        (a.output/'receipts.json').write_text(json.dumps(receipts, indent=2)+'\n')
def fb(*args, timeout=30, check=True):
    return command(['fastboot','-s','xnuloader',*args],timeout,check)
def capture(name):
    fb('oem','log'); fb('get_staged',a.output/name)

boot_args = ['/opt/homebrew/bin/python3', exp/'run-bundle.py', '--serial',a.serial,
             '--loader',loader,'--output',a.output/'boot','--action','link','--stage-wifi-firmware']
if a.recover_idle_loader: boot_args.append('--recover-idle-loader')
command(boot_args, 240)
fb('oem','op13-wifi-rails'); capture('rails.log')
command(['/opt/homebrew/bin/python3',root/'tools/run-op13-wifi-dma-steps.py',
         '--output',a.output/'dma','--steps','9','8','6','7'],180)
command(['/opt/homebrew/bin/python3',exp/'run-qmi.py','--output',a.output/'qmi'],240)
fb('oem','op13-wifi-transfer-prepare')
lease = a.output/'mhi-transfer.private.bin'
fb('get_staged',lease); lease.chmod(0o600)
raw = lease.read_bytes()
assert len(raw)==936 and struct.unpack_from('<Q',raw)[0]==0x4f50313357494649
assert struct.unpack_from('<I',raw,20)[0]==2
assert struct.unpack_from('<Q',raw,40)[0]==0x8000000
blocked = fb('oem','op13-wifi-mhi-export',check=False)
assert blocked.returncode and 'quiesced' in blocked.stdout+blocked.stderr
capture('transfer.log')
fb('oem','op13-dsi-brightness','ahb'); capture('brightness.log')
fb('stage',payload,timeout=60); fb('oem','ramdisk')
fb('stage',system,timeout=180)
fb('oem','op13-slider-probe'); capture('slider.log')
if a.menu:
    fb('oem','menu-ready')
    (a.output/'RESULT.json').write_text(json.dumps(dict(awaiting_phone_boot_button=True,
        wifi_transport_prepared=True,phone_partition_writes=False),indent=2)+'\n')
    print('iOS ready in RAM. Tap Boot iOS, or use Volume and Power.',flush=True)
    raise SystemExit(0)
fb('oem','sm8750-touch-arm'); fb('oem','run','0')
for delay in [5,15,45,60,60]:
    time.sleep(delay)
    fb('getvar','result')
    capture(f'ios-{delay}-{len(receipts)}.log')
logs = '\n'.join(path.read_text(errors='replace') for path in a.output.glob('ios-*.log'))
result = dict(wifi_working=False, ios_launch_sent=True, ownership_transferred=True,
              darwin_mhi_adopted='reserved Peach MHI state adopted' in logs,
              darwin_target_cap_verified='target-cap=1' in logs,
              full_driver_start_succeeded='full Peach ath12k start result=0' in logs,
              network_joined='THE NETWORK IS JOINED: frames can pass' in logs,
              ipv4_observed='waitaddr en0: an address' in logs,
              https_probe_succeeded='net_test: fetch https://example.com/: status 200' in logs,
              panic_detected='panic(cpu' in logs,
              phone_partition_writes=False, production_ios_changed=False)
result['wifi_working']=all(result[k] for k in ['network_joined','ipv4_observed','https_probe_succeeded']) and not result['panic_detected']
(a.output/'RESULT.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2),flush=True)
