"""
An in-process stand-in for api.stripe.com, for the contract tests in
test_payment_provider_contract.py: it replaces the handful of `stripe` SDK
calls LiveStripePaymentProvider makes, and answers them the way Stripe's
documentation says Stripe does - wire-format objects (lower-case currency,
string statuses), the SDK's own exception classes, idempotency per key and
parameters. The live adapter's translation of all that is then checked by
the same assertions as the mock provider.

It is written from Stripe's documentation, not from the mock provider: the
test PaymentMethods below are transcribed from https://docs.stripe.com/testing
(the "Declined payments", "3D Secure" and "Refunds" tables), the
idempotency rules from https://docs.stripe.com/api/idempotent_requests, the
amount limits from https://docs.stripe.com/currencies, the Refund object
from https://docs.stripe.com/api/refunds/object. Where the documentation
gives an error class but no error code, the code is left out (None), and
the contract tests assert only what is documented.
"""
import copy
import time
import uuid
from typing import Any, Dict, Optional, Tuple

import stripe
from stripe import error as stripe_error

# -- docs.stripe.com/testing --------------------------------------------------

# "Simulate a declined payment": PaymentMethod -> (error code, decline code).
DECLINES: Dict[str, Tuple[str, Optional[str]]] = {
    "pm_card_visa_chargeDeclined": ("card_declined", "generic_decline"),
    "pm_card_visa_chargeDeclinedInsufficientFunds": ("card_declined", "insufficient_funds"),
    "pm_card_visa_chargeDeclinedLostCard": ("card_declined", "lost_card"),
    "pm_card_visa_chargeDeclinedStolenCard": ("card_declined", "stolen_card"),
    "pm_card_chargeDeclinedExpiredCard": ("expired_card", None),
    "pm_card_chargeDeclinedIncorrectCvc": ("incorrect_cvc", None),
    "pm_card_chargeDeclinedProcessingError": ("processing_error", None),
    "pm_card_visa_chargeDeclinedVelocityLimitExceeded": ("card_declined", "card_velocity_exceeded"),
    # "Blocked payments return card errors" (Fraud prevention table). The
    # guide words the code as "fraud", which its error-code list does not
    # have; its decline-code list has "fraudulent". See stripe_provider.py.
    "pm_card_radarBlock": ("card_declined", "fraudulent"),
    # 3D Secure "Required | Error": the 3D Secure lookup fails, the payment
    # is declined with card_declined.
    "pm_card_threeDSecureRequiredProcessingError": ("card_declined", "processing_error"),
}

# 3D Secure "Required" rows and "Always authenticate": confirming asks for
# authentication; once the customer has authenticated, the payment succeeds
# (None) or is declined with the given codes.
AUTHENTICATION_REQUIRED: Dict[str, Optional[Tuple[str, Optional[str]]]] = {
    "pm_card_authenticationRequired": None,
    "pm_card_threeDSecure2Required": None,
    "pm_card_threeDSecureRequiredChargeDeclined": ("card_declined", "generic_decline"),
}

# Cards and the 3D Secure "Supported"/"Not supported" rows: the charge
# succeeds without asking for anything.
SUCCEEDING = (
    "pm_card_visa", "pm_card_visa_debit", "pm_card_mastercard", "pm_card_mastercard_debit",
    "pm_card_mastercard_prepaid", "pm_card_amex", "pm_card_discover", "pm_card_diners",
    "pm_card_jcb", "pm_card_unionpay", "pm_card_us", "pm_card_de", "pm_card_gb",
    "pm_card_fr", "pm_card_pl", "pm_card_hu", "pm_card_jp", "pm_card_tw",
    "pm_card_threeDSecureOptional", "pm_card_amex_threeDSecureNotSupported",
    "pm_card_pendingRefund", "pm_card_refundFail",
)

# "Asynchronous refund" rows: the refund's first status, then the status it
# moves to "some time later" and the event Stripe sends for that.
ASYNC_REFUNDS = {
    "pm_card_pendingRefund": ("pending", "succeeded", "refund.updated"),
    "pm_card_refundFail": ("succeeded", "failed", "refund.failed"),
}

# -- docs.stripe.com/currencies -------------------------------------------------

# "Minimum charge amount by currency", in minor units, for the currencies
# the tests use. A charge below it is refused.
MINIMUM_CHARGE = {"usd": 50, "eur": 50, "gbp": 30, "pln": 200, "huf": 17500}
# "Card payments": up to 12 digits.
MAXIMUM_CARD_CHARGE = 999_999_999_999
# The currencies the tests charge in; all are on Stripe's presentment list.
CURRENCIES = {"usd", "eur", "gbp", "pln", "huf", "twd", "jpy"}


class _Obj:
    """What the adapter sees of a stripe.StripeObject: it only calls to_dict()."""

    def __init__(self, values: Dict[str, Any]):
        self._values = copy.deepcopy(values)

    def to_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self._values)


def _invalid(message, param=None, code=None):
    return stripe_error.InvalidRequestError(message, param, code=code)


