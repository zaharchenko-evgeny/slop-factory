"""Read-only snapshots of a configured factory. No gate execution or write path is used."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import config as config_mod
import derive
import gate
import nodes as nodes_mod
import scopes
from factory_plugins.local.surface import plan_local, poll
from gate_port import split_ticket, ticket_from_ref
from ports.system import Query, Subject
from .graph import project_graph
from .eligibility import project_eligibility


def stamp(now):
    return now.astimezone(timezone.utc).isoformat()


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:20]


def safe_url(value):
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    return value if parsed.scheme in ('https', 'http') and parsed.hostname and not parsed.username and not parsed.password else None


def public(value):
    """Redact secret-shaped strings and configured process secrets before serialization."""
    secrets = [v for k, v in os.environ.items() if len(v) >= 8 and
               any(s in k.upper() for s in ('TOKEN', 'SECRET', 'PASSWORD', 'API_KEY'))]
    def clean(item):
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, '[redacted]')
            return derive.redact(item)
        if isinstance(item, list):
            return [clean(x) for x in item]
        if isinstance(item, dict):
            return {k: clean(v) for k, v in item.items()}
        return item
    return clean(value)


def read_gh(args, timeout=None, token=None, cwd=None):
    """Capability boundary: the UI's GitHub adapter can perform reads only."""
    args = [str(x) for x in args]
    allowed = False
    if args[:1] == ['api']:
        method = None
        for i, argument in enumerate(args):
            if argument in ('-X', '--method'):
                method = args[i + 1] if i + 1 < len(args) else 'INVALID'
            elif argument.startswith('--method='):
                method = argument.partition('=')[2]
            elif argument.startswith('-X'):
                method = argument[2:]
        graphql = 'graphql' in args
        query = next((a[6:] for a in args if a.startswith('query=')), '')
        fields = any(a in ('-f', '-F', '--field', '--raw-field', '--input') or
                     a.startswith(('--field=', '--raw-field=', '-f', '-F')) for a in args)
        allowed = method in (None, 'GET') and '--input' not in args and \
            not any(a.startswith('--input=') for a in args) and \
            ((graphql and query.lstrip().startswith(('query', '{')) and not re.search(r'\bmutation\b', query))
             or (not graphql and not fields))
    elif args[:1] in (['pr'], ['issue'], ['repo']):
        allowed = len(args) > 1 and args[1] in ('list', 'view')
    if not allowed:
        raise PermissionError('UI adapter refused a non-read GitHub operation')
    done = subprocess.run([os.environ.get('FACTORY_GH', 'gh'), *args], capture_output=True, text=True,
                          timeout=min(timeout or 30, 30), cwd=cwd,
                          env={**os.environ, 'GH_TOKEN': token} if token else None)
    if done.returncode:
        # Do not forward CLI stderr, which can include credentials or private response bodies.
        raise RuntimeError('GitHub read failed; check gh auth status, repository access, and rate limits')
    return done.stdout


def runners_for(block):
    if scopes.adapter(block) != 'youtrack':
        return {'run': read_gh}
    from factory_plugins.youtrack.store import urllib_runner
    if urlsplit(block.get('url', '')).scheme != 'https':
        raise ValueError('YouTrack token transport requires HTTPS')
    token_name = block.get('token_env', 'YOUTRACK_TOKEN')
    transport = urllib_runner(block['url'], os.environ[token_name])
    cache = {}
    def read(method, path, body=None):
        if method != 'GET' or body is not None:
            raise PermissionError('UI adapter refused a non-read YouTrack operation')
        if path not in cache:
            cache[path] = transport(method, path)
        return cache[path]
    return {'run': read_gh, 'yt_run': read}


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    block: dict
    environments: tuple[str, ...]
    build: object


def sources_for(config):
    """Use exactly the engine's ticket-place and scoped-environment resolution."""
    if config.get('ticket_places'):
        blocks, repos = gate.place_keys(config)
        envs = config.get('environments') or {}
        for key, block in blocks.items():
            environments = []
            for env, settings in envs.items():
                own = gate.run_place(config, env, blocks)
                if own == key or (not settings.get('writable') and
                                  set(scopes.env_repositories(config, env)) & {r.lower() for r in repos[key]}):
                    environments.append(env)
            yield Source(key, key.removeprefix('place:'), block, tuple(environments),
                         lambda key=key, block=block: gate.place_systems(config, key, **runners_for(block)))
    elif scoped := scopes.project_envs(config):
        for env, block in scoped.items():
            yield Source(env, env, block, (env,), lambda env=env, block=block: gate.env_systems(config, env, **runners_for(block)))
    else:
        block = config.get('tracker') or {}
        def build():
            tracker = gate.tracker_of(config, **runners_for(block))
            cls = gate.vcs_class(config, config_mod.vcs(config))
            vcs = cls.from_config(run=read_gh)
            return gate.beside(tracker, vcs)
        yield Source('default', block.get('repository') or block.get('project') or 'Tracker', block,
                     tuple(config.get('environments') or ()), build)


