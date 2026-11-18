"""OpenAI usage reader: reads from a configured reading file."""
import json
import os
from datetime import datetime, timezone

FIELDS = ('percentUsed',)


def fetch(path):
    """(answer, problem): the trimmed answer."""
    if not path:
        return None, 'no reading_file configured for OpenAI'
    try:
        with open(os.path.expanduser(path), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return None, f'OpenAI usage file read failed ({type(e).__name__}): check the file'
    
    # OpenAI might not have "planUsage". If it does not, try to read directly from the top level.
    plan = data.get('planUsage') if isinstance(data, dict) else None
    if not isinstance(plan, dict):
        # Fallback: maybe the file content IS the usage object
        plan = data if isinstance(data, dict) else {}
        
    if not any(k in plan for k in FIELDS):
        return None, 'OpenAI\'s usage answer has no planUsage or valid fields'
    return {'planUsage': {k: plan.get(k) for k in FIELDS}, 'billingCycleEnd': data.get('billingCycleEnd')}, None


def parse(data, field):
    """(used %, reset datetime or None) from fetch's answer."""
    v = ((data or {}).get('planUsage') or {}).get(field)
    if not isinstance(v, (int, float)):
        # Fallback for the top-level reading
        v = (data or {}).get(field)
        if not isinstance(v, (int, float)):
            raise ValueError(f'no "{field}" in OpenAI usage answer')
    end = (data or {}).get('billingCycleEnd')
    reset = datetime.fromtimestamp(int(end) / 1000, timezone.utc) if str(end or '').isdigit() else None
    return round(float(v), 1), reset
