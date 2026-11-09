"""LIVE sandbox probes. No raw response, credential or full provider id is printed."""
import os
import uuid
from decimal import Decimal

import pytest

from agentpal.infra_contracts import ProviderStatus
from agentpal.providers.config import load_config_from_env
from agentpal.providers.paypal_sandbox import PayPalSandboxProvider, PayPalOperationError
from agentpal.spending import ApprovalTerms
from test_infra_integration_platform import world  # noqa: F401

pytestmark = pytest.mark.skipif(
    not (os.environ.get('PAYPAL_SANDBOX_CLIENT_ID', '').strip()
         and os.environ.get('PAYPAL_SANDBOX_CLIENT_SECRET', '').strip()),
    reason='LIVE PayPal sandbox credentials absent')

NORTHWIND = 'agentpal.northwind.16742b@business.example.com'


def live_terms(email=NORTHWIND):
    return ApprovalTerms(request_id='pay_' + uuid.uuid4().hex, agent_id='agent_live_p1',
        amount=Decimal('1.00'), currency='USD', merchant_name='Northwind',
        merchant_domain='northwind.example', purpose='Labelled sandbox P1 test',
        merchant_paypal_email=email)


@pytest.fixture
def live_provider():
    provider = PayPalSandboxProvider(load_config_from_env(negative_testing=True))
    yield provider
    provider.http_client.close()


def create(provider, terms):
    return provider.create_order(terms, idempotency_key='p1-create-' + uuid.uuid4().hex)


def test_live_oauth_reuses_token(live_provider):
    token = live_provider.access_token()
    valid_token = isinstance(token, str) and bool(token)
    assert valid_token
    reused = live_provider.access_token() is token
    assert reused
    print('LIVE OAuth token reused; sandbox only')


def test_live_northwind_payee_readback(live_provider):
    terms = live_terms()
    ref = create(live_provider, terms)
    event = live_provider.get_order_status(ref.provider_order_id)
    assert event.verified and event.order_evidence.email == NORTHWIND
    assert event.order_evidence.amount == terms.amount
    assert event.order_evidence.custom_id == terms.request_id
    assert event.order_evidence.reference_id == terms.request_id
    print('LIVE Northwind order ...' + ref.provider_order_id[-4:] + '; payee read verified')


def test_live_unapproved_capture_has_stable_outcome(live_provider):
    ref = create(live_provider, live_terms())
    with pytest.raises(PayPalOperationError) as caught:
        live_provider.capture_order(ref.provider_order_id,
                                    idempotency_key='p1-capture-' + uuid.uuid4().hex)
    assert caught.value.code == 'ORDER_NOT_APPROVED'
    assert not caught.value.outcome_unknown
    assert live_provider.get_order_status(ref.provider_order_id).status != ProviderStatus.completed
    print('LIVE unapproved order ...' + ref.provider_order_id[-4:] + '; order_not_approved')


def test_live_forced_instrument_decline(live_provider):
    ref = create(live_provider, live_terms())
    with pytest.raises(PayPalOperationError) as caught:
        live_provider.capture_order(ref.provider_order_id,
            idempotency_key='p1-capture-' + uuid.uuid4().hex, mock_response='INSTRUMENT_DECLINED')
    assert caught.value.code == 'INSTRUMENT_DECLINED'
    assert not caught.value.outcome_unknown
    assert live_provider.get_order_status(ref.provider_order_id).status != ProviderStatus.completed
    print('LIVE negative order ...' + ref.provider_order_id[-4:] + '; instrument_declined')


def test_live_bogus_payee_never_confirms(live_provider):
    ref = create(live_provider, live_terms('absent-' + uuid.uuid4().hex + '@example.com'))
    event = live_provider.get_order_status(ref.provider_order_id)
    assert event.verified and event.status != ProviderStatus.completed
    try:
        event = live_provider.capture_order(ref.provider_order_id,
            idempotency_key='p1-capture-' + uuid.uuid4().hex)
    except PayPalOperationError:
        pass
    else:
        assert event.status != ProviderStatus.completed
    print('LIVE bogus-payee order ...' + ref.provider_order_id[-4:] + '; never confirmed')


