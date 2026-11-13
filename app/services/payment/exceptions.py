"""
Provider-neutral exceptions; live_stripe_provider.py maps the SDK's errors onto them.
"""


class PaymentProviderError(Exception):
    """Base class for all payment-provider errors (mirrors stripe.StripeError)."""

    def __init__(self, message: str, *, code: str | None = None):
        self.message = message
        self.code = code
        super().__init__(message)


class CardError(PaymentProviderError):
    """
    A payment was declined by the card issuer/network.
    Mirrors stripe.CardError (decline_code, param, etc).
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "card_declined",
        decline_code: str | None = None,
    ):
        super().__init__(message, code=code)
        self.decline_code = decline_code


class SignatureVerificationError(PaymentProviderError):
    """
    Raised by construct_webhook_event() when the Stripe-Signature header is
    missing, malformed, or does not match the computed HMAC for the payload.
    Mirrors stripe.SignatureVerificationError.
    """

    def __init__(self, message: str, sig_header: str | None = None):
        super().__init__(message, code="signature_verification_failed")
        self.sig_header = sig_header


class IdempotencyError(PaymentProviderError):
    """
    Raised when an idempotency key is reused with materially different
    request parameters - mirrors stripe.IdempotencyError. Reusing the
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
    stripe.InvalidRequestError. Distinct from CardError: a CardError
    means the provider processed the request and the card was declined; an
    InvalidRequestError means the request itself was malformed and nothing
    was ever "sent to the processor" (no PaymentIntent is created/mutated).
    """

    def __init__(
        self,
        message: str,
        *,
        param: str | None = None,
        code: str = "parameter_invalid",
    ):
        super().__init__(message, code=code)
        self.param = param


class PaymentProviderTimeoutError(PaymentProviderError):
    """
    The provider could not be reached / did not respond in time.
    Mirrors stripe.APIConnectionError. This is a transport-level
    failure, distinct from a CardError (which means the provider *did*
    respond, with a decline).

    The live provider fills in what Stripe said about the failure (the SDK
    error's class, its Request-Id, HTTP status and error code) so the route
    can log the cause; each is None when not known.
    """

    def __init__(
        self,
        message: str = "Could not connect to payment provider",
        *,
        error_class: str | None = None,
        request_id: str | None = None,
        http_status: int | None = None,
        provider_code: str | None = None,
    ):
        super().__init__(message, code="api_connection_error")
        self.error_class = error_class
        self.request_id = request_id
        self.http_status = http_status
        self.provider_code = provider_code
