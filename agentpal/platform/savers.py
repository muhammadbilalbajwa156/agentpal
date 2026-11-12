"""Deterministic, advisory-only Savers. No payment/provider authority in this module.

All scans use stored offers. Closed UTC months are evaluated once; retirement suspends
an identity and revokes every key atomically, preserving financial and policy history.
"""
from __future__ import annotations

import asyncio
import calendar
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from starlette.concurrency import run_in_threadpool

from ..agents.common import GROQ_MODELS
from ..wallet.models import AgentStatus
from ..workforce.roles import Role
from .credentials import _event
from .errors import PlatformError, not_found
from .models import CounterOffer, contains_credential
from .payments import body_digest

MANIFESTO = 'I exist to save you money. If I save you nothing this month, I retire myself.'
TEMPLATE_NOTE = 'I look for cheaper stored offers; you decide whether to counter.'
log = logging.getLogger('agentpal.savers')


def iso(at):
    return at.astimezone(timezone.utc).isoformat()


def month(at):
    return at.astimezone(timezone.utc).strftime('%Y-%m')


def next_month(period):
    year, number = map(int, period.split('-'))
    return f'{year + (number == 12):04d}-{number % 12 + 1:02d}'


def totals(items):
    out = {}
    for currency, amount in items:
        out[currency] = out.get(currency, Decimal(0)) + amount
    return {currency: format(amount, f'.{max(2, -amount.as_tuple().exponent)}f')
            for currency, amount in sorted(out.items()) if amount > 0}


class NoteWriter:
    """Optional one-line decoration at hire only, using the workspace Groq integration.

    No financial data or credentials enter the prompt. No new provider calls on a tick.
    Any failure falls back to a labelled template; the note never enters policy decisions.
    """
    transport_factory = staticmethod(lambda: None)

    @classmethod
    def write(cls, integrations, owner_id):
        try:
            fields = integrations.fields(owner_id, 'groq') if integrations else None
            if fields:
                with httpx.Client(timeout=5, follow_redirects=False,
                                  transport=cls.transport_factory()) as client:
                    with client.stream('POST', 'https://api.groq.com/openai/v1/chat/completions',
                            headers={'Authorization':'Bearer ' + fields['api_key']},
                            json={'model':GROQ_MODELS[0], 'max_tokens':80,
                                  'messages':[{'role':'user', 'content':
                                      'Write one short welcome sentence for a sandbox savings '
                                      'assistant. It only suggests stored cheaper offers; the '
                                      'owner decides. Never claim savings, training or authority.'}]}) as response:
                        response.raise_for_status()
                        chunks, size = [], 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > 16384:
                                raise ValueError('note_too_large')
                            chunks.append(chunk)
                        note = json.loads(b''.join(chunks))['choices'][0]['message']['content']
                        if (isinstance(note, str) and note.strip()
                                and fields['api_key'] not in note and not contains_credential(note)):
                            return ' '.join(note.split())[:240], 'ai'
        except Exception:
            pass  # decoration is optional; never log remote text or credentials
        return TEMPLATE_NOTE, 'template'


