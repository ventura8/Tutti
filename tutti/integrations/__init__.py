from . import discord, linear, slack


def notify(cfg, message):
    _call(cfg, 'send', message)


def needs_attention(cfg, items):
    _call(cfg, 'needs_attention', items)

def ask_owner_answer(cfg, question, answer):
    _call(cfg, 'ask_owner_answer', question, answer)


def handle_command(cfg, message):
    if message.startswith('!'):
        # Simple command handler: !backlog list, !needs
        if message == '!backlog list':
            from .. import backlog
            return backlog.listing(backlog.load(cfg))
        elif message == '!needs':
            from .. import needs
            return needs.lines_for_agent(cfg, 'admin', 'owner')
    return 'Unknown command'


def _call(cfg, method, *args):
    integrations = cfg.get('integrations', {})
    if integrations.get('slack', {}).get('enabled'):
        if hasattr(slack, method):
            getattr(slack, method)(cfg, *args)
        elif method == 'send':
            slack.send(cfg, *args)

    if integrations.get('discord', {}).get('enabled'):
        if hasattr(discord, method):
            getattr(discord, method)(cfg, *args)
        elif method == 'send':
            discord.send(cfg, *args)

    if integrations.get('linear', {}).get('enabled'):
        if hasattr(linear, method):
            getattr(linear, method)(cfg, *args)
        elif method == 'send':
            linear.send(cfg, *args)
