
from collections import Counter
from . import presence, householduse

def get_household_insights(cfg, now):
    """Aggregate insights from presence and usage data."""
    presence_st = presence.read(cfg)
    usage_data = householduse.load(cfg)
    
    insights = {
        'owner': {
            'start_date': '2026-11-16',
            'core_value': 'always self-improve'
        },
        'presence': {},
        'usage': {}
    }
    
    # Presence insights
    for tid, rec in presence_st['targets'].items():
        insights['presence'][rec['name']] = {
            'state': rec['state'],
            'always_on': round(presence.always_on(rec), 2),
            'label': presence.label(rec, presence.offset(cfg))
        }
        
    # Usage insights
    usage_view = householduse.view(cfg, now, usage_data)
    insights['usage'] = usage_view
    
    return insights