def stage_clock(ticket, stages, stage, now):
    """Latest current-stage entry, never issue updated_at. Reentry resets this clock."""
    reverse = {value: key for key, value in stages.items()}
    history = [{'stage': reverse[e['value']], 'at': e.get('at')}
               for e in ticket.events if e.get('kind') == 'labeled' and e.get('value') in reverse]
    entered = next((e.get('at') for e in reversed(history) if e['stage'] == stage), None)
    parsed = timestamp(entered)
    age = max(0, (now - parsed).total_seconds()) if parsed else None
    return entered if parsed else None, age, history


def ticket_state(record):
    state = getattr(record, 'state', None)
    if isinstance(state, str):
        normalized = state.strip().lower()
        if normalized in ('open', 'closed'):
            return normalized
    return 'unknown'


def attention(ticket, prs, config, graph, facts, url, pr_evidence_complete=True):
    """Review evidence is independent of the generic human-needed marker."""
    for pr, pr_url in prs:
        if pr.state != 'open' or pr.merged:
            continue
        # Requested reviewers are outstanding by definition. Configured bots and bot handles are exempt.
        bots = set((config.get('reviews') or {}).get('trusted_bots') or []) | \
               set((config.get('tracker') or {}).get('bot_logins') or [])
        requested = [p for p in pr.requested_reviewers if p not in bots and not p.endswith('[bot]')]
        if requested:
            return {'kind': 'review', 'reason': 'Outstanding requested human review',
                    'reviewers': requested, 'url': pr_url or url}
    # A human node whose stage and all readable guards hold is an explicit manual gate.
    # An unguarded global resume node is not a pending approval gate.
    for node in graph['nodes'].values():
        when = node.get('when') or {}
        from_stages = {s for outcome in (node.get('outcomes') or {}).values() for s in outcome.get('from') or []}
        guarded = 'stage' in when or (from_stages and facts.get('stage') in from_stages)
        has_merge = any(any('merge' in e for e in outcome.get('effects') or [])
                        for outcome in (node.get('outcomes') or {}).values())
        if not pr_evidence_complete or node.get('role') != 'human' or not guarded or not has_merge:
            continue
        check = {k: v for k, v in when.items() if k != 'actor'}
        try:
            holds = gate.holds(check, facts, {})
        except (KeyError, TypeError, ValueError, gate.Bad):
            holds = False
        if holds and not (prs and all(p.merged or p.state == 'closed' for p, _ in prs)):
            return {'kind': 'review', 'reason': 'Manual approval gate is unresolved',
                    'reviewers': list(ticket.assignees), 'url': url}
    marker = (config.get('tracker') or {}).get('needs_human_label')
    attrs = set(ticket.attrs) | {a for p, _ in prs for a in p.labels}
    if marker and marker in attrs:
        reason = 'Human-needed marker is present; reason is unknown'
        if 'answered' in facts and facts['answered'] is False:
            reason = 'Waiting for answers to factory questions'
        elif ticket.blockers_open:
            reason = 'Waiting for a person to unblock dependencies'
        return {'kind': 'attention', 'reason': reason, 'reviewers': list(ticket.assignees), 'url': url}
    return {'kind': 'none', 'reason': None, 'reviewers': [], 'url': None}


def ticket_url(block, ticket, tracker):
    if scopes.adapter(block) == 'youtrack':
        return safe_url(str(block.get('url', '')).rstrip('/') + '/issue/' + quote(str(ticket), safe=''))
    repo, number = split_ticket(ticket, block.get('repository') or getattr(tracker, 'repo', ''))
    return safe_url(f'https://github.com/{repo}/issues/{number}') if repo else None