class SaverService:
    def __init__(self, platform):
        self.p = platform
        self.store = platform.deps.wallet_store
        self.clock = platform.clock

    def owned(self, owner_id, saver_id):
        rows = self.store.query('SELECT * FROM platform_savers WHERE saver_id=? AND owner_id=?',
                                (saver_id, owner_id))
        if not rows:
            raise not_found()
        return rows[0]

    def hire(self, owner):
        if self.store.query("SELECT 1 FROM platform_savers WHERE owner_id=? AND status='active'",
                            (owner.owner_id,)):
            raise PlatformError(409, 'saver_exists')
        note, source = NoteWriter.write(self.p.deps.integrations, owner.owner_id)
        with self.store.tx() as c:
            if c.execute("SELECT 1 FROM platform_savers WHERE owner_id=? AND status='active'",
                         (owner.owner_id,)).fetchone():
                raise PlatformError(409, 'saver_exists')
            # Finance has NO payment capabilities in its ceiling; explicit empty grants.
            agent, _ = self.p.agents.register(owner, 'Saver', MANIFESTO, Role.finance, [],
                                               {'kind':'saver'})
            saver_id = 'sav_' + uuid.uuid4().hex
            c.execute('INSERT INTO platform_savers (saver_id,owner_id,agent_id,status,created_at,'
                      'note,note_source) VALUES (?,?,?,\'active\',?,?,?)',
                      (saver_id, owner.owner_id, agent['agent_id'], iso(self.clock()), note, source))
            self.event(c, saver_id, 'hired')
        return self.get(owner.owner_id, saver_id)

    def event(self, c, saver_id, action):
        c.execute('INSERT INTO platform_saver_events (saver_id,action,at) VALUES (?,?,?)',
                  (saver_id, action, iso(self.clock())))

    def list(self, owner_id):
        return [self.get(owner_id, r['saver_id']) for r in self.store.query(
            'SELECT saver_id FROM platform_savers WHERE owner_id=? ORDER BY created_at,saver_id',
            (owner_id,))]

    def feed(self, row):
        rows = self.store.query('SELECT * FROM platform_saver_suggestions WHERE saver_id=? '
                                'AND owner_id=? ORDER BY created_at DESC,suggestion_id',
                                (row['saver_id'], row['owner_id']))
        for suggestion in rows:
            for name in ('body_sha256', 'owner_id', 'agent_id'):
                suggestion.pop(name)
        return rows

    def score(self, row, period):
        suggestions = self.store.query('SELECT * FROM platform_saver_suggestions '
                                       'WHERE saver_id=? AND owner_id=?',
                                       (row['saver_id'], row['owner_id']))
        made = used = 0
        verified, potential = [], []
        for suggestion in suggestions:
            created_period = month(datetime.fromisoformat(suggestion['created_at']))
            made += created_period == period
            links = self.store.query('SELECT * FROM platform_counter_offers WHERE original_request_id=? '
                                      'AND owner_id=? AND agent_id=? AND source=\'channel3_deal\' '
                                      'AND deal_index=? AND body_sha256=? AND replacement_request_id IS NOT NULL',
                                      (suggestion['request_id'], row['owner_id'], suggestion['agent_id'],
                                       suggestion['deal_index'], suggestion['body_sha256']))
            paid = False
            if links:
                link = links[0]
                used += month(datetime.fromisoformat(link['created_at'])) == period
                view = self.p.payments.owner_view(link['replacement_request_id'])
                paid = bool(view.get('counter_saving_verified'))
                if (paid and view['status'] == 'provider_confirmed'
                        and view['verification']['verified_by'] in {'paypal_read','paypal_webhook'}
                        and month(datetime.fromisoformat(
                            self.p.payments.history(link['replacement_request_id']).proofs()[0]['at'])) == period):
                    verified.append((suggestion['currency'], Decimal(view['counter_saving'])))
            if created_period == period and not paid:
                potential.append((suggestion['currency'], Decimal(suggestion['saving'])))
        savings = totals(verified)
        report = ('This month I saved you ' + ', '.join('$' + v + ' ' + k for k, v in savings.items())
                  if savings else 'No PayPal-verified savings this month.')
        return {'period':period, 'suggestions_made':made, 'suggestions_used':used,
                'verified_savings':savings, 'unverified_potential':totals(potential), 'report':report}

    def get(self, owner_id, saver_id):
        row = self.owned(owner_id, saver_id)
        periods = [json.loads(r['report_json']) for r in self.store.query(
            'SELECT report_json FROM platform_saver_periods WHERE saver_id=? ORDER BY period', (saver_id,))]
        now = self.clock().astimezone(timezone.utc)
        return {**row, 'manifesto':MANIFESTO, 'current_period':self.score(row, month(now)),
                'periods':periods, 'feed':self.feed(row),
                'days_left':calendar.monthrange(now.year, now.month)[1] - now.day + 1,
                'automatic_runs': 'Runs automatically while the AgentPal server is running.',
                'automatic_interval_seconds':SaverTicker.interval()}

    def retire_row(self, c, row, reason):
        if row['status'] == 'retired':
            return
        now = self.clock()
        c.execute("UPDATE platform_savers SET status='retired', retired_at=?, retirement_reason=? "
                  'WHERE saver_id=?', (iso(now), reason, row['saver_id']))
        c.execute("UPDATE wallet_agents SET status='suspended' WHERE agent_id=?", (row['agent_id'],))
        for key in c.execute('SELECT key_id FROM platform_agent_keys WHERE agent_id=? AND revoked_at IS NULL',
                             (row['agent_id'],)).fetchall():
            c.execute("UPDATE platform_agent_keys SET revoked_at=?,revoked_reason='saver_retired' WHERE key_id=?",
                      (iso(now), key['key_id']))
            _event(c, row['agent_id'], row['owner_id'], key['key_id'], 'revoke',
                   'saver:' + row['saver_id'], 'saver_retired', now)
        self.event(c, row['saver_id'], reason)

    def retire(self, owner_id, saver_id):
        with self.store.tx() as c:
            self.retire_row(c, self.owned(owner_id, saver_id), 'Retired by owner')
        return self.get(owner_id, saver_id)

    def evaluate(self, owner_id, saver_id):
        with self.store.tx() as c:
            row = self.owned(owner_id, saver_id)
            period = month(datetime.fromisoformat(row['created_at']))
            current = month(self.clock())
            while row['status'] == 'active' and period < current:
                if not c.execute('SELECT 1 FROM platform_saver_periods WHERE saver_id=? AND period=?',
                                 (saver_id, period)).fetchone():
                    score = self.score(row, period)
                    c.execute('INSERT INTO platform_saver_periods VALUES (?,?,?,?)',
                              (saver_id, period, json.dumps(score, sort_keys=True), iso(self.clock())))
                    self.event(c, saver_id, 'evaluated:' + period)
                    if not score['verified_savings']:
                        self.retire_row(c, row, 'Retired: saved $0 in ' + period)
                        break
                period = next_month(period)
        return self.get(owner_id, saver_id)

    def run(self, owner_id, saver_id):
        self.evaluate(owner_id, saver_id)
        # Serialize scans with manual retirement, hiring and concurrent ticks.
        with self.store.tx() as c:
            row = self.owned(owner_id, saver_id)
            if row['status'] != 'active':
                raise PlatformError(409, 'saver_retired')
            if self.p.registry.get_agent(row['agent_id']).status != AgentStatus.active:
                raise PlatformError(409, 'saver_suspended')
            requests = self.store.query('SELECT request_id FROM platform_payment_meta '
                                        'WHERE owner_id=? AND decision_requested=1', (owner_id,))
            for meta in requests:
                request_id = meta['request_id']
                view = self.p.payments.view(request_id)
                if view['status'] != 'awaiting_approval':
                    continue
                check = self.p.payments.market_check(request_id) or {}
                if check.get('provider') != 'channel3':
                    continue
                candidates = []
                deals = check.get('better_deals')
                if not isinstance(deals, list):
                    continue
                for index, deal in enumerate(deals):
                    if not isinstance(deal, dict):
                        continue
                    try:
                        body, _ = self.p.payments._counter_body(request_id, view['agent_id'],
                                                               view['terms'], CounterOffer(deal_index=index))
                        if body.amount < Decimal(str(view['terms']['amount'])):
                            candidates.append((body.amount, index, body))
                    except PlatformError:
                        continue
                if not candidates:
                    continue
                price, index, body = min(candidates, key=lambda x: (x[0], x[1]))
                c.execute('INSERT OR IGNORE INTO platform_saver_suggestions '
                          '(suggestion_id,saver_id,owner_id,request_id,agent_id,deal_index,retailer,price,'
                          'currency,saving,body_sha256,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                          ('sgg_' + uuid.uuid4().hex, saver_id, owner_id, request_id, view['agent_id'], index,
                           body.merchant_name, str(price), body.currency,
                           str(Decimal(str(view['terms']['amount'])) - price), body_digest(body), iso(self.clock())))
            self.event(c, saver_id, 'ran')
        return self.get(owner_id, saver_id)

    def tick(self):
        for row in self.store.query("SELECT saver_id,owner_id FROM platform_savers WHERE status='active'"):
            try:
                self.evaluate(row['owner_id'], row['saver_id'])
                if self.owned(row['owner_id'], row['saver_id'])['status'] == 'active':
                    self.run(row['owner_id'], row['saver_id'])
            except Exception as exc:
                log.error('saver_tick_failed', extra={'error_type':type(exc).__name__})


class SaverTicker:
    def __init__(self, service, interval):
        self.service, self.seconds = service, interval
        self.task = None
        self.stop = asyncio.Event()

    @staticmethod
    def interval(environ=None):
        env = os.environ if environ is None else environ
        try:
            return max(0, int(env.get('AGENTPAL_SAVER_INTERVAL_SECONDS', '900')))
        except (ValueError, TypeError):
            return 900

    async def start(self):
        if self.seconds == 0:
            return
        await run_in_threadpool(self.service.tick)
        self.task = asyncio.create_task(self.loop())

    async def loop(self):
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.seconds)
            except asyncio.TimeoutError:
                await run_in_threadpool(self.service.tick)

    async def close(self):
        self.stop.set()
        if self.task:
            await self.task