def approve_sandbox_buyer(url):
    """Labelled test buyer, separate from AgentPal owner approval. Never supplies proof."""
    from pathlib import Path
    from urllib.parse import urlsplit
    from playwright.sync_api import sync_playwright
    parts = urlsplit(url)
    assert parts.scheme == 'https' and parts.hostname in ('www.sandbox.paypal.com', 'sandbox.paypal.com')
    proxy_url = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
    proxy = None
    if proxy_url:
        parsed = urlsplit(proxy_url)
        proxy = {'server': f'{parsed.scheme}://{parsed.hostname}:{parsed.port or 443}'}
        if parsed.username:
            proxy.update(username=parsed.username, password=parsed.password or '')
    with sync_playwright() as p:
        options = {'headless': True, 'args': ['--no-sandbox']}
        if Path('/opt/pw-browsers/chromium').exists():
            options['executable_path'] = '/opt/pw-browsers/chromium'
        if proxy:
            options['proxy'] = proxy
        browser = p.chromium.launch(**options)
        try:
            page = browser.new_page()
            page.set_default_timeout(30000)
            stage = "navigation"
            page.goto(url, wait_until='domcontentloaded', timeout=45000)
            stage = 'email'
            page.locator('#email').fill(os.environ['PAYPAL_SANDBOX_BUYER_EMAIL'])
            next_button = page.locator('#btnNext')
            if next_button.is_visible():
                next_button.click()
            stage = 'password'
            page.locator('#password').fill(os.environ['PAYPAL_SANDBOX_BUYER_PASSWORD'])
            stage = 'login'
            page.locator('#btnLogin').click()
            stage = 'buyer_approval'
            review = page.get_by_text('Continue to Review Order', exact=True)
            final = page.locator('#payment-submit-btn, #confirmButtonTop')
            for text in ('Pay Now', 'Pay now', 'Agree & Pay', 'Complete Purchase',
                         'Complete purchase', 'Agree & Continue', 'Agree and Pay'):
                final = final.or_(page.get_by_text(text, exact=True))
            final.or_(review).first.wait_for(state='visible', timeout=30000)
            if review.is_visible():
                review.click()
            stage = 'final_buyer_approval'
            final.first.click(timeout=45000)
            page.wait_for_timeout(1500)
        except Exception:
            raise RuntimeError('sandbox_buyer_harness_' + stage) from None
        finally:
            browser.close()


@pytest.mark.skipif(not (os.environ.get('PAYPAL_SANDBOX_BUYER_EMAIL')
                        and os.environ.get('PAYPAL_SANDBOX_BUYER_PASSWORD')),
                    reason='LIVE sandbox buyer harness credentials absent')
def test_live_decline_through_real_owner_approval_and_hold_release(world):  # noqa: F811
    from test_infra_integration_platform import bearer, approve, execute
    from agentpal.infra_contracts import PaymentLifecycle as L

    class NegativeCaptureProvider(PayPalSandboxProvider):
        def build_order_body(self, terms, return_url=None):
            value = super().build_order_body(terms, return_url or 'https://www.sandbox.paypal.com/')
            value['payment_source']['paypal']['experience_context']['user_action'] = 'PAY_NOW'
            return value

        def capture_order(self, provider_order_id, *, idempotency_key):
            return super().capture_order(provider_order_id, idempotency_key=idempotency_key,
                                         mock_response='INSTRUMENT_DECLINED')

    provider = NegativeCaptureProvider(load_config_from_env(negative_testing=True))
    world[1].provider = world[1].lifecycle.provider = provider
    try:
        response = world[0].post('/v1/payment-requests', headers={**bearer(world[4]),
            'Idempotency-Key': 'p1-live-decline'}, json={'amount': '1.00', 'currency': 'USD',
            'merchant_name': 'Northwind', 'merchant_paypal_email': NORTHWIND,
            'purpose': 'LIVE labelled sandbox negative testing'})
        assert response.status_code == 201
        payment = response.json()
        approve(world, payment)
        assert execute(world, payment).json()['status'] == 'provider_pending'
        order = world[1].risk_service.store.provider_order(payment['request_id'])
        url = world[1].risk_service.store.provider_payer_url(payment['request_id'], agent_id=world[5])
        approve_sandbox_buyer(url)
        response = world[0].post('/v1/payment-requests/' + payment['request_id'] + '/capture',
                                 headers=bearer(world[2]))
        assert response.json()['status'] == L.failed.value
        ledger = world[1].risk_service.store
        assert ledger.spend_breakdown(world[5], 'USD', ledger.clock())['committed'] == 0
        assert len(ledger.query("SELECT * FROM payment_outcomes WHERE outcome='failed'")) == 1
        print('LIVE owner-approved negative order ...' + order['provider_order_id'][-4:]
              + '; failed, hold released once')
    finally:
        provider.http_client.close()