def event_ticket(item, tracker):
    payload = item[2]
    issue = payload.get('issue') or {}
    if issue:
        number = issue.get('number')
        repo = (payload.get('repository') or {}).get('full_name')
        return f'{repo.lower()}#{number}' if repo and str(number).isdigit() else str(number)
    pr = payload.get('pull_request') or {}
    number = ticket_from_ref((pr.get('head') or {}).get('ref'))
    if isinstance(number, int):
        repo = (payload.get('repository') or {}).get('full_name') or tracker.repo
        if repo.lower() not in {r.lower() for r in getattr(tracker, 'ticket_repos', [tracker.repo])}:
            repo = tracker.repo
        return f'{repo.lower()}#{number}'
    return str(number or '')


def copy_matches(block, copy):
    own = copy.get('tracker')
    if not own:
        return None
    if scopes.identity(block) != scopes.identity(own):
        return False
    scope = own.get('scope')
    if not scope:
        return True
    if not block.get('scope'):
        return None
    return scope == block.get('scope')


def configure_ui_ci(tracker, config):
    """Set CI checks for the repository that supplies this tracker view's PRs."""
    if not hasattr(tracker, 'ci_checks'):
        return
    urls = {r['name']: '/'.join(r['url'].rstrip('/').split('/')[-2:]).lower()
            for r in config.get('repositories') or []}
    pr_repo = str(getattr(tracker, 'pr_repo', '') or '').lower()
    for settings in (config.get('environments') or {}).values():
        owned = settings.get('writable') or (settings.get('repositories') or [])[:1]
        if pr_repo in {urls.get(name, name).lower() for name in owned}:
            tracker.ci_checks = list((settings.get('ci') or {}).get('checks') or [])
            return


def node_projection(name, node, source, facts, events):
    result = project_eligibility(name, node, facts, events)
    copies = node.get('_ui_copies')
    if copies is None or node.get('role') == 'human':
        return result
    matches = [copy_matches(source.block, copy) for copy in copies]
    if True in matches:
        return result
    status = 'unknown' if None in matches else 'not-matched'
    return {**result, 'trigger': status, 'eligibility': status,
            'reason': 'Environment scope evidence unavailable' if status == 'unknown' else
            'No node copy listens to this ticket source'}


