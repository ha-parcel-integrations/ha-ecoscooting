"""Sample Ecoscooting gateway payloads shared by the test modules.

Every value here is invented. The shape mirrors what the gateway really
sends, but references are zero-filled (a real one embeds the destination
postcode), coordinates are zeros and the pickup-point block carries a made-up
shop. Keep the samples in this one module rather than inline in each test —
when the payload shape turns out to differ from what we assumed, there is
then exactly one place to fix.
"""
from __future__ import annotations

ACTIVE_CODE = "CNPRT00000000000000000001"
DELIVERED_CODE = "000000000000000001"
PICKUP_CODE = "000000000000000002"

# The gateway's reply for a reference it cannot resolve — identical for an
# unknown reference and for one whose order is merely unavailable.
QUERY_ERROR = {
    "success": "false",
    "errorCode": "P-011-0201-00-00-502",
    "errorMsg": "query logisticOrder by mailNo error",
}

# A rejected request comes back as XML rather than JSON.
XML_ERROR = (
    "<response><success>false</success><errorCode>S23</errorCode>"
    "<errorMsg>can't find app through fromCode</errorMsg></response>"
)


def event(
    action_code: str,
    op_timestamp: str | None,
    status_name: str,
    *,
    description: str = "",
    timezone_hours: str = "2",
    extra: dict | None = None,
) -> dict:
    """One entry of the gateway's own ``statuses`` list."""
    offset = f"+{timezone_hours}" if not timezone_hours.startswith("-") else timezone_hours
    return {
        "datetime": f"2026-10-09 12:00:00 UTC{offset}",
        "opTimestamp": op_timestamp,
        "timezone": timezone_hours,
        "statusName": status_name,
        "description": description,
        "actionCode": action_code,
        "opCode": "0",
        **(extra or {}),
    }


def _envelope(code: str, statuses: list[dict], **extra) -> dict:
    """Wrap events in the gateway's success envelope."""
    return {
        "success": "true",
        "packageParam": {
            "dimWeight": {"weight": "1000", "weightUnit": "g"},
            "outOrderId": "0",
            "toZipcode": "0000-000",
            "trackingNumber": code,
        },
        "feature": "{}",
        "statuses": statuses,
        "cainiaoId": "0",
        "solutionParam": {
            "bizSource": "other",
            "logisticsMode": "other",
            "solutionCode": "X",
        },
        **extra,
    }


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """A door delivery, the ``GTMS_*``/``TD_*`` code family."""
    return _envelope(
        code,
        [
            event(
                "GTMS_SIGNED",
                "1791574506000",
                "Delivery Success",
                description="Parcel has been delivered successfully",
                timezone_hours="1",
                extra={
                    "deliveryLatitude": "0",
                    "deliveryLongitude": "0",
                    "imgUrl": "https://example.invalid/photo.jpg",
                },
            ),
            event(
                "GTMS_DO_DEPART",
                "1791544506000",
                "Out for Delivery",
                description="Out for delivery",
                timezone_hours="1",
            ),
            event(
                "GTMS_ACCEPT",
                "1791444506000",
                "Accepted by Last Mile",
                description="Parcel has been accepted by last mile",
            ),
            event(
                "TD_TRANSWH_OUTBOUND",
                "1791344506000",
                "Sorting Center Outbound",
                description="Parcels outbound from sorting center",
            ),
            event(
                "TD_TRANS_ARRIVE_C",
                "1791244506000",
                "Sorting Center Inbound",
                description="Parcel arrives at sorting center",
            ),
        ],
    )


def active_sample(code: str = ACTIVE_CODE) -> dict:
    """An out-for-delivery parcel, the last-mile ``LM_*``/``SC_*`` family."""
    return _envelope(
        code,
        [
            event(
                "LM_DELIVERY_DEPART",
                "1790580927000",
                "Out for Delivery",
                description="Your shipment is out for delivery",
                timezone_hours="1",
            ),
            event(
                "SL_ACCEPT",
                "1790574912000",
                "Accepted by Last Mile",
                description="Your shipment has been accepted by delivery carrier",
                timezone_hours="1",
            ),
            event(
                "SC_OUTBOUND",
                "1790532764000",
                "Sorting Center Outbound",
                description="Your shipment has been processed at sorting center",
            ),
            event(
                "SC_INBOUND",
                "1790354511000",
                "Sorting Center Inbound",
                description="Your shipment has arrived at sorting center",
            ),
            event(
                "CREATE_ORDER_CES",
                "1789437064000",
                "create",
                description="Shipment order created",
            ),
        ],
    )


def pickup_sample(code: str = PICKUP_CODE) -> dict:
    """A parcel waiting at a pickup point, with the station block attached."""
    return _envelope(
        code,
        [
            event(
                "PUDO_INBOUND",
                "1790600000000",
                "PUDO Inbound Success",
                description="Your shipment has arrived at parcelshop",
            ),
            event(
                "PUDO_DELIVERY",
                "1790590000000",
                "Delivered to PUDO",
                description="Your package has been processed by the courier",
            ),
            event(
                "LM_DELIVERY_DEPART",
                "1790580000000",
                "Out for Delivery",
                description="Your shipment is out for delivery",
            ),
        ],
        popStationParam={
            "officeTime": "Monday(09:00-13:00)",
            "phone": "+00****0000",
            "pinCode": "0000",
            "popStationId": "0",
            "detailAddress": "Example Street, 1, NaN, Example Town",
            "stationName": "Example Parcelshop",
        },
    )


def collected_sample(code: str = PICKUP_CODE) -> dict:
    """Collected at the pickup point — the station block is kept afterwards."""
    sample = pickup_sample(code)
    sample["statuses"].insert(
        0,
        event(
            "PUDO_SIGN_SUCCESS",
            "1790700000000",
            "PUDO Sign Success",
            description="Your shipment has been collected by consignee",
        ),
    )
    return sample


def failure_sample(code: str = ACTIVE_CODE) -> dict:
    """A last-mile exception. The carrier puts the reason in the label."""
    sample = active_sample(code)
    sample["statuses"].insert(
        0,
        event(
            "LM_DELIVERY_FAILURE",
            "1790600000000",
            "Delivery Attempt Failure",
            description="Your shipment delivery attempt failed [Recipient not at home]",
            extra={"opRemark": "example reason"},
        ),
    )
    return sample


def return_sample(code: str = ACTIVE_CODE) -> dict:
    """A parcel signed for back at the sender: a return, not a delivery."""
    sample = active_sample(code)
    for action_code, timestamp, name in (
        ("LM_UNREACHABLE_RETURN", "1790600000000", "Return to Origin"),
        ("RT_SC_INBOUND", "1790610000000", "Return Inbound at Sorting Center"),
        ("RT_LM_SIGN_SUCCESS", "1790620000000", "Return Success"),
    ):
        sample["statuses"].insert(0, event(action_code, timestamp, name))
    return sample


def malformed_timestamp_sample(code: str = ACTIVE_CODE) -> dict:
    """``opTimestamp`` missing or not an epoch value on some events."""
    sample = active_sample(code)
    sample["statuses"][0]["opTimestamp"] = "2026-10-09 20:35:06"
    sample["statuses"][1]["opTimestamp"] = None
    del sample["statuses"][2]["opTimestamp"]
    return sample
