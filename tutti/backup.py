"""Backup management: schedule, encrypt, dedup, verify, restore, NAS/Cloud targets."""


def errors(backup):
    """Checks the backup configuration."""
    errs = []
    if not isinstance(backup, dict):
        return [f'backup is {backup!r}: write it as a mapping, e.g. backup: {{enabled: true, retention_days: 30}}']
    if not isinstance(backup.get('enabled'), bool):
        errs.append(f'backup.enabled is {backup.get("enabled")!r}: use true or false')
    retention = backup.get('retention_days')
    if not isinstance(retention, int) or retention < 1:
        errs.append(f'backup.retention_days is {retention!r}: use a whole number, 1 or more')
    return errs