def project_ticket(source, tracker, record, config, graph, events, now):
    stages = config['tracker']['stages']
    state = ticket_state(record)
    if scopes.adapter(source.block) != 'youtrack':
        actual_repo, _ = split_ticket(record.id, source.block.get('repository') or getattr(tracker, 'repo', ''))
        if actual_repo and actual_repo.lower() != str(getattr(tracker, 'repo', '')).lower() and hasattr(tracker, 'at'):
            tracker = tracker.at(actual_repo)
        configure_ui_ci(tracker, config)
        block = {**source.block, 'repository': actual_repo}
        copies = [copy for node in graph['nodes'].values() if node.get('role') != 'human'
                  for copy in node.get('_ui_copies') or [] if copy_matches(block, copy) is True]
        environments = tuple(dict.fromkeys((*source.environments, *(c['env'] for c in copies)))) \
            if any('_ui_copies' in n for n in graph['nodes'].values()) else source.environments
        source = Source(source.id, source.name, block, environments, source.build)
    rules = tracker.rules()
    stage = derive.stage(record, rules.stages)
    entered, age, history = stage_clock(record, stages, stage, now)
    url = ticket_url(source.block, record.id, tracker)
    identity = [scopes.adapter(source.block), str(source.block.get('url') or '').rstrip('/').lower()] if scopes.adapter(source.block) == 'youtrack' else ['github-issues', 'github.com']
    # A qualified GitHub reference and a bare number on that repository are the same ticket.
    repo, number = split_ticket(record.id, source.block.get('repository') or getattr(tracker, 'repo', None))
    key = digest([identity, str(number) if scopes.adapter(source.block) == 'youtrack' else [repo.lower(), str(number)]])
    names = tracker.ticket_repositories(record.id, record.body) if hasattr(tracker, 'ticket_repositories') else list(tracker.repositories)[:1]
    prs, errors = [], []
    for name in names:
        try:
            pr = tracker.read(Subject('pr', record.id, name), derive.pr_want(stage) | {'partial'})
            if pr:
                errors.extend(f'PR {pr.number}: {field} evidence unavailable' for field in pr.unavailable)
                repository = (getattr(tracker, 'repositories', {}) or {}).get(name) or name
                prs.append((pr, safe_url(f'https://github.com/{repository}/pull/{pr.number}')))
        except Exception as exc:
            errors.append(f'PR evidence unavailable ({type(exc).__name__})')
    children = None
    if any('children' in (n.get('when') or {}) for n in graph['nodes'].values()):
        try:
            children = tracker.find(Query('children', of=record.id))
        except Exception:
            pass
    by_name = {p.repo: p for p, _ in prs}
    facts, _ = derive.facts(record, [(name, by_name.get(name)) for name in names] or [('primary', None)], rules, children)
    facts['ticket'] = record.id
    if any(error.startswith('PR evidence unavailable') for error in errors):
        for fact_key in ('pr_state', 'ci', 'head', 'reviews', 'pr_labels', 'behind', 'conflict', 'round_complete', 'round_head', 'fix_rounds', 'focus_missing'):
            facts.pop(fact_key, None)
    for pr, _ in prs:
        for field in pr.unavailable:
            keys = {'ci': ['ci'], 'reviews': ['reviews', 'round_complete', 'round_head', 'fix_rounds', 'focus_missing'],
                    'behind': ['behind'], 'commit_date': ['round_complete', 'round_head', 'fix_rounds'],
                    'events': ['fix_rounds'], 'comments': []}.get(field, [])
            for fact_key in keys:
                facts.pop(fact_key, None)
    facts['_poll_complete'] = events is not None
    event_key = str(record.id) if scopes.adapter(source.block) == 'youtrack' else f'{repo.lower()}#{number}'
    mine = [e for e in events or [] if event_ticket(e, tracker) == event_key]
    projections = [node_projection(name, node, source, facts, mine) for name, node in graph['nodes'].items()]
    result = {'key': key, 'id': str(record.id) if not str(record.id).isdigit() else '#' + str(record.id),
              'title': record.title, 'url': url, 'repository': names[0] if names else (repo or 'Unknown'),
              'environments': list(source.environments), 'stage': stage, 'state': state,
              'stageEnteredAt': entered, 'ageSeconds': age, 'warning': state != 'closed' and age is not None and age > 3600 and
              config['tracker'].get('stage_kinds', {}).get(stage, 'active') == 'active',
              'attention': {'kind': 'none', 'reason': None, 'reviewers': [], 'url': None} if state == 'closed' else
              attention(record, prs, config, graph, facts, url, pr_evidence_complete=not any(e.startswith('PR evidence unavailable') for e in errors)),
              'evidenceErrors': list(errors),
              'prs': [{'number': str(p.number), 'url': link, 'repository': p.repo, 'head': p.head,
                       'state': 'merged' if p.merged else p.state, 'ci': p.ci,
                       'requestedReviewers': list(p.requested_reviewers)} for p, link in prs],
              'blockers': [str(x) for x in record.blockers_open], 'history': history, 'nodes': projections}
    return result, errors