def _card_error(code, decline_code, message="Your card was declined."):
    return stripe_error.CardError(
        message, None, code,
        json_body={"error": {"type": "card_error", "message": message, "code": code,
                             "decline_code": decline_code}},
    )


class FakeStripeAPI:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.offline = False
        self.intents: Dict[str, Dict[str, Any]] = {}
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.refunds: Dict[str, Dict[str, Any]] = {}
        # idempotency key -> (endpoint, parameters, ("ok", response) | ("error", exception))
        self.saved: Dict[str, Tuple[str, Dict[str, Any], Tuple[str, Any]]] = {}

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(stripe.PaymentIntent, "create", self.payment_intent_create)
        monkeypatch.setattr(stripe.PaymentIntent, "confirm", self.payment_intent_confirm)
        monkeypatch.setattr(stripe.PaymentIntent, "retrieve", self.payment_intent_retrieve)
        monkeypatch.setattr(stripe.Refund, "create", self.refund_create)
        monkeypatch.setattr(stripe.checkout.Session, "create", self.session_create)
        monkeypatch.setattr(stripe.checkout.Session, "retrieve", self.session_retrieve)
        monkeypatch.setattr(stripe.checkout.Session, "expire", self.session_expire)

    # -- plumbing ------------------------------------------------------------

    def _request(self, params: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
        if self.offline:
            raise stripe_error.APIConnectionError("Could not connect to Stripe")
        assert params.pop("api_key") == self.api_key, "every call carries the provider's key"
        return params.pop("idempotency_key", None), params

    def _idempotent(self, endpoint, key, params, execute):
        """Run `execute` once per idempotency key: a retry with the same
        parameters gets the saved response back, error included; the same key
        with other parameters, or on another endpoint, is an IdempotencyError.
        Only called once the parameters passed validation: a request refused
        at validation saves nothing (docs: "we don't save the idempotent
        result because no API endpoint initiates the execution")."""
        if key is not None and key in self.saved:
            saved_endpoint, saved_params, (kind, outcome) = self.saved[key]
            if (saved_endpoint, saved_params) != (endpoint, params):
                raise stripe_error.IdempotencyError(
                    "Keys for idempotent requests can only be used with the same "
                    "parameters they were first used with."
                )
            if kind == "error":
                raise outcome
            return _Obj(outcome)
        try:
            response = execute()
        except stripe_error.StripeError as error:
            if key is not None:
                self.saved[key] = (endpoint, params, ("error", error))
            raise
        if key is not None:
            self.saved[key] = (endpoint, params, ("ok", copy.deepcopy(response)))
        return _Obj(response)

    @staticmethod
    def _require_amount(amount, param="amount"):
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise _invalid("Invalid positive integer", param, "parameter_invalid_integer")

    def _require_chargeable(self, amount, currency, param="amount"):
        self._require_amount(amount, param)
        if not isinstance(currency, str) or currency.lower() not in CURRENCIES:
            raise _invalid(f"Invalid currency: {currency}", "currency")
        if amount > MAXIMUM_CARD_CHARGE:
            raise _invalid("Amount must be no more than the maximum", param, "amount_too_large")
        if amount < MINIMUM_CHARGE.get(currency.lower(), 1):
            raise _invalid("Amount must be at least the minimum", param, "amount_too_small")

    def _intent(self, intent_id):
        if intent_id not in self.intents:
            raise _invalid(f"No such payment_intent: '{intent_id}'", "intent", "resource_missing")
        return self.intents[intent_id]

    def _session(self, session_id):
        if session_id not in self.sessions:
            raise _invalid(f"No such checkout.session: '{session_id}'", "session",
                           "resource_missing")
        return self.sessions[session_id]

    # -- PaymentIntent -----------------------------------------------------

    def payment_intent_create(self, **params):
        key, params = self._request(params)
        assert params.pop("allowed_payment_method_types") == ["card"]
        self._require_chargeable(params.get("amount"), params.get("currency"))

        def execute():
            intent_id = f"pi_{uuid.uuid4().hex[:24]}"
            intent = {
                "id": intent_id, "object": "payment_intent",
                "amount": params["amount"], "amount_received": 0,
                "currency": params["currency"].lower(),
                "status": ("requires_confirmation" if params.get("payment_method")
                           else "requires_payment_method"),
                "client_secret": f"{intent_id}_secret_{uuid.uuid4().hex[:24]}",
                "payment_method": params.get("payment_method"),
                "metadata": dict(params.get("metadata") or {}),
                "last_payment_error": None, "next_action": None,
            }
            self.intents[intent_id] = intent
            return intent

        return self._idempotent("payment_intents.create", key, params, execute)

    def payment_intent_confirm(self, intent_id, **params):
        key, params = self._request(params)

        def execute():
            intent = self._intent(intent_id)
            if intent["status"] not in ("requires_confirmation", "requires_payment_method",
                                        "requires_action"):
                raise _invalid(
                    "This PaymentIntent's status does not allow it to be confirmed",
                    None, "payment_intent_unexpected_state",
                )
            if intent["status"] == "requires_action":
                # The customer has not authenticated yet: still waiting.
                return intent
            token = intent["payment_method"]
            if token is None:
                raise _invalid("You cannot confirm this PaymentIntent because it's "
                               "missing a payment method", None,
                               "payment_intent_unexpected_state")
            if token in DECLINES:
                self._decline(intent, *DECLINES[token])
            if token in AUTHENTICATION_REQUIRED:
                intent["status"] = "requires_action"
                intent["next_action"] = {
                    "type": "redirect_to_url",
                    "redirect_to_url": {
                        "url": f"https://hooks.stripe.test/authenticate/{intent['id']}",
                        "return_url": params["return_url"],
                    },
                }
                return intent
            self._succeed(intent)
            return intent

        return self._idempotent(f"payment_intents.confirm:{intent_id}", key, params, execute)

    def payment_intent_retrieve(self, intent_id, **params):
        self._request(params)
        return _Obj(self._intent(intent_id))

    def authenticate(self, intent_id) -> None:
        """The customer completes 3D Secure on the page next_action sent
        them to; Stripe then finishes the payment on its own."""
        intent = self.intents[intent_id]
        assert intent["status"] == "requires_action"
        intent["next_action"] = None
        outcome = AUTHENTICATION_REQUIRED[intent["payment_method"]]
        if outcome is None:
            self._succeed(intent)
        else:
            try:
                self._decline(intent, *outcome)
            except stripe_error.CardError:
                pass  # reported to the integration by webhook, not by a call

    @staticmethod
    def _succeed(intent):
        intent["status"] = "succeeded"
        intent["amount_received"] = intent["amount"]
        intent["amount_refunded"] = 0

    @staticmethod
    def _decline(intent, code, decline_code):
        intent["status"] = "requires_payment_method"
        intent["last_payment_error"] = {"type": "card_error", "code": code,
                                        "decline_code": decline_code}
        raise _card_error(code, decline_code)

    # -- Refund --------------------------------------------------------------

    def refund_create(self, **params):
        key, params = self._request(params)
        if "amount" in params:
            self._require_amount(params["amount"])

        def execute():
            intent = self._intent(params["payment_intent"])
            if intent["status"] != "succeeded":
                raise _invalid("This PaymentIntent does not have a successful charge to refund.")
            remaining = intent["amount_received"] - intent["amount_refunded"]
            if remaining == 0:
                raise _invalid("Charge has already been refunded.", None,
                               "charge_already_refunded")
            amount = params.get("amount", remaining)
            if amount > remaining:
                raise _invalid("Refund amount is greater than the unrefunded amount.", "amount")
            first_status = ASYNC_REFUNDS.get(intent["payment_method"], ("succeeded",))[0]
            intent["amount_refunded"] += amount
            refund = {
                "id": f"re_{uuid.uuid4().hex[:24]}", "object": "refund", "amount": amount,
                "currency": intent["currency"], "payment_intent": intent["id"],
                "status": first_status, "failure_reason": None,
                "created": int(time.time()), "metadata": {},
            }
            self.refunds[refund["id"]] = refund
            return refund

        return self._idempotent("refunds.create", key, params, execute)

    def settle_refund(self, refund_id) -> Dict[str, Any]:
        """'Some time later': the asynchronous refund reaches its final
        status, and the event Stripe sends for it is returned."""
        refund = self.refunds[refund_id]
        intent = self.intents[refund["payment_intent"]]
        _, final_status, event_type = ASYNC_REFUNDS[intent["payment_method"]]
        refund["status"] = final_status
        if final_status == "failed":
            refund["failure_reason"] = "unknown"
            intent["amount_refunded"] -= refund["amount"]
        return {"id": f"evt_{uuid.uuid4().hex[:24]}", "object": "event", "type": event_type,
                "created": int(time.time()), "data": {"object": copy.deepcopy(refund)}}

    # -- Checkout Session ------------------------------------------------------

    def session_create(self, **params):
        key, params = self._request(params)
        for url_param in ("success_url", "cancel_url"):
            if not params.get(url_param):
                raise _invalid(f"Missing required param: {url_param}.", url_param,
                               "parameter_missing")
        (line,) = params["line_items"]
        price = line["price_data"]
        self._require_chargeable(price["unit_amount"], price["currency"],
                                 "line_items[0][price_data][unit_amount]")

        def execute():
            session_id = f"cs_test_{uuid.uuid4().hex}"
            session = {
                "id": session_id, "object": "checkout.session",
                "url": f"https://checkout.stripe.test/c/pay/{session_id}",
                "status": "open", "payment_status": "unpaid",
                "amount_total": price["unit_amount"] * line["quantity"],
                "currency": price["currency"].lower(), "payment_intent": None,
                "metadata": dict(params.get("metadata") or {}),
            }
            self.sessions[session_id] = session
            return session

        return self._idempotent("checkout.sessions.create", key, params, execute)

    def session_retrieve(self, session_id, **params):
        self._request(params)
        return _Obj(self._session(session_id))

    def session_expire(self, session_id, **params):
        self._request(params)
        session = self._session(session_id)
        if session["status"] != "open":
            raise _invalid("Only Checkout Sessions with a status of open can be expired.")
        session["status"] = "expired"
        return _Obj(session)
