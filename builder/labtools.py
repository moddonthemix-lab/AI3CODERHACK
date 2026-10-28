"""Authorized-lab EXECUTION — actually run recon/enumeration tools against a target the boss AUTHORIZED.

This is the only place in Builder that reaches the live network and runs external security tools, and it is
hard-gated so it can ONLY ever touch a target the boss put on record as theirs or a practice target (a CTF,
a VM they own). Every run must pass, in order:

1. ``guard_lab``      — authorized-lab mode must be ON.
2. allowlist          — the tool must be a recon/enumeration tool we recognise (no exploit frameworks, no
                        arbitrary shell).
3. argument safety    — no shell metacharacters, and no *second* host/IP/URL — so a tool can't be pointed at
                        anything other than the authorized target (which we always append ourselves).
4. ``guard_target``   — the target must match an authorization on record.
5. ``guard_action``   — the boss must approve this specific run (it reaches the network); logged either way.

Everything is written to the audit log. This is for CTFs and systems the boss owns — never anything else,
and never anything destructive. Reconnaissance and enumeration only; actual exploitation stays hands-on
(Builder guides, the boss runs it), so there is no weaponised capability bundled here.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time

from .safety import Safety

# Recon / enumeration tools only. Dual-use, but these are the standard first-phase tools for authorized
# testing — they look and list, they don't exploit. (No metasploit, no exploit kits, no reverse shells.)
TOOLS: dict[str, str] = {
    "nmap": "port & service scan",
    "gobuster": "brute-force web directories/files",
    "ffuf": "fuzz web paths/parameters",
    "dirb": "brute-force web directories",
    "feroxbuster": "brute-force web content",
    "nikto": "web server vulnerability scan",
    "whatweb": "fingerprint web technologies",
    "wpscan": "WordPress enumeration",
    "curl": "fetch a URL / inspect responses",
    "wget": "fetch a file/URL",
    "dig": "DNS lookup",
    "nslookup": "DNS lookup",
    "host": "DNS lookup",
    "whois": "domain/IP registration",
    "enum4linux": "SMB / Windows enumeration",
    "smbclient": "list/connect SMB shares",
    "smbmap": "enumerate SMB shares",
    "nbtscan": "NetBIOS scan",
    "snmpwalk": "SNMP enumeration",
    "nc": "banner grab / connect",
    "ncat": "banner grab / connect",
    "ping": "check the host is up",
    "traceroute": "trace the network path",
}

# Reject obvious command-injection / chaining attempts in args (we never use a shell, but be strict anyway).
_BAD_ARG = re.compile(r"[;&|`$><\n]|\$\(")
# A second host/IP/URL in the args would let a tool be aimed somewhere other than the authorized target.
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_URL_HOST = re.compile(r"https?://([^/\s:]+)", re.I)


def tool_available(tool: str) -> bool:
    return shutil.which(tool) is not None


class LabToolError(Exception):
    """A lab tool run was rejected before it could run (bad tool, unsafe args, not installed)."""


def _check_args(args: list[str], target: str) -> None:
    """Refuse shell metacharacters and any host/IP/URL that isn't the authorized target."""
    for a in args:
        s = str(a)
        if _BAD_ARG.search(s):
            raise LabToolError(f"refused unsafe argument {s!r} (no shell metacharacters allowed).")
        for ip in _IP.findall(s):
            if ip != target:
                raise LabToolError(f"argument {s!r} names a different host ({ip}); Builder only touches the "
                                   f"authorized target {target!r}.")
        for hostpart in _URL_HOST.findall(s):
            if target not in hostpart:
                raise LabToolError(f"argument {s!r} points at {hostpart!r}, not the authorized target "
                                   f"{target!r}.")


def run_tool(cfg, db, tool: str, target: str, args: list[str] | None = None, approve=None,
             log=print, timeout: int = 420) -> dict:
    """Run one recon/enum ``tool`` against an AUTHORIZED ``target``, through every safety gate.

    ``approve(kind, detail, target) -> bool`` is asked before the (network-reaching) run. Returns
    ``{tool, target, cmd, output, seconds}``. Raises on any gate failure (lab off, target not authorized,
    tool not allowlisted, unsafe args, not approved) — Builder never runs it unless all gates pass.
    """
    args = list(args or [])
    safety = Safety(db, cfg, approver=approve, log=log)
    safety.guard_lab()                              # 1. lab mode ON (else LabModeOff)
    if tool not in TOOLS:                           # 2. allowlist
        raise LabToolError(f"{tool!r} isn't a recon/enum tool Builder will run. Allowed: "
                           f"{', '.join(sorted(TOOLS))}.")
    _check_args(args, target)                       # 3. argument safety
    safety.guard_target(target)                     # 4. target must be authorized (else TargetNotAuthorized)
    if not tool_available(tool):
        raise LabToolError(f"'{tool}' isn't installed. Install it first (e.g. on a Mac: brew install {tool}), "
                           f"then try again.")
    # The authorized target is always the final argument, so the tool is aimed at it and nothing else.
    cmd = [tool, *args, target]
    safety.guard_action("offense", detail=f"{tool} {' '.join(args)}".strip(), target=target)  # 5. approval + log
    log(f"  running: {' '.join(cmd)}")
    start = time.monotonic()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        output = (p.stdout or "")
        if p.stderr:
            output += ("\n[stderr]\n" + p.stderr)
    except subprocess.TimeoutExpired:
        output = f"(no result — {tool} ran longer than {timeout}s and was stopped)"
    except OSError as e:
        output = f"(couldn't run {tool}: {e})"
    db.log_safety("offense", "allowed", detail=f"{tool} {' '.join(args)}".strip(), target=target,
                  reason="authorized-lab recon/enum")
    return {"tool": tool, "target": target, "cmd": cmd, "output": output,
            "seconds": round(time.monotonic() - start, 1)}
