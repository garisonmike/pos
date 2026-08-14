"""
Talking to Safaricom's Daraja API.

Behind a small interface with a fake alongside it, for a practical reason: a
callback needs a publicly reachable URL, and `localhost` is not one. Driving the
whole path through a fake means every branch - success, refusal, timeout, a
replayed callback, a callback for a voided sale - is exercised by the test suite
without a tunnel, and the sandbox is then used to confirm the wire format rather
than to discover the logic.

Amounts cross this boundary in **whole shillings**, because that is what Daraja
accepts. Everything inside this system is integer cents, so the conversion
happens here and only here, and refuses anything that is not a whole shilling
rather than rounding silently - a rounded payment request is a customer charged
a different amount from the one on their receipt.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime

import requests
from django.conf import settings


class DarajaError(Exception):
    """Daraja refused, or could not be reached."""

    def __init__(self, detail: str, code: str = "daraja_error", payload: dict | None = None):
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.payload = payload or {}


@dataclass(frozen=True)
class StkPushResult:
    """What Daraja says when it accepts a push."""

    checkout_request_id: str
    merchant_request_id: str
    customer_message: str
    raw: dict


@dataclass(frozen=True)
class StkQueryResult:
    """What Daraja says when asked how a push ended."""

    result_code: int
    result_description: str
    raw: dict

    @property
    def succeeded(self) -> bool:
        return self.result_code == 0

    @property
    def still_pending(self) -> bool:
        """1032 is 'request cancelled by user'; 1037 is a timeout with no answer.

        Safaricom also returns 500.001.1001 while a transaction is still being
        processed, which is the case that must not be read as a failure - doing
        so would let the reconciliation job mark a sale unpaid while the money
        was still on its way.
        """
        return str(self.raw.get("errorCode", "")) == "500.001.1001"


def to_shillings(amount_cents: int) -> int:
    """Convert cents to the whole shillings Daraja expects.

    Refuses a fractional shilling rather than rounding. Rounding here would ask
    the customer for an amount different from the one printed on their receipt,
    and the difference would then have nowhere to go in the ledger.
    """
    if amount_cents % 100 != 0:
        raise DarajaError(
            f"M-Pesa takes whole shillings, and {amount_cents} cents is not one. "
            "Round the sale to the shilling first.",
            "fractional_amount",
        )
    return amount_cents // 100


def normalise_phone(phone: str) -> str:
    """Put a Kenyan number into the 2547XXXXXXXX form Daraja wants.

    People write their number every way there is - 0712..., +254712...,
    254712..., 712... - and a till should accept all of them rather than making
    a cashier reformat while a customer waits.
    """
    digits = "".join(character for character in phone if character.isdigit())

    if digits.startswith("254"):
        normalised = digits
    elif digits.startswith("0"):
        normalised = "254" + digits[1:]
    elif len(digits) == 9:
        normalised = "254" + digits
    else:
        normalised = digits

    if len(normalised) != 12 or not normalised.startswith("254"):
        raise DarajaError(f"{phone!r} is not a Kenyan mobile number.", "bad_phone")
    return normalised


class DarajaClient:
    """The real thing. One instance per credential."""

    def __init__(self, credential, timeout: int = 20):
        self.credential = credential
        self.timeout = timeout

    def _access_token(self) -> str:
        response = requests.get(
            f"{self.credential.base_url}/oauth/v1/generate?grant_type=client_credentials",
            auth=(self.credential.consumer_key, self.credential.consumer_secret),
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise DarajaError(
                "Could not authenticate with M-Pesa. Check this business's "
                "credentials under payment settings.",
                "daraja_auth_failed",
            )
        return response.json()["access_token"]

    def _password(self, timestamp: str) -> str:
        raw = f"{self.credential.shortcode}{self.credential.passkey}{timestamp}"
        return base64.b64encode(raw.encode()).decode()

    def stk_push(
        self, *, amount_cents: int, phone: str, reference: str, description: str, callback_url: str
    ) -> StkPushResult:
        """Ask Safaricom to prompt a customer's phone."""
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

        payload = {
            "BusinessShortCode": self.credential.shortcode,
            "Password": self._password(timestamp),
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": to_shillings(amount_cents),
            "PartyA": normalise_phone(phone),
            "PartyB": self.credential.shortcode,
            "PhoneNumber": normalise_phone(phone),
            "CallBackURL": callback_url,
            "AccountReference": reference[:12],
            "TransactionDesc": description[:20],
        }

        try:
            response = requests.post(
                f"{self.credential.base_url}/mpesa/stkpush/v1/processrequest",
                json=payload,
                headers={"Authorization": f"Bearer {self._access_token()}"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise DarajaError(
                "Could not reach M-Pesa. Take cash, or try again in a moment.",
                "daraja_unreachable",
            ) from exc

        body = response.json() if response.content else {}
        if response.status_code != 200 or "CheckoutRequestID" not in body:
            raise DarajaError(
                body.get("errorMessage", "M-Pesa refused the request."),
                "daraja_refused",
                body,
            )

        return StkPushResult(
            checkout_request_id=body["CheckoutRequestID"],
            merchant_request_id=body.get("MerchantRequestID", ""),
            customer_message=body.get("CustomerMessage", ""),
            raw=body,
        )

    def stk_query(self, *, checkout_request_id: str) -> StkQueryResult:
        """Ask what happened to a push whose callback never arrived."""
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        payload = {
            "BusinessShortCode": self.credential.shortcode,
            "Password": self._password(timestamp),
            "Timestamp": timestamp,
            "CheckoutRequestID": checkout_request_id,
        }

        try:
            response = requests.post(
                f"{self.credential.base_url}/mpesa/stkpushquery/v1/query",
                json=payload,
                headers={"Authorization": f"Bearer {self._access_token()}"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise DarajaError("Could not reach M-Pesa.", "daraja_unreachable") from exc

        body = response.json() if response.content else {}
        return StkQueryResult(
            result_code=int(body.get("ResultCode", -1)),
            result_description=body.get("ResultDesc", ""),
            raw=body,
        )


def get_client(credential):
    """Build a client, or the fake when one is installed.

    The fake is chosen by a setting rather than by inspecting the environment,
    so a test can swap it without pretending to be a different deployment.
    """
    factory = getattr(settings, "DARAJA_CLIENT_FACTORY", None)
    if factory is not None:
        return factory(credential)
    return DarajaClient(credential)


def parse_callback(payload: dict) -> dict:
    """Flatten Safaricom's callback into the handful of fields that matter.

    Their shape nests the interesting values inside a list of name/value pairs,
    so this pulls them out once rather than at every use. Anything missing comes
    back as None rather than raising: a malformed callback still needs recording
    as suspect, and an exception here would lose it entirely.
    """
    body = payload.get("Body", {}).get("stkCallback", {})
    items = {
        entry.get("Name"): entry.get("Value")
        for entry in body.get("CallbackMetadata", {}).get("Item", [])
    }

    amount = items.get("Amount")
    amount_cents = int(round(float(amount) * 100)) if amount is not None else None

    return {
        "checkout_request_id": body.get("CheckoutRequestID", ""),
        "merchant_request_id": body.get("MerchantRequestID", ""),
        "result_code": body.get("ResultCode"),
        "result_description": body.get("ResultDesc", ""),
        "mpesa_receipt_number": items.get("MpesaReceiptNumber", "") or "",
        "amount_cents": amount_cents,
        "phone": str(items.get("PhoneNumber", "") or ""),
    }
