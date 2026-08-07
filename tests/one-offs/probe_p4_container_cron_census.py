#!/usr/bin/env python3
"""P4: cron census across minimal Linux base images (#69, D2's empirical leg).

Question being measured, not argued: do the minimal images that VPSes and
containers start from ship cron AT ALL? This decides how common the AC-7
refusal path is on the feature's target machines, and it is the recorded
reopen-trigger for the no-systemd-backend-in-v1 decision (D2):

  - crond present nearly everywhere  -> daemon pre-flight relaxes toward a
                                        cheap warning; refusal stays rare
  - crond absent on most bases       -> refusal is the COMMON path on the
                                        exact target machines; a real
                                        systemd backend moves to v1.1

Per image (binary PRESENCE, not running state -- containers run no init;
runtime detection is AC-7's process-table pre-flight, a different check):

  crontab   -- the user-facing spool tool csb's installer drives
  daemon    -- any of cron / crond / fcron on PATH
  busybox   -- crond applet (the Alpine way; AC-7's detection list
               includes it for exactly this reason)

Usage:  python tests/one-offs/probe_p4_container_cron_census.py
Read-only apart from docker image pulls. Kept per the keep-one-offs rule.

Result 2026-08-06 (docker server 29.6.2, this box):

    debian:stable-slim     crontab NONE   daemon NONE            busybox NONE
    ubuntu:24.04           crontab NONE   daemon NONE            busybox NONE
    alpine:latest          /usr/bin/crontab  crond (busybox)     applet
    rockylinux:9-minimal   crontab NONE   daemon NONE            busybox NONE

  3/4 have NO cron capability. Interpretation bounds (be honest about
  what this measures): these are CONTAINER/minimal bases -- the floor a
  from-scratch or containerized environment starts at -- NOT full VPS
  cloud images (Ubuntu Server / Debian cloud images commonly include
  cron; the user's own production VPS has run crontab for years).
  Consequences recorded in the Rnd4 assessment, Addendum 3: the AC-7
  daemon pre-flight + refusal path is validated as load-bearing, the
  --print-systemd recipe is elevated (on a minimal-base REAL machine
  systemd is the init and present while cron is not), and the v1.1
  systemd backend moves from reopen-on-demand to planned-next.
"""

from __future__ import annotations

import subprocess
import sys

IMAGES = [
    "debian:stable-slim",
    "ubuntu:24.04",
    "alpine:latest",
    "rockylinux:9-minimal",   # RHEL-family datapoint (refusal text cites dnf/cronie)
]

PROBE_SH = (
    'echo "crontab=$(command -v crontab || echo NONE)"; '
    'D=NONE; for d in cron crond fcron; do '
    'command -v "$d" >/dev/null 2>&1 && D="$d=$(command -v $d)"; done; '
    'echo "daemon=$D"; '
    'B=NONE; command -v busybox >/dev/null 2>&1 && '
    'busybox --list 2>/dev/null | grep -qx crond && B=applet; '
    'echo "busybox-crond=$B"'
)


def probe(image: str) -> dict[str, str]:
    r = subprocess.run(
        ["docker", "run", "--rm", image, "sh", "-c", PROBE_SH],
        capture_output=True, text=True, timeout=600,
    )
    if r.returncode != 0:
        return {"error": (r.stderr.strip().splitlines() or ["unknown"])[-1]}
    out = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


def main() -> int:
    try:
        v = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                           capture_output=True, text=True, timeout=60)
        if v.returncode != 0:
            print("docker daemon not reachable:", v.stderr.strip())
            return 1
        print(f"docker server {v.stdout.strip()}\n")
    except FileNotFoundError:
        print("docker CLI not found")
        return 1

    print(f"{'image':<22} {'crontab':<28} {'daemon':<24} busybox-crond")
    absent = 0
    for image in IMAGES:
        r = probe(image)
        if "error" in r:
            print(f"{image:<22} ERROR: {r['error']}")
            continue
        has_any = (r.get("crontab", "NONE") != "NONE"
                   or r.get("daemon", "NONE") != "NONE"
                   or r.get("busybox-crond", "NONE") != "NONE")
        absent += 0 if has_any else 1
        print(f"{image:<22} {r.get('crontab','?'):<28} "
              f"{r.get('daemon','?'):<24} {r.get('busybox-crond','?')}")

    print(f"\nverdict: {absent}/{len(IMAGES)} images have NO cron capability at all")
    print("(presence census only -- daemons never run in init-less containers;")
    print(" runtime liveness is AC-7's process-table pre-flight)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
