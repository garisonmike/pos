"""
A stand-in for Daraja, and the payloads Safaricom really sends.

A callback needs a publicly reachable URL and `localhost` is not one, so without
this the whole path - success, refusal, a replayed callback, a callback for a
sale that has since been voided - could only be exercised by hand against a
tunnel. Driving it through a fake means the logic is proved by the suite, and
the sandbox is then used to confirm the wire format rather than to discover the
behaviour.

The payload shapes below are Safaricom's, including the parts that look odd:
metadata as a list of name/value pairs, the amount as a float, and the phone
number as a number rather than a string.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from apps.payments.daraja import DarajaError, StkPushResult, StkQueryResult


@dataclass
class FakeDaraja:
    """A Daraja that answers however a test needs it to."""

    credential: object = None

    #: What the next push should do.
    should_fail: bool = False
    failure_detail: str = "M-Pesa refused the request."
    checkout_request_id: str = "ws_CO_14082026110233001"
    merchant_request_id: str = "29115-34620561-1"

    #: What a later query should say happened.
    query_result_code: int = 0
    query_description: str = "The service request is processed successfully."
    query_raw: dict = field(default_factory=dict)

    pushes: list = field(default_factory=list)

    def stk_push(self, *, amount_cents, phone, reference, description, callback_url):
        self.pushes.append(
            {
                "amount_cents": amount_cents,
                "phone": phone,
                "reference": reference,
                "callback_url": callback_url,
            }
        )
        if self.should_fail:
            raise DarajaError(self.failure_detail, "daraja_refused")

        return StkPushResult(
            checkout_request_id=self.checkout_request_id,
            merchant_request_id=self.merchant_request_id,
            customer_message="Success. Request accepted for processing",
            raw={},
        )

    def stk_query(self, *, checkout_request_id):
        return StkQueryResult(
            result_code=self.query_result_code,
            result_description=self.query_description,
            raw=self.query_raw,
        )


def success_callback(
    *,
    checkout_request_id: str = "ws_CO_14082026110233001",
    merchant_request_id: str = "29115-34620561-1",
    receipt: str = "QK12ABC34D",
    amount_shillings: float = 180.0,
    phone: int = 254712345678,
) -> dict:
    """What Safaricom posts when a customer pays.

    The amount really does arrive as a float, which is why it is converted to
    integer cents the moment it crosses into this system and never handled as a
    float again.
    """
    return {
        "Body": {
            "stkCallback": {
                "MerchantRequestID": merchant_request_id,
                "CheckoutRequestID": checkout_request_id,
                "ResultCode": 0,
                "ResultDesc": "The service request is processed successfully.",
                "CallbackMetadata": {
                    "Item": [
                        {"Name": "Amount", "Value": amount_shillings},
                        {"Name": "MpesaReceiptNumber", "Value": receipt},
                        {"Name": "TransactionDate", "Value": 20260814110245},
                        {"Name": "PhoneNumber", "Value": phone},
                    ]
                },
            }
        }
    }


def failure_callback(
    *,
    checkout_request_id: str = "ws_CO_14082026110233001",
    result_code: int = 1032,
    description: str = "Request cancelled by user",
) -> dict:
    """What arrives when the customer declines or lets it time out.

    No metadata block at all - which is why the parser must tolerate its
    absence rather than assuming a successful shape.
    """
    return {
        "Body": {
            "stkCallback": {
                "MerchantRequestID": "29115-34620561-1",
                "CheckoutRequestID": checkout_request_id,
                "ResultCode": result_code,
                "ResultDesc": description,
            }
        }
    }
