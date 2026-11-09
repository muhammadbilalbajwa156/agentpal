"""Channel3 is untrusted advisory data: no fabricated retailer identity or consent."""
import json
from decimal import Decimal

import httpx
import pytest

from agentpal.intelligence.market_check import DISCLOSURE, MarketChecker
from agentpal.tools.channel3 import Channel3Client


def checker(offers, *, title='Acme Widget 7', brands=None):
    payload = {'products': [{'id': 'h3-widget-7', 'title': title,
        'brands': brands or [{'name': 'Acme'}], 'offers': offers}]}
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return MarketChecker(Channel3Client('h3-offline-catalog-key', client=http)), requests, http


def offer(price='0.50', domain='retailer.example', url='https://retailer.example/widget'):
    return {'domain': domain, 'price': {'price': price, 'currency': 'USD'},
            'condition': 'new', 'availability': 'InStock', 'url': url}


def check(market):
    return market.check(amount=Decimal('10.00'), currency='USD',
                        merchant_domain='merchant.example', product_query='Acme Widget 7')


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'data:text/html,<script>alert(1)</script>',
    'file:///etc/passwd', 'https://user:password@retailer.example/widget',
    'https://retailer.example.evil.example/widget', 'https://127.0.0.1/private',
    'https://retailer.example/\nforged'])
# Fixed by the lead 2026-11-05 (was strict xfail E2E-H3-1).
def test_untrusted_deal_url_is_not_advertised_as_a_verified_retailer(h3_world, url):
    w = h3_world
    market, requests, http = checker([offer(url=url)])
    try:
        w.deps.market_checker = market
        payment = w.prepare(amount='5.00', product_query='Acme Widget 7')
        path = '/v1/payment-requests/' + payment['request_id']
        owner_view = w.client.get(path, headers=w.owner)
        assert owner_view.status_code == 200
        advisory = owner_view.json()['market_check']
        assert owner_view.json()['status'] == 'awaiting_approval'
        assert owner_view.json()['terms_sha256'] == payment['terms_sha256']
        assert advisory['disclosure'] == DISCLOSURE
        assert advisory['advisory_only'] is True and advisory['authoritative'] is False
        assert len(requests) == 1
        # Refuse the offer or remove its actionable URL. API JSON is not executed;
        # this assertion concerns unsafe/misattributed owner-facing buy links.
        assert not any(deal.get('buy_url') == url for deal in advisory['better_deals'])
    finally:
        http.close()


@pytest.mark.parametrize('price', ['-0.01', '1000000000000', '1e100000', 'NaN',
    'Infinity', True, '0.00001'])
def test_invalid_catalog_prices_never_become_deals(price):
    market, requests, http = checker([offer(price=price)])
    try:
        result = check(market)
        assert result['better_deals'] == []
        assert result['market'] is None
        assert result['advisory_level'] != 'no_concern'
        assert result['disclosure'] == DISCLOSURE
        assert len(requests) == 1
    finally:
        http.close()


@pytest.mark.parametrize('title', ['Acme Widget 8', 'Acme Widget 7 Case',
    'Acme Widget 7 Pro', 'Acme Widget 7 replacement tips'])
def test_fake_similar_products_never_claim_same_product_deals(title):
    market, _, http = checker([offer()], title=title)
    try:
        result = check(market)
        assert result['better_deals'] == []
        assert result['market'] is None
        assert result['advisory_level'] == 'no_match'
    finally:
        http.close()


@pytest.mark.parametrize('domain', ['retailer.example.evil.example', 'reta\u0131ler.example'])
def test_catalog_lookalike_never_becomes_the_claimed_merchant(domain):
    market, _, http = checker([offer(domain=domain, url='https://' + domain + '/widget')])
    try:
        result = market.check(amount=Decimal('10'), currency='USD',
            merchant_domain='retailer.example', product_query='Acme Widget 7')
        assert result['merchant_offer'] is None
        assert 'MERCHANT_NOT_IN_MARKET' in {s['code'] for s in result['signals']}
        assert 'verify' in result['verify_notice'].lower() or 'check' in result['verify_notice'].lower()
    finally:
        http.close()


def test_disclosure_is_locally_supplied_even_if_upstream_omits_it():
    market, _, http = checker([offer()])
    try:
        result = check(market)
        assert len(result['better_deals']) == 1
        assert result['disclosure'] == DISCLOSURE
        assert result['advisory_only'] and not result['authoritative']
        assert 'new owner approval' in result['switch_notice']
        assert 'discount' in result['discount_codes_note']
        json.dumps(result, allow_nan=False)
    finally:
        http.close()
