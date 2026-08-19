"""
Exception hierarchy mirroring the shape of the real `stripe` Python SDK's
`stripe.error` module (StripeError -> CardError / APIConnectionError / ...).

We deliberately do not depend on the real `stripe` package (see
app/services/payment/README notes in stripe_provider.py) - these are small,
dependency-free stand-ins with the same names/semantics so the rest of the
codebase (and anyone who has used the real SDK) reads naturally.
"""
from typing import Optional


class PaymentProviderError(Exception):
    """Base class for all payment-provider errors (mirrors stripe.error.StripeError)."""

    def __init__(self, message: str, *, code: Optional[str] = None):
        self.message = message
        self.code = code
        super().__init__(message)


class CardError(PaymentProviderError):
    """
    A payment was declined by the card issuer/network.
    Mirrors stripe.error.CardError (decline_code, param, etc).
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "card_declined",
        decline_code: Optional[str] = None,
    ):
        super().__init__(message, code=code)
        self.decline_code = decline_code


class SignatureVerificationError(PaymentProviderError):
    """
    Raised by construct_webhook_event() when the Stripe-Signature header is
    missing, malformed, or does not match the computed HMAC for the payload.
    Mirrors stripe.error.SignatureVerificationError.
    """

    def __init__(self, message: str, sig_header: Optional[str] = None):
        super().__init__(message, code="signature_verification_failed")
        self.sig_header = sig_header


class IdempotencyError(PaymentProviderError):
    """
    Raised when an idempotency key is reused with materially different
    request parameters - mirrors stripe.error.IdempotencyError. Reusing the
    same key with the same parameters is NOT an error; it returns the
    original cached response instead (see StripePaymentProvider).
    """

    def __init__(self, message: str):
        super().__init__(message, code="idempotency_key_in_use")


class InvalidRequestError(PaymentProviderError):
    """
    Raised when a call's parameters are structurally invalid regardless of
    what a real charge outcome would be - e.g. a non-integer/non-positive
    amount, or a currency code that is not a real ISO 4217 code. Mirrors
    stripe.error.InvalidRequestError. Distinct from CardError: a CardError
    means the provider processed the request and the card was declined; an
    InvalidRequestError means the request itself was malformed and nothing
    was ever "sent to the processor" (no PaymentIntent is created/mutated).
    """

    def __init__(
        self,
        message: str,
        *,
        param: Optional[str] = None,
        code: str = "parameter_invalid",
    ):
        super().__init__(message, code=code)
        self.param = param


class PaymentProviderTimeoutError(PaymentProviderError):
    """
    The provider could not be reached / did not respond in time.
    Mirrors stripe.error.APIConnectionError. This is a transport-level
    failure, distinct from a CardError (which means the provider *did*
    respond, with a decline).
    """

    def __init__(self, message: str = "Could not connect to payment provider"):
        super().__init__(message, code="api_connection_error")
