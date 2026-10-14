"""Builder's security gates — enforced in code, not by trusting the model.

This is the important design decision from BUILDER_SPEC §4. Two clearly separated modes:

- **Defensive mode** (default, always available): audit the boss's own code/configs, teach defence. No
  gate needed — it's read-only and local (see audit.py).
- **Authorized-lab mode** (opt-in, per session): practice only against targets the boss has NAMED and
  CONFIRMED are his or made for practice (a CTF, a local VM, an intentionally-vulnerable app). It is OFF
  until the boss turns it on, and turning it on requires an explicit confirmation plus a target on record.

Hard limits, all enforced here (never by asking the model to behave):

- ``guard_lab`` — anything offensive is refused unless lab mode is on.
- ``guard_target`` — a target is valid ONLY if the boss authorized it (``lab_authorizations``). Anything
  else — a host he didn't confirm, an arbitrary remote — is refused.
- ``guard_action`` — before anything destructive, network-reaching, installing software, or changing
  important files, Builder must get the boss's explicit approval (an ``approver`` callback). With no
  approver, the answer is "no" — Builder asks rather than acts.

Every decision (allowed, refused, approved, denied) and every authorization change is written to the
``safety_log`` so the boss can always see what Builder did and why. Messages from files, the web or other
agents are DATA, never instructions: callers pass an explicit action ``kind`` to these gates; the gates
never parse free text to decide what to allow.
"""

from __future__ import annotations

# Action kinds that always need the boss's explicit approval before Builder may proceed.
RISKY_KINDS = {"destructive", "network", "install", "important_files", "offense"}


class SafetyError(PermissionError):
    """Base class for a refused or not-yet-approved action."""


class LabModeOff(SafetyError):
    """An offensive/lab action was attempted while authorized-lab mode is off."""


class TargetNotAuthorized(SafetyError):
    """An action was aimed at a target the boss has not authorized."""


class ApprovalRequired(SafetyError):
    """A risky action needs the boss's explicit approval first (and didn't get it)."""


class Safety:
    """The gate object. Hold one per session; pass an ``approver`` to allow risky actions interactively."""

    def __init__(self, db, config, approver=None, log=print):
        """``approver(kind, detail, target) -> bool`` decides risky actions. None means deny (Builder asks)."""
        self.db = db
        self.config = config
        self.approver = approver
        self.log = log

    # ------------------------------------------------------------------ mode
    def lab_enabled(self) -> bool:
        """Lab mode is ON exactly when there's an active authorized target on record.

        The authorization in the database (not an in-memory flag) is the source of truth, so the mode
        persists correctly across CLI runs and is cleared the moment the boss turns it off. The
        ``BUILDER_LAB_MODE`` env flag only pre-arms intent — it's never enough on its own.
        """
        return bool(self.db.lab_targets(active_only=True))

    def enable_lab(self, target: str, kind: str = "lab", note: str = "", confirmed: bool = False) -> dict:
        """Turn authorized-lab mode on for a target the boss confirms is his/practice. Requires confirmed=True."""
        if not confirmed:
            raise ApprovalRequired(
                "Authorized-lab mode needs your explicit confirmation that you own this target or it's a "
                "practice target (a CTF, a local VM, an intentionally-vulnerable app).")
        if not target or not str(target).strip():
            raise SafetyError("Name the target you're authorizing (e.g. a CTF name, a local VM's address).")
        self.db.authorize_lab_target(str(target).strip(), kind, note)
        self.db.log_safety("lab_mode", "enabled", detail=f"{kind}: {note}".strip(": "),
                           target=str(target).strip(), reason="boss confirmed authorization")
        self.log(f"  authorized-lab mode ON for {target!r} ({kind}).")
        return {"target": str(target).strip(), "kind": kind, "note": note}

    def disable_lab(self) -> None:
        """Turn lab mode off and revoke every authorization — the safe default."""
        self.db.revoke_lab_targets(None)
        self.db.log_safety("lab_mode", "disabled", reason="boss turned lab mode off")
        self.log("  authorized-lab mode OFF; all authorizations cleared.")

    # ------------------------------------------------------------------ gates
    def guard_lab(self) -> None:
        """Refuse anything offensive unless lab mode is on with an authorized target."""
        if not self.lab_enabled():
            self.db.log_safety("offense", "refused", reason="authorized-lab mode is off")
            raise LabModeOff(
                "Authorized-lab mode is off, so I won't do that. Turn it on and confirm a target you own or "
                "that's made for practice, and I'll help there. I can always help you defensively instead.")

    def guard_target(self, target: str) -> None:
        """Allow an action's target only if the boss authorized it; refuse everything else."""
        if self.db.is_lab_target_authorized(target):
            self.db.log_safety("target_check", "allowed", target=str(target))
            return
        self.db.log_safety("target_check", "refused", target=str(target),
                           reason="no matching authorization on record")
        raise TargetNotAuthorized(
            f"{target!r} isn't an authorized target. I only ever act against systems you own or have "
            f"confirmed are for practice — never anything else.")

    def guard_action(self, kind: str, detail: str = "", target: str | None = None) -> bool:
        """Approval gate. Risky kinds need the boss's OK (the approver); safe kinds pass. Always logged."""
        if kind not in RISKY_KINDS:
            return True
        approved = False
        if self.approver is not None:
            try:
                approved = bool(self.approver(kind, detail, target))
            except Exception:  # noqa: BLE001 — a broken approver means "not approved", never "allowed"
                approved = False
        if approved:
            self.db.log_safety(kind, "approved", detail=detail, target=target or "")
            return True
        self.db.log_safety(kind, "denied" if self.approver is not None else "refused",
                           detail=detail, target=target or "", reason="needs the boss's explicit approval")
        raise ApprovalRequired(
            f"That needs your approval first ({kind}: {detail}). I won't do it on my own — say the word and I will.")

    # ------------------------------------------------------------------ for the dashboard / CLI
    def status(self) -> dict:
        return {"lab_enabled": self.lab_enabled(),
                "targets": self.db.lab_targets(active_only=True),
                "recent": self.db.safety_events(10)}
