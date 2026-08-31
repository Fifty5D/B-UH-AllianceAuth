"""Transparent Jita buy-order pricing compatible with compressed-ore valuation."""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

import requests
from django.utils.timezone import now

from . import __version__, app_settings
from .models import ZERO, AuditRun, PriceSnapshot, TaxConfiguration

CENT = Decimal("0.01")
PRICE_PRECISION = Decimal("0.00000001")


class PriceUnavailable(RuntimeError):
    pass


def weighted_buy_value(orders: list[dict], quantity: Decimal) -> dict:
    """Value a quantity against highest Jita buy orders until it is filled."""

    requested = max(ZERO, Decimal(quantity))
    remaining = requested
    total = ZERO
    used = 0
    best = None
    worst = None
    for order in sorted(orders, key=lambda item: Decimal(str(item["price"])), reverse=True):
        if remaining <= ZERO:
            break
        available = Decimal(str(order.get("volume_remain", 0)))
        price = Decimal(str(order.get("price", 0)))
        if available <= ZERO or price <= ZERO:
            continue
        filled = min(available, remaining)
        total += filled * price
        remaining -= filled
        used += 1
        best = price if best is None else best
        worst = price

    priced = requested - remaining
    weighted = total / priced if priced > ZERO else ZERO
    return {
        "requested": requested,
        "priced": priced,
        "total": total.quantize(CENT, rounding=ROUND_HALF_UP),
        "weighted": weighted.quantize(PRICE_PRECISION, rounding=ROUND_HALF_UP),
        "complete": remaining <= ZERO,
        "shortfall": remaining,
        "orders_used": used,
        "best_price": best,
        "worst_price": worst,
    }


class EsiJitaBuyClient:
    """Small, injectable ESI order-book client with finite timeouts and paging."""

    def __init__(self, session=None):
        self.session = session or requests.Session()

    def orders(self, type_id: int, *, region_id: int, location_id: int) -> list[dict]:
        url = f"{app_settings.ESI_BASE_URL}/markets/{region_id}/orders/"
        headers = {
            "Accept": "application/json",
            "User-Agent": f"B-UH-Moon-Tax/{__version__}",
        }
        params = {
            "datasource": "tranquility",
            "order_type": "buy",
            "type_id": int(type_id),
            "page": 1,
        }
        response = self.session.get(
            url,
            params=params,
            headers=headers,
            timeout=app_settings.ESI_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        pages = int(response.headers.get("X-Pages", "1"))
        results = list(response.json())
        for page in range(2, pages + 1):
            params["page"] = page
            page_response = self.session.get(
                url,
                params=params,
                headers=headers,
                timeout=app_settings.ESI_TIMEOUT_SECONDS,
            )
            page_response.raise_for_status()
            results.extend(page_response.json())
        return [
            order
            for order in results
            if int(order.get("location_id", 0)) == int(location_id)
            and bool(order.get("is_buy_order", True))
        ]


def _cached_snapshot(type_id: int, config: TaxConfiguration):
    cutoff = now() - dt.timedelta(hours=app_settings.PRICE_CACHE_HOURS)
    return (
        PriceSnapshot.objects.filter(
            compressed_type_id=type_id,
            complete=True,
            fetched_at__gte=cutoff,
            region_id=config.pricing_region_id,
            location_id=config.pricing_location_id,
        )
        .order_by("-fetched_at")
        .first()
    )


def create_price_snapshots(
    audit_run: AuditRun,
    needs: dict[int, dict],
    *,
    client: EsiJitaBuyClient | None = None,
) -> tuple[dict[int, PriceSnapshot], list[str]]:
    """Fetch each required compressed type once and preserve calculation evidence."""

    config, _ = TaxConfiguration.objects.get_or_create(singleton_id=1)
    client = client or EsiJitaBuyClient()
    snapshots = {}
    warnings = []
    for type_id, need in sorted(needs.items()):
        quantity = Decimal(need["quantity"])
        name = need["name"]
        try:
            orders = client.orders(
                type_id,
                region_id=config.pricing_region_id,
                location_id=config.pricing_location_id,
            )
            result = weighted_buy_value(orders, quantity)
            snapshot = PriceSnapshot.objects.create(
                audit_run=audit_run,
                compressed_type_id=type_id,
                compressed_type_name=name,
                requested_quantity=result["requested"],
                priced_quantity=result["priced"],
                weighted_unit_price=result["weighted"],
                total_value=result["total"],
                region_id=config.pricing_region_id,
                location_id=config.pricing_location_id,
                source="ESI Jita 4-4 buy-order depth",
                complete=result["complete"],
                raw_evidence={
                    "orders_considered": len(orders),
                    "orders_used": result["orders_used"],
                    "best_price": str(result["best_price"] or ""),
                    "worst_price": str(result["worst_price"] or ""),
                    "shortfall": str(result["shortfall"]),
                },
            )
            if not snapshot.complete:
                warnings.append(
                    f"Jita buy depth was insufficient for {name}; "
                    f"{result['shortfall']} compressed units were not priced."
                )
        except (requests.RequestException, ValueError, KeyError) as exc:
            cached = _cached_snapshot(type_id, config)
            if cached:
                snapshot = PriceSnapshot.objects.create(
                    audit_run=audit_run,
                    compressed_type_id=type_id,
                    compressed_type_name=name,
                    requested_quantity=quantity,
                    priced_quantity=quantity,
                    weighted_unit_price=cached.weighted_unit_price,
                    total_value=(quantity * cached.weighted_unit_price).quantize(CENT),
                    region_id=config.pricing_region_id,
                    location_id=config.pricing_location_id,
                    source=f"Cached ESI snapshot from {cached.fetched_at.isoformat()}",
                    complete=False,
                    raw_evidence={"fallback_snapshot_id": cached.pk, "error": str(exc)},
                )
                warnings.append(
                    f"Used a marked-stale cached Jita price for {name}: {exc}"
                )
            else:
                snapshot = PriceSnapshot.objects.create(
                    audit_run=audit_run,
                    compressed_type_id=type_id,
                    compressed_type_name=name,
                    requested_quantity=quantity,
                    priced_quantity=ZERO,
                    weighted_unit_price=ZERO,
                    total_value=ZERO,
                    region_id=config.pricing_region_id,
                    location_id=config.pricing_location_id,
                    source="Unavailable",
                    complete=False,
                    raw_evidence={"error": str(exc)},
                )
                warnings.append(f"No usable Jita price for {name}: {exc}")
        snapshots[type_id] = snapshot
    return snapshots, warnings
