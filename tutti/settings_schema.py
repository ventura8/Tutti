"""The Settings tab's schema: every config key with a plain label, a one-line help, its control, its unit and its default.

A path is dotted; `*` stands for an entry's own name (machines.*.slots). The default comes from config.DEFAULTS (or the
entry defaults) unless the field gives its own. A key in config.DEFAULTS that has no line here still shows (an automatic
label from its name), and a key in the owner's file that is in neither shows under "Other" as raw YAML: a new key never
needs this file to appear. tests/test_settings.py checks that every key of tutti.config.example.yaml has a line here.

Sections go in the order of the tab. A section may later be scoped to one user or to the household (`scope`): the tab
draws whatever sections the server sends."""
from . import config, scouts

MISSING = object()
DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
# Settings that change only when the dashboard starts again (the page then offers a Restart button)
RESTART = {'host', 'dashboard.port', 'dashboard.bind', 'dashboard.origins', 'auth.client_id', 'auth.session_hours',
           'project.state', 'household.port', 'household.bind', 'telemetry.endpoint', 'telemetry.environment'}
# Named entries: each one is drawn as its own group of fields
ENTRIES = ('machines', 'devices', 'workers', 'accounts', 'projects', 'scouts', 'autopilot.repos', 'scouts.*.watches',
           'review.repos')
# About people, not this system: shown on the Manage tab (users, roles, invites, the household, each user's schedule and
# main agent; tutti/manage.py), never in Settings
MANAGED = ('user', 'household', 'pacing', 'main.agent', 'main.preferred')
SECTIONS = [
    ('accounts', 'Accounts and readings', 'The quotas Tutti paces and where their readings come from.', ['accounts', 'usage']),
    ('energy', 'Energy', 'Local models\' estimated energy cost: your electricity price and currency (docs/ENERGY.md).', ['energy']),
    ('workers', 'Workers', 'The agents and local models Tutti can brief, and the order `tutti run auto` tries them.',
     ['workers', 'routing.workers', 'brief']),
    ('main', 'Main agent', 'Who conducts `tutti conduct` and who takes over when its quota runs out.', ['main']),
    ('machines', 'Machines', 'The computers that run heavy commands, their slot queues and how commands run there.',
     ['machines', 'failover', 'routing.order', 'runner', 'sync', 'files', 'guard', 'home_assistant']),
    ('devices', 'Devices', 'Android devices for end-to-end runs, and when a run may start on them.', ['devices', 'night']),
    ('autopilot', 'Autopilot', 'Backlog tasks sent to accounts that are behind their pace; retries climb the ladder.',
     ['autopilot', 'autopilot.repos', 'depbots']),
    ('fleet', 'Local models', 'Every machine\'s Ollama as a worker; they take tasks while the online accounts are spared '
     '(docs/FLEET.md).', ['fleet', 'decisional', 'work_queue']),
    ('dispatch', 'Keep busy', 'Idle devices and machines pull the projects\' pending test shards (docs/DISPATCH.md).', ['dispatch']),
    ('time', 'Time zone', 'What local time means for your hours, quiet hours and tariffs; auto is this computer\'s.', ['time']),
    ('presence', 'Presence', 'Devices and machines that come and go: away, asleep, moving work and Wake-on-LAN (docs/PRESENCE.md).',
     ['presence']),
    ('autocompact', 'Autocompact', 'When a worker\'s context is compacted.', ['autocompact']),
    ('review', 'Review', '`tutti pr review`: who reviews a pull request and who steps in when no one answers.', ['review', 'review.repos']),
    ('scouts', 'Scouts', 'Helpers that are not workers (Grok Bot): questions, watches and reviews.', ['scouts']),
    ('telemetry', 'Telemetry and self-improvement', 'Crash reports and usage statistics, and the fixes they turn into.',
     ['telemetry', 'self_improve', 'ai_assist', 'welcome']),
    ('projects', 'Projects and repos', 'Your projects, their checks, shipping, delegation, tests and translation.',
     ['projects', 'ship', 'delegate', 'test', 'translate']),
    ('optins', 'Opt-ins', 'Everything that is off until you turn it on: what costs money per use, and Android runs.', []),
    ('update', 'Updates and backups', 'Tutti itself (`tutti self-update`), the worker CLIs on every machine (`tutti update`) and '
     'the daily backup of the config and the state folder (`tutti backup`).', ['self_update', 'update', 'backup']),
    ('browser', 'Work browser', 'The household\'s one Chrome for every browser task; sites signed in there are shared by '
     'everyone who uses it (docs/BROWSER.md).', ['browser']),
    ('dashboard', 'Dashboard', 'This page: its port, how often it refreshes, and which processes it shows as jobs.',
     ['dashboard', 'project']),
    ('chat', 'Chat', 'The Chat tab: what viewers may ask it to do, and how much of a conversation a model sees (docs/CHAT.md).',
     ['chat']),
    ('auth', 'Sign-in', 'Sign-in with Google and how long a session lasts (docs/AUTH.md; `tutti auth setup` sets the clients).',
     ['auth']),
    ('host', 'Where Tutti runs', 'This computer as Tutti\'s host, and where it moved (docs/MOVE.md).', ['host']),
    ('vpn', 'Private network (optional)', 'Already have a VPN to your home? Nothing to do. Which one `tutti vpn setup` sets up '
     '(docs/NETWORK.md).', ['vpn']),
]


def field(label, help_text, control=None, **opts):
    return {'label': label, 'help': help_text, 'control': control, **opts}


LOOK_EVERY = 'Look every'
GB = {'control': 'number', 'unit': 'GB', 'min': 0}
MIN = {'control': 'number', 'unit': 'minutes', 'min': 0}
SEC = {'control': 'number', 'unit': 'seconds', 'min': 1}
HRS = {'control': 'number', 'unit': 'hours', 'min': 0}
PCT = {'control': 'number', 'unit': '%', 'min': 0, 'max': 100}
CMD = {'control': 'list', 'item': 'command word'}
YAML = {'control': 'yaml'}
POLICY = {
    'policy.min_battery': field('Lowest battery', 'A run starts only above this charge.', **PCT),
    'policy.require_charging': field('Only while charging', 'A run starts only while the device charges.', 'switch', default=False),
    'policy.max_battery_c': field('Hottest battery', 'No new run at or above this battery temperature.', 'number', unit='°C',
                                  min=0, max=80),
    'policy.wifi_only': field('Wi-Fi only', 'A run starts only while the device is on Wi-Fi.', 'switch', default=False),
    'policy.screen': field('Screen', 'What a run does with the screen.', 'select', choices=['wake', 'off', 'any']),
}

ROLES_OVERRIDE, ROLES_ADDED, ROLES_REMOVED = 'Roles (override)', 'Roles added', 'Roles removed'   # tutti/roles.py