class SnapshotStore:
    """One refresh at a time; successful sources replace their data, failed sources retain theirs."""
    def __init__(self, config=None, graph=None, sources=None, now=None):
        self.config = config if config is not None else config_mod.load()
        self.graph = graph if graph is not None else nodes_mod.load()
        if graph is None:
            import definition
            if definition.present(config_mod.team_root()):
                definition_ = definition.load(config_mod.team_root())
                self.graph = {**self.graph, 'nodes': {name: {**node, '_ui_copies': definition.copies(definition_, name)}
                              for name, node in self.graph['nodes'].items()}}
        self.sources = sorted(list(sources) if sources is not None else list(sources_for(self.config)),
                              key=lambda source: not bool(source.block.get('scope')))
        if graph is None and any('_ui_copies' in n for n in self.graph['nodes'].values()):
            self.sources = [Source(source.id, source.name, source.block, tuple(dict.fromkeys((
                *source.environments,
                *(copy['env'] for node in self.graph['nodes'].values() if node.get('role') != 'human'
                  for copy in node.get('_ui_copies') or [] if copy_matches(source.block, copy) is True)))),
                source.build) for source in self.sources]
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.lock = threading.Lock()
        self.cache = {}
        self.last_success = None
        self.latest = None
        self.factory = {'id': digest(str(config_mod.team_root())),
                        'name': self.config.get('project', {}).get('name') or 'Factory'}
        self.structure = project_graph(self.config, self.graph)

    def read_source(self, source, now, older=False):
        previous = self.cache.get(source.id) or {'tickets': {}, 'updatedAt': None}
        tickets, errors = {}, []
        try:
            tracker, _ = source.build()
            configure_ui_ci(tracker, self.config)
            kinds = self.config['tracker'].get('stage_kinds') or {}
            active = tuple(s for s in self.config['tracker']['stages'] if kinds.get(s) != 'final')
            final = tuple(s for s in self.config['tracker']['stages'] if kinds.get(s) == 'final')
            ids = tracker.find(Query('stage', values=active, closed_hours=None)) if active else []
            if final:
                ids += tracker.find(Query('stage', values=final, closed_hours=None, updated_hours=None if older else 168))
            ids = list(dict.fromkeys(ids))
            if hasattr(tracker, 'prefetch'):
                tracker.prefetch(ids)
            plan = plan_local(self.graph, self.config)
            try:
                events = []
                views = [tracker.at(repo) for repo in tracker.ticket_repos] if scopes.adapter(source.block) != 'youtrack' and hasattr(tracker, 'at') else [tracker]
                for view in views:
                    for event_id, entry, payload in poll(plan, [view]):
                        events.append((event_id, entry, {**payload, 'repository': {'full_name': view.repo}}))
            except Exception as exc:
                events = None
                errors.append(f'Trigger evidence unavailable ({type(exc).__name__})')
            for ticket_id in ids:
                try:
                    record = tracker.read(Subject('ticket', ticket_id, scope=getattr(tracker, 'scope', None)))
                    ticket, failures = project_ticket(source, tracker, record, self.config, self.graph, events, now)
                    if failures and ticket['key'] in previous['tickets']:
                        old_prs = previous['tickets'][ticket['key']]['prs']
                        ticket['prs'] = list({(p['repository'], p['number']): p
                                              for p in [*old_prs, *ticket['prs']]}.values())
                        ticket['evidenceErrors'].append('Previous PR details may be stale')
                    tickets[ticket['key']] = ticket
                    errors.extend(f'Ticket {ticket_id}: {failure}' for failure in failures)
                except Exception as exc:
                    errors.append(f'Ticket {ticket_id}: evidence unavailable ({type(exc).__name__})')
                    for key, old in previous['tickets'].items():
                        if old['id'] in (str(ticket_id), '#' + str(ticket_id)):
                            tickets[key] = old
            updated = stamp(self.now()) if not errors else previous['updatedAt']
        except Exception as exc:
            tickets = previous['tickets']
            updated = previous['updatedAt']
            if isinstance(exc, KeyError) and str(exc).strip("'").endswith(('TOKEN', 'KEY')):
                errors.append(f'Missing credential: {str(exc).strip(chr(39))}. Set it in the server environment and refresh.')
            else:
                errors.append(f'Source unavailable ({type(exc).__name__}). Check server credentials, network and tracker access.')
        return source, tickets, updated, list(dict.fromkeys(errors))

    def snapshot(self, older=False):
        if not self.lock.acquire(blocking=False):
            if self.latest:
                return self.visible({**self.latest, 'stale': True}, older)
            raise BlockingIOError('Initial refresh is in progress')
        try:
            now = self.now()
            statuses, merged = [], {}
            with ThreadPoolExecutor(max_workers=min(4, max(1, len(self.sources)))) as pool:
                results = list(pool.map(lambda s: self.read_source(s, now, older), self.sources))
            for source, tickets, updated, errors in results:
                self.cache[source.id] = {'tickets': tickets, 'updatedAt': updated}
                statuses.append({'id': source.id, 'name': source.name, 'ok': not errors,
                                 'error': '; '.join(errors[:5]) + (f'; {len(errors) - 5} more evidence errors' if len(errors) > 5 else '') or None, 'updatedAt': updated})
                for key, ticket in tickets.items():
                    if key in merged:
                        old = merged[key]
                        ticket = {**old, 'environments': sorted(set(old['environments']) | set(ticket['environments'])),
                                  'prs': old['prs']}
                    merged[key] = ticket
            if any(s['ok'] for s in statuses):
                self.last_success = stamp(self.now())
            self.latest = public({'version': 1, 'factory': self.factory, **self.structure,
                                  'generatedAt': stamp(now), 'lastSuccessAt': self.last_success,
                                  'stale': any(not s['ok'] for s in statuses), 'sources': statuses,
                                  'tickets': list(merged.values())})
            return self.visible(self.latest, older)
        finally:
            self.lock.release()

    def visible(self, snapshot, older):
        if older:
            return snapshot
        final = {s['id'] for s in snapshot['stages'] if s['kind'] == 'final'}
        return {**snapshot, 'tickets': [t for t in snapshot['tickets'] if t['stage'] not in final or
                                      t['ageSeconds'] is None or t['ageSeconds'] <= 7 * 86400]}
