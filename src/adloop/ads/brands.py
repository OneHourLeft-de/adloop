"""Google Ads brand lists — the SharedSet side of brand targeting.

A brand list is a ``SharedSet`` of type ``BRANDS``. Its members are
``SharedCriteria`` whose ``brand.entity_id`` carries the brand's Commercial
Knowledge Graph MID, not its display name.

Attaching a list to a campaign does NOT use ``CampaignSharedSet`` (that is the
negative-keyword path) but a ``CampaignCriterion.brand_list``, whose
``negative`` flag decides whether the list restricts targeting or excludes it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

# ---------------------------------------------------------------------------
# Brand lists — read side
# ---------------------------------------------------------------------------
#
# A brand list is a SharedSet of type BRANDS. Its members are SharedCriteria
# whose ``brand.entity_id`` carries the Commercial Knowledge Graph MID. Attaching
# a list to a campaign does NOT use CampaignSharedSet (that is the
# negative-keyword path) but a ``CampaignCriterion.brand_list`` — the criterion
# is what decides whether the list restricts targeting or excludes.


def get_brand_lists(config: AdLoopConfig, *, customer_id: str = "") -> dict:
    """List all brand lists (SharedSets of type BRANDS) in the account.

    Mirror of ``get_negative_keyword_lists`` for brands: ID, name, status and
    member count of every list. Call this before creating a new list so an
    existing one can be reused instead of duplicated.
    """
    from adloop.ads.gaql import execute_query

    query = """
        SELECT shared_set.id, shared_set.name, shared_set.status,
               shared_set.member_count, shared_set.resource_name
        FROM shared_set
        WHERE shared_set.type = 'BRANDS'
          AND shared_set.status != 'REMOVED'
        ORDER BY shared_set.name
    """

    rows = execute_query(config, customer_id, query)
    return {"brand_lists": rows, "total_lists": len(rows)}


def get_brand_list_brands(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    shared_set_id: str = "",
) -> dict:
    """List the brands inside one brand list.

    ``shared_set_id``: numeric ID from ``get_brand_lists`` (``shared_set.id``).

    Each entry carries its ``criterion_id`` and a ``resource_id`` of the form
    ``{shared_set_id}~{criterion_id}`` — that pair is what
    ``remove_from_brand_list`` needs.
    """
    from adloop.ads.gaql import execute_query

    sid = str(shared_set_id or "").strip()
    if not sid:
        return {"error": "shared_set_id is required"}
    if not sid.isdigit():
        return {"error": "shared_set_id must be a numeric ID"}

    query = f"""
        SELECT shared_criterion.criterion_id,
               shared_criterion.brand.entity_id,
               shared_criterion.brand.display_name,
               shared_criterion.brand.primary_url,
               shared_criterion.brand.status,
               shared_set.id, shared_set.name
        FROM shared_criterion
        WHERE shared_set.id = {sid}
        ORDER BY shared_criterion.brand.display_name
    """

    rows = execute_query(config, customer_id, query)
    for row in rows:
        ssid = row.get("shared_set.id")
        criterion_id = row.get("shared_criterion.criterion_id")
        if ssid and criterion_id:
            row["resource_id"] = f"{ssid}~{criterion_id}"
    return {"brands": rows, "total_brands": len(rows), "shared_set_id": sid}


def get_brand_list_campaigns(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    shared_set_id: str = "",
) -> dict:
    """List the campaigns a brand list is attached to.

    ``shared_set_id`` optional — omit to see every brand-list attachment in
    the account. Unlike negative keyword lists these attachments are
    ``CampaignCriterion`` rows of type ``BRAND_LIST``, so each entry carries
    the ``criterion_id`` and whether it is targeted or excluded.
    """
    from adloop.ads.gaql import execute_query

    sid = str(shared_set_id or "").strip()
    if sid and not sid.isdigit():
        return {"error": "shared_set_id must be a numeric ID (or empty)"}

    query = """
        SELECT campaign.id, campaign.name, campaign.status,
               campaign_criterion.criterion_id,
               campaign_criterion.negative,
               campaign_criterion.status,
               campaign_criterion.brand_list.shared_set
        FROM campaign_criterion
        WHERE campaign_criterion.type = 'BRAND_LIST'
          AND campaign_criterion.status != 'REMOVED'
        ORDER BY campaign.name
    """

    rows = execute_query(config, customer_id, query)
    if sid:
        suffix = f"/sharedSets/{sid}"
        rows = [
            row
            for row in rows
            if str(row.get("campaign_criterion.brand_list.shared_set", "")).endswith(suffix)
        ]

    for row in rows:
        campaign_id = row.get("campaign.id")
        criterion_id = row.get("campaign_criterion.criterion_id")
        if campaign_id and criterion_id:
            row["resource_id"] = f"{campaign_id}~{criterion_id}"
        row["role"] = "excluded" if row.get("campaign_criterion.negative") else "targeted"
    return {"campaigns": rows, "total_attachments": len(rows)}