FIELDS = {
    # accounts and readings
    'accounts.*.window': field('Window', 'How often the quota resets: 5h, 7d, 2w or monthly.', 'text'),
    'accounts.*.resets': field('A known reset', 'One reset: UTC, or with an offset or zone (Europe/Bucharest); the window repeats it.',
                               'text'),
    'accounts.*.resets_estimated': field('Reset is a guess', 'The reset day is estimated until a reading names the real one.', 'switch',
                                     default=False),
    'accounts.*.lead_hours': field('Finish early by', 'Pace the quota to run out this long before the reset.', **HRS),
    'accounts.*.share': field('Share to use', 'The part of the quota Tutti may use (1 = all of it).', 'number', min=0, max=1, step=0.05),
    'accounts.*.panic_at': field('Panic at', 'At or over this use before the target time, nothing new goes to it.', **PCT, default=95),
    'accounts.*.source': field('Where readings come from', 'Shown beside the account so you know where to look.', 'text'),
    'accounts.*.worker': field('Worker', 'The worker that spends this account.', 'select', choices_from='workers'),
    'accounts.*.usage_url': field('Usage page', 'The page the reading helper reads in one click.', 'text'),
    'accounts.*.helper_label': field('Helper label', 'The meter\'s name on the usage page, for the reading helper.', 'text'),
    'accounts.*.background': field('Spent outside Tutti too', 'The planner estimates use it does not see (automatic reviews).',
                               'switch', default=False),
    'accounts.*.subscription': field('Plan', 'Your plan (a label: pro, max5, ...); `tutti setup` picks a default.', 'text'),
    'accounts.*.plan': field('Plan command', 'A command that prints the plan, the field to read and how often.', **YAML),
    'accounts.*.match': field('Which jobs spend it', 'Regexes on a job\'s command and model that count against this account.', **YAML),
    'accounts.*.reading_file': field('Reading file', 'A file your own tool writes {"used": 42} to.', 'text'),
    'accounts.*.stale_hours': field('Reading goes stale after', 'A reading by hand older than this gets a reminder.', **HRS),
    'accounts.*.args': field('Extra worker flags', 'Added when `tutti run auto` or conduct picks this account.', **CMD),
    'accounts.*.reader': field('Automatic reader', 'A command that prints usage, or format: cursor-api, codex-app-server '
                               'or browser-usage for a scoped usage page.', **YAML),
    'accounts.*.tiers': field('Models by pace', 'Behind its limit: the strong tier\'s flags and the large tasks; ahead: the cheap '
                              'tier\'s and the small ones ({strong: {args: [--model, x], sizes: [L, M]}, cheap: ...}).', **YAML),
    'accounts.*.limits': field('Bounded by', 'Longer quotas that bound this one (a weekly over a 5-hour window).', 'list',
                           choices_from='accounts'),
    'accounts.*.window_points': field('Points per window', 'What one whole window costs of its weekly limit; measured when empty.',
                                  'number', min=0),
    'accounts.*.reserve': field('Reserve', 'Points the planner leaves for work outside Tutti.', 'number', min=0, default=0),
    'accounts.*.share_spare': field('Share spare room', 'Room it would lose at its reset may do the household\'s shared tasks.',
                                'switch', default=False),
    'accounts.*.target_pct': field('Target', 'The % of the quota to have used by its spend-by time (reset minus lead).', **PCT),
    'accounts.*.task_pct': field('One task costs', 'The share of the quota one task spends, for pacing.', **PCT),
    'accounts.*.costs_money': field('Costs money per use', 'Kept off until you turn it on.', 'switch', default=False),
    'accounts.*.enabled': field('On', 'Off takes it out of pacing, routing and the dashboard.', 'switch'),
    'accounts.*.opt_in': field('Opt-in', 'Off until you turn it on, without costing money.', 'switch', default=False),
    'usage.log': field('Usage log', 'The file every reading is appended to.', 'text', default='~/.local/state/tutti/usage.jsonl'),
    # workers
    'workers.*.kind': field('Kind', 'cli: another vendor\'s agent; ollama: a local model server.', 'select', choices=['cli', 'ollama']),
    'workers.*.command': field('Command', 'The command line; {prompt} is the brief, {log} a log file per run.', **CMD),
    'workers.*.chat_account_selector': field('Chat account selector', 'The CLI flag an admin asserts selects an account or model.', 'text'),
    'workers.*.chat_output_format': field('Chat output format', 'Use text or cursor-stream-json for Cursor progress and final result.',
                                         'text'),
    'workers.*.denied': field('Denied marker', 'Text in its log that means a headless permission was refused.', 'text'),
    'workers.*.costs_money': field('Costs money per use', 'Kept off until you turn it on.', 'switch', default=False),
    'workers.*.enabled': field('On', 'Off takes it out of routing, conduct and review.', 'switch'),
    'workers.*.opt_in': field('Opt-in', 'Off until you turn it on, without costing money.', 'switch', default=False),
    'workers.*.host': field('Server', 'The model server, host:port.', 'text'),
    'workers.*.model': field('Model', 'The model\'s name on the server.', 'text'),
    'workers.*.threads': field('Threads', 'CPU threads the model may use.', 'number', min=1),
    'workers.*.num_ctx': field('Context', 'The model\'s context window.', 'number', unit='tokens', min=512),
    'workers.*.context_tokens': field('Context window', 'Used by autocompact when the worker does not say.', 'number', unit='tokens',
                                      min=1000),
    'workers.*.machine': field('Machine', 'Where this worker runs (Codex on the laptop): yield, pause, heat and quiet hours '
                               'are honoured; Move work knows the host.', 'select', choices_from='machines'),
    'workers.*.tunnel': field('Tunnel', 'An ssh tunnel to the model server: {ssh, remote}.', **YAML),
    'routing.workers': field('Worker order', '`tutti run auto` prefers these first (order only; every account that names a '
                            'worker still joins, so Codex is not left out when the list is only cursor and agy).', 'list',
                            choices_from='workers'),
    'brief.rules': field('Rules in every brief', 'Added to every brief (the standing ones are always added).', 'list', item='rule'),
    # main agent
    'main.target_pct': field('Target', 'The % of the main agent\'s quota to use by its spend-by time; the owner\'s reserve still holds.',
                             **PCT),
    'main.agent': field('Main agent', 'The worker that conducts the next goal, or auto: Tutti keeps preferred while it '
                        'has room, else the first worker in main.fallback with room.', 'select', choices_from='workers',
                        suggest=['auto']),
    'main.preferred': field('Preferred main', 'The owner\'s lasting main agent; Tutti returns here after its limit resets. '
                            'Empty: the file\'s main.agent when that is a worker, else the first of main.fallback, else claude.',
                            'select', choices_from='workers'),
    'main.fallback': field('Takes over', 'In this order, when the main agent\'s quota is in panic (and the auto pick order).',
                           'list', choices_from='workers'),
    'main.commands': field('Commands when conducting', 'A command per agent; default: its worker command.', **YAML),
    'main.ask_first': field('Ask first', 'Extra actions the conductor must ask you about.', 'list', item='action'),
    'main.check_seconds': field('Check readings every', 'How often a conduct run checks the quota.', **SEC),
    'main.rounds': field('Rounds', 'The most rounds one goal may take.', 'number', min=1),
    'main.command_timeout': field('One round may take', 'A round that takes longer is stopped.', **SEC),
    # machines
    'machines.*.host': field('Host', 'An ssh alias from ~/.ssh/config, or local for this computer.', 'text'),
    'machines.*.physical_machine': field('Physical machine',
                                         'Name the main machine entry for another environment on the same computer '
                                         '(dashboard card only; different connection addresses).', 'text'),
    'machines.*.transport': field('Reached by', 'ssh, local, wsl (a Windows machine\'s WSL) or android (Termux).', 'select',
                              choices=list(config.TRANSPORTS)),
    'machines.*.user': field('User', 'The login on the machine (Termux).', 'text'),
    'machines.*.port': field('ssh port', 'Termux\'s sshd listens on 8022.', 'number', min=1, max=65535),
    'machines.*.root': field('Copies folder', 'Under the remote home, where the copies go.', 'text'),
    'machines.*.ups': field('On a UPS', 'This machine keeps running through a power cut: preferred as the backup main machine.',
                            'switch'),
    'machines.*.always_on': field('Always on', 'This machine is never turned off: preferred as the backup main machine.', 'switch'),
    'failover.backup': field('Backup main machine', 'Takes over the dashboard, coordinator and autopilot when the main machine is '
                             'away (empty: Tutti picks the healthiest always-on one; none: no backup). docs/FAILOVER.md.', 'select',
                             choices_from='machines'),
    'failover.minutes': field('Take over after', 'How long the main machine may be silent before the backup takes over.', **MIN),
    'failover.failback_minutes': field('Return after', 'How long the main machine must be back and healthy before Tutti returns.',
                                       **MIN),
    'failover.sync_seconds': field('Sync every', 'How often the main machine copies its state to the backup.', 'number',
                                   unit='seconds', min=10),
    'failover.budget_mb': field('State budget', 'Tutti\'s state should stay under this size (`tutti doctor` shows it).', 'number',
                                unit='MB', min=1),
    'selfheal.enabled': field('Self-healing', 'Each round Tutti installs what work needs and restarts or repairs what broke. '
                              'docs/SELFHEAL.md.', 'switch'),
    'selfheal.every_seconds': field('Self-heal every', 'How often the self-heal checks run on each machine.', 'number',
                                    unit='seconds', min=30),
    'machines.*.slots': field('Slots', 'At most this many runs at once.', 'number', min=1),
    'machines.*.min_free_gb': field('Free memory needed', 'A run starts only with this much memory free.', **GB),
    'machines.*.max_load_ratio': field('Load limit', 'Fit only while its load is under this share of its cores.', 'number', min=0, max=4,
                                   step=0.05),
    'machines.*.use_pct': field('Tutti\'s share (%)', 'How much of this machine\'s slots, cores and model threads Tutti uses.',
                                'number', min=0, max=100, step=5),
    'machines.*.in_use_pct': field('Share while in use (%)', 'While someone uses this machine (keyboard or mouse in the last '
                                   'minutes).', 'number', min=0, max=100, step=5),
    'machines.*.quiet_pct': field('Share in quiet hours (%)', 'Fans and noise at night: the share in its quiet hours.', 'number',
                                  min=0, max=100, step=5),
    'machines.*.limit': field('Limited hours', 'A daily window when Tutti uses at most pct % of it: {window: "18-01", pct: 60, '
                              'days: [mon, ...]} (every day unless listed; main: true follows the main machine on a move).', **YAML),
    'machines.*.quiet_hours': field('Quiet hours', 'Local-time spans such as ["23:00-07:00"] (else autopilot.quiet_hours).', **YAML),
    'machines.*.heat': field('Heat', 'cap_c: one run at this temperature; stop_c: none; hot_slots: runs while hot.', **YAML),
    'machines.*.yield_file': field('Yield flag', 'While this file exists, the machine gets nothing new.', 'text'),
    'machines.*.lock_prefix': field('Lock files', 'Where its slot locks live.', 'text'),
    'machines.*.locks': field('Named locks', 'One slot per lock name, instead of a count.', 'list', item='lock'),
    'machines.*.wsl_helper': field('WSL helper', 'The path inside WSL of the base64 runner.', 'text'),
    'machines.*.runner': field('Runner override', 'This machine\'s own runner keys over the shared runner.', **YAML),
    'machines.*.containers': field('Containers', 'Container name prefixes shown as its jobs.', 'list', item='prefix'),
    'machines.*.files': field('Files override', 'This machine\'s own files keys.', **YAML),
    'machines.*.enabled': field('On', 'An Android machine is opt-in.', 'switch'),
    'routing.order': field('Machine order', 'First fit wins; when none fits the run queues on the first.', 'list', choices_from='machines'),
    'runner.kind': field('Runs commands with', 'shell: plain bash; docker: a capped container; native: niced (Termux).', 'select',
                     choices=list(config.RUNNERS)),
    'runner.image': field('Image', 'The docker image a run uses.', 'text'),
    'runner.memory_gb': field('Memory cap', 'Per run.', **GB),
    'runner.cpus': field('CPU cap', 'Per run.', 'number', unit='CPUs', min=1),
    'runner.env': field('Environment', 'Variables set in every run (never secrets).', **YAML),
    'runner.args': field('Extra docker flags', 'Added to every docker run.', **CMD),
    'runner.prelude': field('Prelude', 'A command run first in every run.', 'text'),
    'runner.rewrite': field('Rewrites', 'Substring to replacement, for a command that starts docker itself.', **YAML),
    'sync.exclude': field('Never copied', 'Patterns never copied to a machine.', 'list', item='pattern'),
    'sync.ensure_dirs': field('Folders to create', 'Created on the machine before a run.', 'list', item='folder'),
    'files.cache': field('Files cache', 'Where `tutti files send` puts files on a machine.', 'text'),
    'files.budget_gb': field('Cache budget', 'The cache\'s size budget.', **GB),
    'files.keep_free_gb': field('Keep free', 'Disk left free on the machine.', **GB),
    'files.bwlimit_kbps': field('Bandwidth limit', '0: no limit.', 'number', unit='kbit/s', min=0),
    'files.compress': field('Compress', 'Compress files in transit.', 'switch'),
    'files.refuse': field('Also refuse', 'Patterns refused on top of the secrets that always are.', 'list', item='pattern'),
    'guard.low_gb': field('Pause containers under', '`tutti guard` pauses the runner\'s containers below this free memory.', **GB),
    'guard.high_gb': field('Resume them over', 'And resumes them above this.', **GB),
    # devices
    'devices.*.serial': field('adb serial', 'Wireless: its mDNS name (adb-…._adb-tls-connect._tcp, set by `tutti devices pair`); '
                                'USB: from `adb devices`.', 'text'),
    'devices.*.wireless': field('Wireless adb', 'host:port addresses tried after USB: LAN first, then the VPN.', 'list',
                                item='host:port'),
    'devices.*.machine': field('Plugged into', 'The machine the device is reached from.', 'select', choices_from='machines'),
    'night.enabled': field('Night mode', 'While Tutti holds or works an Android device in the night hours: 1 % brightness, every '
                           'volume at 0 (the alarm stays yours); your own values come back at hand-back.', 'switch'),
    'night.hours': field('Night hours', 'Local hours HH-HH, e.g. 22-08.', 'text'),
    'night.devices': field('Night mode on', 'The devices it applies to; empty: every device.', 'list', choices_from='devices'),
    'devices.*.roles': field(ROLES_OVERRIDE, 'The whole list, over what is detected (tutti/roles.py): cast-receiver, '
                             'cast-sender, android-device, tv-device...', 'list', item='role'),
    'devices.*.roles_add': field(ROLES_ADDED, 'Added to the detected roles (`tutti roles set`).', 'list', item='role'),
    'devices.*.roles_off': field(ROLES_REMOVED, 'Taken from the detected roles (`tutti roles unset`).', 'list', item='role'),
    'machines.*.roles': field(ROLES_OVERRIDE, 'The whole list, over what is detected: windows-test-runner, ui-desktop, gpu-ai, '
                              'docker-host... (docs/MACHINES.md, Roles).', 'list', item='role'),
    'machines.*.roles_add': field(ROLES_ADDED, 'Added to the detected roles (`tutti roles set`).', 'list', item='role'),
    'machines.*.roles_off': field(ROLES_REMOVED, 'Taken from the detected roles (`tutti roles unset`).', 'list', item='role'),
    'devices.*.hours': field('Blocked hours', 'Daily local hours when the device is not used at all, e.g. {window: "22-07", '
                             'days: [mon, tue]} (no work from 30 min before; its own settings restored). Empty: none.', 'yaml'),
    'home_assistant.url': field('Home Assistant', 'Its address, e.g. http://homeassistant.local:8123 (no user or password in '
                                'it). Shown on the Machines tab (docs/MACHINES.md, Home Assistant).', 'text'),
    'home_assistant.plugs': field('Machine plugs', 'Which power sensor is each machine\'s plug, e.g. {w5090: '
                                  'sensor.pc_plug_power}; a sensor with the machine\'s name in its id is found by itself.', 'yaml'),
    'home_assistant.token_env': field('Its token name', 'The name its long-lived access token has in '
                                      '~/.config/tutti/home-assistant.env (default TUTTI_HA_TOKEN); the token itself is never '
                                      'in the config.', 'text'),
    'lan_devices.*.roles': field(ROLES_OVERRIDE, 'A media device on the network (by its address): the whole list.', 'list',
                                 item='role'),
    'lan_devices.*.roles_add': field(ROLES_ADDED, 'Added to what mDNS and SSDP found, e.g. bluetooth-audio.', 'list', item='role'),
    'lan_devices.*.roles_off': field(ROLES_REMOVED, 'Taken from what mDNS and SSDP found.', 'list', item='role'),
    'devices.*.kind': field('Kind', 'Set by `tutti devices pair`: phone, tablet, watch or tv.', 'select',
                            choices=['phone', 'tablet', 'watch', 'tv']),
    'devices.*.model': field('Model', 'Set by `tutti devices pair`; a move checks the device it reaches is this one.', 'text'),
    'devices.*.capabilities': field('Capabilities', 'Set by `tutti devices pair`: camera, telephony, touchscreen, tv...', 'list',
                                    item='capability'),
    'devices.*.adb': field('adb', 'The adb on its machine; empty: found by itself (PATH, the Android SDK, Tutti\'s own copy).', 'text'),
    **{f'devices.*.{k}': v for k, v in POLICY.items()},
    # autopilot
    'autopilot.enabled': field('Autopilot', 'Sends backlog tasks to accounts that are behind their pace.', 'switch'),
    'autopilot.accounts': field('Trigger accounts', 'Their pace starts the autopilot.', 'list', choices_from='accounts'),
    'autopilot.ladder': field('Retry ladder', 'Attempt n starts at step n; the last step repeats.', 'list', choices_from='accounts'),
    'autopilot.fresh_fallback_accounts': field('Fresh-work fallback accounts', 'Opt-in ladder accounts may take fresh work '
                                               'after usual candidates when their measured, unshared quota has room.',
                                               'list', choices_from='accounts'),
    'autopilot.max_parallel': field('Runs at once (cap)', 'Optional: the most autopilot runs at once. Empty: adaptive (account room, '
                                    'free memory and cores, free gate slots).', 'number', min=1, unit='runs'),
    'autopilot.parallel': field('Adaptive parallelism', 'run_gb, run_cpu, reserve_gb, gate_share, ceiling and the heavy-gate pattern '
                                '(docs/AUTOPILOT.md).', **YAML),
    'autopilot.no_progress': field('Park after', 'A task with the same failure this many times waits for you.', 'number', min=1,
                               unit='attempts'),
    'depbots.enabled': field('Dependency bots and security', 'Dependabot and Cursor pull requests and the repos\' vulnerability '
                             'reports become tasks (docs/SECURITY-INTAKE.md).', 'switch'),
    'depbots.every_minutes': field('Read every', 'How often the autopilot reads them.', 'number', min=15, unit='minutes'),
    'depbots.group_trivial': field('Group small bumps', 'Green patch and minor bumps become one task per repo.', 'switch'),
    'depbots.bots': field('Dependency bots', 'Whose pull requests are dependency updates: name, authors, branch prefixes.', **YAML),
    'depbots.scanners': field('Security scanners', 'Whose review comments are security findings: name, authors.', **YAML),
    'autopilot.max_attempts': field('Most attempts', 'A task is parked after this many attempts in all.', 'number', min=1, unit='attempts'),
    'autopilot.quiet_hours': field('Quiet hours', 'Local-time spans the autopilot must not run in.', 'timespans'),
    'autopilot.fill_room': field('Fill free room', 'Free room on the trigger accounts is filled from the repos\' TODOs, gaps and '
                             'untested files.', 'switch'),
    'autopilot.auto_review': field('Review and land results', 'A passing result is reviewed by review.fallback, then landed.', 'switch'),
    'autopilot.repos.*.path': field('Folder', 'The repository on this computer.', 'text'),
    'autopilot.repos.*.check': field('Check', 'The repo\'s gates; a result passes only when they pass.', 'text'),
    'autopilot.repos.*.where': field('Checks run', 'local, or route (on a machine).', 'select', choices=['local', 'route']),
    'autopilot.repos.*.pr': field('Open a pull request', 'Deprecated by land: pr.', 'switch'),
    'autopilot.repos.*.land': field('Land as', 'merge after a clean review, a pull request a person merges, or a branch.', 'select',
                                choices=['pr', 'merge', 'branch'], default='pr'),
    'autopilot.repos.*.base': field('Base branch', 'Where results branch from and land.', 'text'),
    'autopilot.repos.*.commit': field('Commit message', '{title} is the task\'s title.', 'text', default='chore(autopilot): {title}'),
    'autopilot.repos.*.trailers': field('Commit trailers', 'Lines added to every commit.', 'list', item='trailer'),
    'autopilot.repos.*.gaps': field('Gap sources', 'Where fill_room looks for work.', **YAML),
    # keep busy
    'presence.grace_seconds': field('Missing for', 'A target missing this long counts as away or asleep.', **SEC),
    'presence.night': field('Night hours', 'Local hours in which a machine that does not answer is asleep, e.g. 23-08.', 'text'),
    'presence.long_minutes': field('Long shard', 'A shard this long never goes to a target likely to leave soon.', **MIN),
    'presence.leave_soon': field('Likely to leave', 'Under this share of presence in the coming hours, a target is leaving soon.',
                                 'number', min=0, max=1),
    'presence.owner_laptop': field('My laptop', 'The machine your main agent lives on: away or asleep means you are away.', 'text'),
    'presence.host_laptop': field('Tutti runs on a laptop', 'Shows the warning to move Tutti to an always-on machine.', 'switch'),
    'presence.sleep_watch': field('Hand work over before sleep', 'On Linux, running work is handed over before the computer sleeps.',
                                  'switch'),
    'presence.move_after_minutes': field('Move urgent work after', 'Urgent work waiting this long on an absent target moves by '
                                         'itself (off when empty).', **MIN),
    'presence.wake': field('Wake-on-LAN', 'Wake a sleeping machine (with wol_mac) when work waits for it; on the LAN only.', 'switch'),
    'presence.wake_after_minutes': field('Wake after', 'Work waiting this long wakes its machine.', **MIN),
    'dispatch.enabled': field('Keep busy', 'Idle devices and machines pull pending test shards by themselves.', 'switch'),
    'dispatch.idle_minutes': field('Idle after', 'A target with nothing to do this long is offered work.', **MIN),
    'dispatch.lease_minutes': field('Lease', 'A run with no heartbeat this long frees its target; its shard is retried.', **MIN),
    'dispatch.retries': field('Retries', 'Tries of a failed shard on the same commit.', 'number', min=0),
    'dispatch.quiet_hours': field('Quiet hours', 'Local-time spans in which nothing new is sent.', 'timespans'),
    'dispatch.call_poll_seconds': field('Look for phone calls every', 'A phone in a call pauses its run.', **SEC),
    'dispatch.resume_after_call_seconds': field('Resume after a call', 'Wait this long after a call ends.', **SEC),
    'dispatch.pause_on_owner_use': field('Pause while I use a device', 'A run pauses while you use the device.', 'switch'),
    'projects.*.commit': field('Latest commit', 'Shards not passed on this commit are pending.', 'text'),
    'projects.*.tests': field('Test suites', 'kind, targets, run with {shard}, shards, retries (docs/DISPATCH.md).', **YAML),
    # fleet (docs/FLEET.md)
    'work_queue.enabled': field('Work queue', 'Machines with a free slot run the projects\' gates, tests and reports.', 'switch'),
    'work_queue.jobs': field('Jobs', 'name, run, project, repo, where, heavy, every, findings (docs/FLEET.md).', **YAML),
    'fleet.enabled': field('Use every machine\'s models', 'Find each machine\'s Ollama and make it a worker.', 'switch'),
    'fleet.port_base': field('Tunnel ports from', 'Another machine\'s Ollama is reached on this port and the next ones.',
                             'number', min=1024, step=1),
    'fleet.memory_floor_gb': field('Keep memory free (GB)', 'No local model runs on this computer with less memory free.',
                                   'number', min=0, step=0.5),
    # the decisional model (docs/FLEET.md, *The decisional model*)
    'decisional.enabled': field('Judgement model', 'Tutti\'s judgement steps ask the best free model that fits your strongest '
                                'machine first.', 'switch'),
    'decisional.host': field('Machine', 'Empty: the strongest suitable machine (the most graphics memory).', 'select',
                             choices_from='machines'),
    'decisional.model': field('Model', 'Empty: the first candidate that fits. A name such as qwen3:32b pins it.', 'text'),
    'decisional.candidates': field('Candidates', 'Free models, best first: a name that shows its size, or {model, gb}.', **YAML),
    'decisional.purposes': field('Steps that ask it first', 'unblock, triage, scout, needs, welcome, routing.', **YAML),
    'decisional.auto_pull': field('Fetch it when missing', 'The probe pulls the model (tens of GB) onto the chosen machine; never '
                                  'while someone is at this computer.', 'switch'),
    'decisional.allow_cpu': field('Allow memory (RAM)', 'A machine without enough graphics memory may hold the model in RAM.',
                                  'switch'),
    'decisional.min_free_gb': field('Keep memory free (GB)', 'In RAM mode, a candidate must leave this much memory free.', **GB),
    'decisional.pull_timeout_minutes': field('Pull time limit', 'How long a pull may take before it counts as failed.', **MIN),
    'decisional.hardware': field('Hardware', 'Where it is not measured: {<machine>: {vram_gb: 32, mem_gb: 64}}.', **YAML),
    # energy (docs/ENERGY.md)
    'energy.price_per_kwh': field('Price per kWh', 'From your bill, in your currency. Empty: a typical price, shown as estimated.',
                                  'number', min=0, step=0.01),
    'energy.currency': field('Currency', 'Empty: from your country (time zone, then locale).', 'text'),
    'energy.country': field('Country', 'Two letters (RO, DE, US). Empty: detected from the time zone, then the locale.', 'text'),
    'energy.tariff': field('Time-of-day prices', 'Local hours and price per kWh: [{hours: "23-07", price: 0.9}].', **YAML),
    'energy.prefer_cheap_hours': field('Prefer cheap hours', 'With time-of-day prices, local-model runs wait for the cheap hours.',
                                       'switch'),
    'energy.include_idle': field('Count idle draw', 'The power while Tutti keeps a model loaded after its run.', 'switch'),
    'energy.keep_alive_minutes': field('Model kept loaded', 'How long after a run the idle draw counts as Tutti\'s.', **MIN),
    'energy.compare_with': field('Compare with', 'The cloud API priced for the same tokens.', 'select',
                                 choices=['claude-sonnet', 'claude-haiku', 'gemini-flash', 'gpt-mini']),
    'energy.machines': field('Machines', 'Per machine: base_w (board, RAM, disks), gpu, gpu_tdp_w, cpu_tdp_w, ollama (host:port).',
                             **YAML),
    # updates
    'backup.enabled': field('Daily backup', 'The config and the state folder, verified, every backup.every_hours (docs/BACKUP.md).',
                            'switch'),
    'backup.dest': field('Backup folder', 'Where the archives go: any folder you may write, a mounted NAS too. Empty: '
                         '~/.local/share/tutti/backups.', 'text'),
    'backup.keep': field('Backups kept', 'The newest ones kept; older ones are deleted after a verified backup.', 'number',
                         min=1, max=365),
    'backup.every_hours': field('Back up every', 'Hours between backups (24: daily).', 'number', min=1, max=720),
    'self_update.enabled': field('Tutti updates itself', 'At login and daily: the newest checksummed release.', 'switch'),
    'update.auto': field('Update by itself', 'Daily from the dashboard.', 'switch'),
    'update.every_hours': field('Every', 'How often worker CLIs are updated.', **HRS),
    'update.packages': field('npm packages', 'Worker CLIs installed with npm.', 'list', item='package'),
    'update.programs': field('Programs', 'Worker CLIs that update themselves.', 'list', item='program'),
    # time
    'time.zone': field('Time zone', 'auto: this computer\'s, found by itself (daylight saving included); or an IANA name.', 'text',
                       suggest=['auto', 'Europe/Bucharest', 'Europe/London', 'America/New_York']),
    # pacing
    'pacing.dates': field('Days with other hours', 'One day\'s own owner hours: {"2026-11-16": "20-24"} or "off".', **YAML),
    'pacing.schedule.days': field('Working days', 'The days you prompt yourself.', 'days'),
    'pacing.schedule.hours': field('Hours kept for you', 'The reserve kept on those days.', **HRS),
    'pacing.schedule.window': field('Your hours', 'Local hours, from-to.', 'hours'),
    'pacing.schedule.rate': field('Points per hour', 'What one of your hours spends.', 'number', min=0, step=0.1),
    'pacing.schedule.utc_offset': field('Fixed UTC offset', 'Empty (best): time.zone, with daylight saving.', 'number', unit='hours',
                                        min=-12, max=14),
    'pacing.mode': field('Mode', 'auto learns from your readings; active, away or vacation-until-YYYY-MM-DD say it yourself.', 'text',
                     suggest=['auto', 'active', 'away', 'vacation-until-YYYY-MM-DD']),
    'pacing.away_after_hours': field('Away after', 'Your hours with no rise after which auto says away.', **HRS),
    'pacing.stop_after_minutes': field('Stopped after', 'Minutes with no rise in your hours after which today\'s reserve is released.',
                                       **MIN),
    'pacing.accounts': field('Your accounts', 'Empty: the main agent\'s.', 'list', choices_from='accounts'),
    # autocompact
    'autocompact.enabled': field('Autocompact', 'Compact a worker\'s context before its window is full.', 'switch'),
    'autocompact.threshold_pct': field('Compact at', 'The share of the window at which the context is compacted.', 'number', unit='%',
                                   min=10, max=95),
    'autocompact.context_tokens': field('Window', 'When workers.<name>.context_tokens does not say.', 'number', unit='tokens', min=1000),
    # review
    'review.reviewers': field('Reviewers', 'Who to ask for a pull request review.', 'list', choices=['copilot', 'coderabbit', 'greptile']),
    'review.timeout_minutes': field('Wait for them', '0: do not wait, fall back at once.', **MIN),
    'review.poll_seconds': field(LOOK_EVERY, 'How often to look for their answer.', **SEC),
    'review.coderabbit': field('When CodeRabbit is limited', 'wait: no merge until it reviews (asked again when its limit passes); '
                               'fallback: another reviewer stands in, CodeRabbit is asked again later; skip: never asked; '
                               'skip-when-unavailable: merge on the other reviews.', 'select',
                               choices=['wait', 'fallback', 'skip', 'skip-when-unavailable'], default='wait'),
    'review.greptile': field('When Greptile is limited', 'The same choices as for CodeRabbit, for Greptile.', 'select',
                             choices=['wait', 'fallback', 'skip', 'skip-when-unavailable'], default='wait'),
    'review.repos.*.greptile': field('Greptile for this repo', 'Overrides the household policy for one owner/name.', 'select',
                                     choices=['wait', 'fallback', 'skip', 'skip-when-unavailable']),
    'review.repos.*.coderabbit': field('CodeRabbit for this repo', 'Overrides the household policy for one owner/name.', 'select',
                                       choices=['wait', 'fallback', 'skip', 'skip-when-unavailable']),
    'review.modes': field('When each reviewer is asked (admin)', 'Per reviewer (copilot, coderabbit, greptile, a scout): never, '
                          'on_pr_open (the first ask only), on_new_pushes (first ask and after every push; the default) or '
                          'on_pr_ready_to_merge (once, when every other merge gate is green).', **YAML, default={}),
    'review.rounds': field('Review rounds (admin)', 'How many rounds (ask, fix the findings, ask again) each reviewer gets per pull '
                           'request: copilot, coderabbit, greptile, a scout (grok) and local (the fallback worker). 1 each by default; 0: '
                           'not called.', **YAML, default={}),
    'review.fallback': field('Fallback reviewers', 'Cheap workers that review when no one answers.', 'list', choices_from='workers'),
    # scouts
    'scouts.*.kind': field('Kind', 'grok-bot-webhook: Grok Bot\'s webhook routine.', 'select', choices=['grok-bot-webhook']),
    'scouts.*.url_env': field('URL variable', 'The environment variable that holds the webhook URL (set by `tutti scout setup`).', 'text'),
    'scouts.*.key_env': field('Key variable', 'The environment variable that holds the webhook key (set by `tutti scout setup`).', 'text'),
    'scouts.*.account': field('Paced against', 'The allowance its watches and questions spend.', 'select', choices_from='accounts'),
    'scouts.*.reply': field('Answers on', 'The GitHub repo and label of the issue it answers on.', **YAML),
    'scouts.*.skills': field('Skills', 'What it may be asked to do.', 'list', item='skill'),
    'scouts.*.timeout_minutes': field('Wait for an answer', '`tutti scout ask` waits this long.', **MIN,
                                  default=scouts.DEFAULTS['timeout_minutes']),
    'scouts.*.poll_seconds': field(LOOK_EVERY, 'How often to look for an answer.', **SEC, default=scouts.DEFAULTS['poll_seconds']),
    'scouts.*.auto': field('Rounds', 'The dashboard sends due watches and collects answers.', 'switch', default=scouts.DEFAULTS['auto']),
    'scouts.*.max_open': field('Most waiting', 'Questions waiting for answers at once.', 'number', min=1,
                               default=scouts.DEFAULTS['max_open']),
    'scouts.*.per_tick': field('Per round', 'Questions sent in one round.', 'number', min=1, default=scouts.DEFAULTS['per_tick']),
    'scouts.*.give_up_hours': field('Give up after', 'A question with no answer this long is dropped.', **HRS,
                                default=scouts.DEFAULTS['give_up_hours']),
    'scouts.*.watches': field('Watches', 'Questions sent when due; each answer is an issue to triage.', **YAML),
    'scouts.*.watches.*.every': field('Every', '1d, 7d, 12h…', 'text'),
    'scouts.*.watches.*.early': field('May go early', 'Ahead of time while the allowance is behind its pace.', 'switch', default=False),
    'scouts.*.watches.*.task': field('Task', 'What the scout is asked.', 'textarea'),
    'scouts.*.sentry_triage': field('Sentry triage', 'A daily watch per project with a sentry entry.', 'switch', default=False),
    'scouts.*.spend_rest': field('The week\'s plan', 'Scout work spread evenly over the week up to 95 %, a burst before the reset '
                                 '(true, false or {hours, stop_at, pace_gap_hours, burst_per_tick, review_repos}).', **YAML, default=True),
    'scouts.*.watches.*.project': field('Project', 'Orders the extra runs of spend the rest by project priority.', 'text'),
    'scouts.*.review_trial': field('Review trial', 'The scout also reviews the next pull requests of these repos.', **YAML),
    # telemetry and self-improvement
    'telemetry.sentry_dsn': field('Sentry DSN', 'Set with `tutti telemetry setup`; never shown here.', 'secret',
                              setup='tutti telemetry setup'),
    'telemetry.endpoint': field('Own collector', 'Your own collector instead of Sentry.', 'text'),
    'telemetry.environment': field('Environment tag', 'Sentry\'s environment tag.', 'text'),
    'self_improve.enabled': field('Self-improvement', 'Crashes and insights become tasks, pull requests and releases.', 'switch'),
    'self_improve.every_hours': field(LOOK_EVERY, 'How often it looks for new crashes and insights.', **HRS),
    'self_improve.max_new_per_day': field('New tasks a day', 'At most this many.', 'number', min=0),
    'self_improve.min_count': field('Seen at least', 'A crash becomes a task once seen this many times.', 'number', min=1),
    'self_improve.home': field('Home project', 'The project Tutti\'s own fixes go to.', 'select', choices_from='projects'),
    'autopilot.stuck_minutes': field('Stuck after', 'A run with no new output this long is held visibly while Tutti waits '
                                     'for its report.', **MIN),
    'welcome.enabled': field('Welcome back', 'When you return after being away, a summary of what happened, and What did I '
                             'miss? at any time. Each user may turn theirs off.', 'switch', default=True),
    'welcome.away_hours': field('Away after', 'Gone this long counts as away (empty: the pacing\'s away_after_hours).', **HRS),
    'welcome.ai': field('Written by a local model', 'A local model writes the opening words when AI assist allows; '
                        'otherwise the template.', 'switch'),
    'ai_assist.enabled': field('AI assist (admin opt-in)', 'Tutti uses AI to make itself better: proposals, smarter retries, '
                               'auto-tuning, research. Off until you turn it on.', 'switch', default=False),
    'ai_assist.local': field('Local AI', 'Your local models (ollama workers) read Tutti\'s telemetry, failures and tasks.', 'switch'),
    'ai_assist.online': field('Online AI', 'The scout researches release notes, models and prices, and reviews the scrubbed '
                              'telemetry; only with spare quota, never your reserve.', 'switch', default=False),
    'ai_assist.online_code': field('Code to online AI', 'Briefs, failure output and code may go to online models too. Off: '
                                   'they stay on local models.', 'switch', default=False),
    'ai_assist.auto_tune': field('Auto-tune', 'Small, safe changes to Tutti\'s own parameters are applied by themselves '
                                 '(logged, undo on the dashboard); larger ones wait for you.', 'switch'),
    'ai_assist.every_hours': field(LOOK_EVERY, 'How often the local model analyses the telemetry.', **HRS),
    'ai_assist.max_proposals': field('Proposals a round', 'At most this many; the day\'s task cap still holds.', 'number', min=1),
    'ai_assist.local_worker': field('Local model', 'Kept for old configs: Tutti\'s own AI now always takes the most capable '
                                    'free model in the household (tutti/brain.py).', 'select', choices_from='workers'),
    'ai_assist.online_worker': field('Online fallback', 'A subscription CLI worker asked when no local model answers, or auto: '
                                     'the first one with room within pacing. Empty: no online fallback.', 'text'),
    'ai_assist.preempt': field('Tutti first on the best model', 'true: Tutti\'s own short jobs park other work on the best local '
                               'model (it waits, nothing is lost) and resume it after; false: never; or a list of the kinds that '
                               'may (needs, welcome, triage, sizing, compaction, scout, self-improve, unblock, conductor).', **YAML),
    'ai_assist.household_models': field('Use members\' shared models', 'Tutti\'s own AI may also use the models on machines '
                                        'other household members share. Off by default: its prompts (failure output, task '
                                        'titles) then go to a machine another person controls.', 'switch', default=False),
    'ai_assist.unblock': field('Unblocker', 'Each round Tutti finds blocked or failing work and clears what it can itself, '
                               'local models first; only true decisions reach you.', 'switch', default=True),
    'ai_assist.unblock_repos': field('Rerun red CI in', 'owner/name repositories whose failed pull-request checks the unblocker '
                                     'reruns once per commit.', 'list', item='repository'),
    'ai_assist.scout': field('Research scout', 'The scout that researches (empty: the one with the research skill).', 'select',
                             choices_from='scouts'),
    'ai_assist.research_every': field('Research every', 'How often the online research and design review run, e.g. 7d.', 'text'),
    # projects and repos
    'projects.*.priority': field('Priority', '0 is the most important; work is shared out by priority, the lowest still gets its turn.',
                             'number', min=0, step=1, default=None),
    'projects.*.match': field('Matches', 'A regex on the git remote.', 'text'),
    'projects.*.repo': field('GitHub repo', 'owner/name: its pull requests, CI and reviews.', 'text'),
    'projects.*.containers': field('Containers', 'Container name prefixes shown as its jobs.', 'list', item='prefix'),
    'projects.*.machines': field('Machines', 'Machines lent to this project.', 'list', choices_from='machines'),
    'projects.*.windows': field('Windows folders', 'A WSL machine\'s Windows folder of this project.', **YAML),
    'projects.*.checks': field('Checks', '`tutti run --repo` in this repo runs these.', **YAML),
    'projects.*.sentry': field('Sentry project', '{org, project}: a daily Sentry triage by a scout.', **YAML),
    'projects.*.self_improve': field('Self-improvement here', 'tasks, autopilot_repo, base, merge, release.', **YAML),
    'ship.protected': field('Protected branches', 'Never committed to by accident.', 'list', item='branch'),
    'ship.format': field('Formatter', 'A command; TUTTI_CHANGED holds the uncommitted files.', 'text', default=''),
    'ship.add': field('Add', 'Paths `git add` takes.', 'list', item='path'),
    'ship.share': field('Shared from the main checkout', 'A linked worktree gets these by hard link when missing.', 'list', item='folder',
                    default=[]),
    'ship.trailer': field('Commit trailer', 'For example a Co-Authored-By line.', 'text', default=''),
    'ship.base': field('Changed since', 'TUTTI_CHANGED for the checks: files changed since this ref.', 'text'),
    'ship.checks': field('Targeted checks', 'Only what changed.', **YAML),
    'ship.full_checks': field('Full checks', '`--full`: before a release.', **YAML),
    'ship.push': field('Push to', '{remote, branch}.', **YAML),
    'ship.deploy': field('Deploy', 'A command run after the push.', 'text', default=''),
    'delegate.project': field('Default project', 'The checks below are for this project only.', 'text'),
    'delegate.checks': field('Checks of a worker\'s diff', 'Default: ship.checks.', **YAML),
    'delegate.prefix': field('Worktree prefix', 'Worktrees go under project.worktrees/<prefix><id>.', 'text'),
    'delegate.remote_track': field('Folder on the test machine',
                                   'Base name for per-run remote copies (`<name>-<run-id>`); --remote-track keeps one shared folder.',
                                   'text'),
    'test.prepare': field('Steps first', 'When a test command matches `when`, `run` goes in front.', **YAML),
    'test.no_coverage': field('Coverage off looks like', 'A regex for a run with coverage turned off.', 'text'),
    'translate.translator': field('Translator', 'The worker that translates.', 'select', choices_from='workers'),
    'translate.judge': field('Judge', 'A different model that checks the translation.', 'select', choices_from='workers'),
    'translate.source_language': field('From', 'The source language.', 'text'),
    'translate.languages': field('Into', 'code: language name.', **YAML),
    'translate.keep': field('Never translated', 'Names kept as they are.', 'list', item='name'),
    'translate.retries': field('Retries', 'Tries after a failed check.', 'number', min=0),
    # chat
    'chat.viewer_tasks_per_day': field('Work a viewer may create a day', 'Tasks, hand-offs and scout questions from the chat, per '
                                       'viewer; 0: viewers only ask. Admins have no limit.', 'number', min=0, max=1000, unit='a day'),
    'chat.viewer_projects': field('Viewers\' projects', 'The projects viewers may queue work for; empty: every project.', 'list',
                                  item='project'),
    'chat.history_turns': field('Messages a model sees', 'The earlier messages of a conversation sent with each question.', 'number',
                                min=0, max=50),
    # dashboard
    'browser.machine': field('Browser machine', 'The one machine whose Chrome every user and worker uses; empty is this one.',
                             'select', choices_from='machines'),
    'browser.mode': field('Which Chrome', 'existing: your own Chrome and its sign-ins; dedicated: Tutti\'s own profile; '
                          'isolated: a sandboxed Chrome as its own user (`tutti browser isolate`).', 'select',
                          choices=['existing', 'dedicated', 'isolated']),
    'browser.sites': field('Sites to sign in to', 'Shared by everyone who uses the work browser: choose only what the work needs.',
                           'list', item='https address'),
    'browser.scopes': field('Sites per task type', '{task type: [hosts]} a task may open; the defaults come from the sites and the '
                            'usage pages.', **YAML),
    'browser.allow_local': field('Local addresses allowed', 'Isolated mode: the local services it may reach (all others are '
                                 'blocked).', 'list', item='address'),
    'browser.file_tasks': field('Task types that get files', 'Only these may take a downloaded file, by its name.', 'list',
                                item='task type'),
    'browser.max_file_mb': field('Largest file a task takes', 'A downloaded file over this size is refused, with the reason.',
                                 'number', min=1, max=2048),
    'browser.max_tabs': field('Tabs at once', 'More tasks wait in the queue.', 'number', min=1, max=20),
    'browser.path': field('Browser program', 'Chrome or Chromium to start; empty finds one (then Playwright\'s Chromium).', 'text'),
    'browser.profile': field('Tutti profile folder', 'Dedicated mode\'s own sign-ins; empty is the default. Never your browser\'s.',
                             'text'),
    'browser.user_profile': field('Your Chrome profile folder', 'Existing mode; empty is Chrome\'s default.', 'text'),
    'browser.socket': field('Isolated socket', 'Isolated mode\'s Unix socket; empty is /run/tutti-browser/devtools.sock.', 'text'),
    'browser.headless': field('Hidden', 'Dedicated mode: readings run without a window; a sign-in opens one.', 'switch'),
    'browser.idle_minutes': field('Close after', 'The connection (and Tutti\'s own Chrome) closes when no task used it this long.',
                                  **MIN),
    'browser.port': field('DevTools port', 'Dedicated mode, on 127.0.0.1 only: nothing on the network can drive it.', 'number',
                          min=1024, max=65535),
    'dashboard.port': field('Port', 'The dashboard\'s port on 127.0.0.1.', 'number', min=1, max=65535),
    'dashboard.bind': field('Address', 'Where the dashboard listens: 127.0.0.1, or this computer\'s LAN or VPN address (sign-in '
                            'required; never a public address).', 'text'),
    'auth.client_id': field('Web client ID', 'The Google OAuth "Web application" client for this computer and https names. '
                            'Public, not a secret. Empty: the one `tutti auth setup` saved.', 'text'),
    'auth.session_hours': field('Session length', 'How long a sign-in lasts before signing in again.', **HRS),
    'dashboard.refresh_seconds': field('Page refresh', 'How often this page asks for news.', **SEC),
    'dashboard.probe_seconds': field('Probe machines every', 'How often machines and workers are probed.', **SEC),
    'dashboard.stall_minutes': field('Stalled after', 'A running job whose log is quiet this long is stalled.', 'number', unit='minutes',
                                 min=1),
    'dashboard.watch_scripts': field('Scripts shown as jobs', 'Running scripts with these names.', 'list', item='script'),
    'dashboard.watch_agents': field('Agents shown', 'Agent CLIs shown by process.', 'list', item='program'),
    'dashboard.task_globs': field('Background task outputs', 'An agent session\'s task outputs (shown by time only).', 'list', item='glob'),
    'dashboard.shapes': field('Progress shapes', 'A job kind\'s known phases, or lines counted against a total.', **YAML),
    'dashboard.typical_minutes': field('Typical minutes', 'The estimate for an agent kind before any run of it finished.', **YAML),
    'project.worktrees': field('Worktrees folder', '`tutti test <track>` runs over <worktrees>/<track>.', 'text'),
    'project.state': field('State folder', 'Logs, pid files and the usage log.', 'text'),
    # you and your household
    'user.name': field('Your name', 'Empty: the login\'s.', 'text'),
    'user.weight': field('Your share', 'Your weight in the household\'s shared machines.', 'number', min=0, step=0.5),
    'host': field('Where Tutti runs', 'Written by `tutti move` (docs/MOVE.md): the machine that serves the dashboard and keeps '
                  'the state, and on a client how to reach it. Change it with `tutti move`, not by hand.', **YAML),
    'household.port': field('Coordinator port', 'Where the household\'s coordinator listens.', 'number', min=1, max=65535),
    'household.bind': field('Coordinator address', 'The address it binds to.', 'text'),
    'household.lease_minutes': field('Lease', 'A task\'s lease without a heartbeat.', **MIN),
    'household.starve_minutes': field('Starved after', 'A member with no slot this long counts as starved.', **MIN),
    'household.share_spare_hours': field('Share spare room', 'How long before a reset spare room may be shared.', **HRS),
    'household.timeout_seconds': field('Coordinator timeout', 'After this, a member works alone.', **SEC),
    # the optional private network (docs/NETWORK.md)
    'vpn.prefer': field('Set up', 'Which one `tutti vpn setup` sets up; neither is on until you run it.', 'select',
                        choices=['tailscale', 'wireguard']),
    'vpn.wireguard.port': field('WireGuard port', 'UDP; the only port ever forwarded on the router.', 'number', min=1024, max=65535),
    'vpn.wireguard.subnet': field('WireGuard subnet', 'auto: a free private /24, never 100.64.0.0/10.', 'text'),
    'vpn.wireguard.hub': field('WireGuard hub', 'The always-on Tutti host. Empty: the dashboard\'s computer.', 'select',
                               choices_from='machines'),
    'vpn.wireguard.endpoint': field('Hub\'s public name', 'For use away from home. Empty: its LAN address.', 'text'),
}
# a Termux machine's policy fields, like a device's
FIELDS.update({f'machines.*.{k}': v for k, v in POLICY.items()})
# the entry defaults that are not in config.DEFAULTS
ENTRY_DEFAULTS = {'machines': config.MACHINE_DEFAULTS}
