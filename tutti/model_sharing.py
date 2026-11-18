"""Sharing of learned model profiles with the Tutti project."""

import os
import json
from . import config, owner

def is_sharing_enabled(cfg, kind):
    """
    kind: 'local' or 'online'
    Sharing is OFF by default for all new installations.
    Sharing is ON by default for the owner's installation.
    """
    sharing_cfg = cfg.get('sharing', {})
    enabled = sharing_cfg.get(kind)
    
    if enabled is not None:
        return bool(enabled)
    
    # If not set, check if owner
    return is_owner(cfg)

def is_owner(cfg):
    """
    Simple check to see if this is the owner's installation.
    """
    folder = config.state_dir(cfg)
    o = owner.owner_of(cfg, folder)
    return bool(o.get('accounts'))

def share_learnings(cfg, kind, data):
    """
    Share anonymized and aggregated model learnings with Tutti.
    """
    if not is_sharing_enabled(cfg, kind):
        return

    # TODO: Implement anonymization and sending of data.
    # The requirement says:
    # Aggregated per-model strengths/limits, success rates per task type, speed, tokens, watts per success, hardware class;
    # never code, prompts, repo names, emails, tokens or anything personal.
    # Shared data improves default routing priors for all installs.
    pass
