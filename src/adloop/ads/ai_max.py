"""AI Max controls — the container that makes brand exclusions usable in Search.

Google Ads API v25 fields this module touches (verified against the SDK, not
guessed):

    campaign.ai_max_setting.enable_ai_max                            bool, writable
    campaign.ai_max_setting.bundling_required                        output only
    campaign.asset_automation_settings[]                             repeated
        .asset_automation_type    AssetAutomationTypeEnum
        .asset_automation_status  AssetAutomationStatusEnum (OPTED_IN / OPTED_OUT)
    ad_group.ai_max_ad_group_setting.disable_search_term_matching     bool, writable

Why this exists: attaching a brand list to a plain Search campaign is rejected
with

    For search advertising channel, brand lists can only be applied to
    exclusive targeting, broad match campaigns for inclusive targeting or
    PMax generated campaigns.

Search campaigns therefore need AI Max as the container for brand exclusions —
while everything AI Max would otherwise automate stays switched off: search
term matching per ad group, and the text/URL asset automations at campaign
level. ``enable_ai_max`` alone is not a safe state, which is why the apply
order below matters.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

# AssetAutomationTypeEnum members we expose by name in the tool interface.
# v25 has no plain "FINAL_URL_EXPANSION": the URL knob is modelled as
# FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION.
TEXT_ASSET_AUTOMATION = "TEXT_ASSET_AUTOMATION"
FINAL_URL_EXPANSION = "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION"

AUTOMATION_CHOICES = ("OPTED_IN", "OPTED_OUT", "UNCHANGED")

_CAMPAIGN_QUERY_ALL = """
    SELECT campaign.id, campaign.name, campaign.status,
           campaign.advertising_channel_type,
           campaign.ai_max_setting.enable_ai_max,
           campaign.ai_max_setting.bundling_required,
           campaign.asset_automation_settings
    FROM campaign
    WHERE campaign.status != 'REMOVED'
      AND campaign.advertising_channel_type = 'SEARCH'
    ORDER BY campaign.name
"""

_AD_GROUP_QUERY_ALL = """
    SELECT campaign.id, ad_group.id, ad_group.name, ad_group.status,
           ad_group.ai_max_ad_group_setting.disable_search_term_matching
    FROM ad_group
    WHERE ad_group.status != 'REMOVED'
      AND campaign.status != 'REMOVED'
      AND campaign.advertising_channel_type = 'SEARCH'
    ORDER BY campaign.id, ad_group.name
"""


def get_ai_max_settings(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
) -> dict:
    """Read-only view of the AI Max knobs, per campaign and per ad group.

    Without ``campaign_id`` every non-removed Search campaign is returned.
    """
    from adloop.ads.client import get_ads_client, normalize_customer_id

    cid = normalize_customer_id(customer_id or config.ads.customer_id)
    client = get_ads_client(config)
    return read_ai_max_state(client, cid, campaign_id=str(campaign_id or "").strip())


def read_ai_max_state(client: object, cid: str, *, campaign_id: str = "") -> dict:
    """Query the campaign settings and ad group switches; return a nested view."""
    service = client.get_service("GoogleAdsService")

    campaign_query = _CAMPAIGN_QUERY_ALL
    ad_group_query = _AD_GROUP_QUERY_ALL
    if campaign_id:
        campaign_query = _CAMPAIGN_QUERY_ALL.replace(
            "WHERE campaign.status != 'REMOVED'\n      "
            "AND campaign.advertising_channel_type = 'SEARCH'",
            f"WHERE campaign.id = {campaign_id}",
        )
        ad_group_query = _AD_GROUP_QUERY_ALL.replace(
            "AND campaign.status != 'REMOVED'\n      "
            "AND campaign.advertising_channel_type = 'SEARCH'",
            f"AND campaign.id = {campaign_id}",
        )

    campaign_rows = _search(service, cid, campaign_query)
    ad_group_rows = _search(service, cid, ad_group_query)

    campaigns = _normalize_campaign_rows(campaign_rows)
    by_id = {campaign["campaign_id"]: campaign for campaign in campaigns}
    for row in ad_group_rows:
        campaign_key = str(row.get("campaign.id", ""))
        campaign = by_id.get(campaign_key)
        if campaign is None:
            continue
        campaign["ad_groups"].append(
            {
                "ad_group_id": str(row.get("ad_group.id", "")),
                "ad_group_name": row.get("ad_group.name"),
                "status": row.get("ad_group.status"),
                "disable_search_term_matching": row.get(
                    "ad_group.ai_max_ad_group_setting.disable_search_term_matching"
                ),
            }
        )

    return {
        "campaigns": campaigns,
        "total_campaigns": len(campaigns),
        "total_ad_groups": sum(len(c["ad_groups"]) for c in campaigns),
    }


def _search(service: object, cid: str, query: str) -> list[dict]:
    """Run a GAQL query and flatten each row into {field_path: value}."""
    from adloop.ads.gaql import _extract_field, _parse_select_fields

    fields = _parse_select_fields(query)
    return [
        {field: _extract_field(row, field) for field in fields}
        for row in service.search(customer_id=cid, query=query)
    ]


def _normalize_campaign_rows(rows: list[dict]) -> list[dict]:
    """Collapse GAQL rows into one entry per campaign.

    GAQL may return one row per repeated ``asset_automation_settings`` element
    or a single row with parallel lists, depending on how the field is
    expanded. Both shapes are handled instead of assuming one.
    """
    campaigns: dict[str, dict] = {}
    for row in rows:
        key = str(row.get("campaign.id", ""))
        if not key:
            continue
        entry = campaigns.setdefault(
            key,
            {
                "campaign_id": key,
                "campaign_name": row.get("campaign.name"),
                "status": row.get("campaign.status"),
                "advertising_channel_type": row.get("campaign.advertising_channel_type"),
                "enable_ai_max": row.get("campaign.ai_max_setting.enable_ai_max"),
                "bundling_required": row.get(
                    "campaign.ai_max_setting.bundling_required"
                ),
                "asset_automation_settings": [],
                "ad_groups": [],
            },
        )
        for pair in _asset_automation_pairs(row):
            existing = {
                item["asset_automation_type"]: item
                for item in entry["asset_automation_settings"]
            }
            existing[pair["asset_automation_type"]] = pair
            entry["asset_automation_settings"] = list(existing.values())
    return list(campaigns.values())


def _asset_automation_pairs(row: dict) -> list[dict]:
    """Normalize the asset automation settings of one GAQL row.

    The whole message field is selected (``campaign.asset_automation_settings``,
    not its sub-fields — GAQL answers those with PROHIBITED_FIELD_IN_SELECT_CLAUSE),
    so the common shape is a list of dicts. The parallel-list shape is still
    handled because it is what a sub-field select would have produced.
    """
    value = row.get("campaign.asset_automation_settings")
    pairs: list[dict] = []

    if isinstance(value, dict):
        entries: list[object] = [value]
    elif isinstance(value, list):
        entries = value
    else:
        entries = []

    for entry in entries:
        if isinstance(entry, dict):
            asset_type = entry.get("asset_automation_type")
            if not asset_type:
                continue
            normalized = {"asset_automation_type": asset_type}
            if entry.get("asset_automation_status"):
                normalized["asset_automation_status"] = entry["asset_automation_status"]
            # AUTOMATED_VIDEO_CRAWL carries its configuration in a nested
            # setting instead of a status; keep it so a later write can send it
            # back unchanged.
            if entry.get("automated_video_crawl_setting"):
                normalized["automated_video_crawl_setting"] = entry[
                    "automated_video_crawl_setting"
                ]
            pairs.append(normalized)

    if pairs:
        return pairs

    # Fallback: sub-field selection shape (parallel lists in one row).
    types = row.get("campaign.asset_automation_settings.asset_automation_type")
    statuses = row.get("campaign.asset_automation_settings.asset_automation_status")
    if isinstance(types, list):
        for index, asset_type in enumerate(types):
            status = (
                statuses[index]
                if isinstance(statuses, list) and index < len(statuses)
                else None
            )
            if asset_type:
                pairs.append(
                    {"asset_automation_type": asset_type, "asset_automation_status": status}
                )
    elif types:
        pairs.append(
            {"asset_automation_type": types, "asset_automation_status": statuses}
        )
    return pairs


def merge_asset_automation(
    current: list[dict], updates: dict[str, str]
) -> list[dict]:
    """Return the complete settings list with ``updates`` applied per type.

    Google documents the field only as "the opt-in/out status of each
    AssetAutomationType" — not whether an update replaces or merges the list.
    Sending the complete merged list is correct either way, and preserving the
    untouched types is what keeps this from silently switching other
    automations on or off.
    """
    merged: dict[str, dict] = {}
    for item in current:
        asset_type = item.get("asset_automation_type")
        if asset_type:
            merged[asset_type] = dict(item)
    for asset_type, status in updates.items():
        if status == "UNCHANGED":
            continue
        entry = merged.setdefault(
            asset_type,
            {"asset_automation_type": asset_type, "asset_automation_status": None},
        )
        entry["asset_automation_status"] = status
    return list(merged.values())


def plan_targets(
    state: dict,
    *,
    disable_search_term_matching: bool,
    ad_group_ids: list[str] | None,
    include_paused_ad_groups: bool,
) -> tuple[list[dict], list[str]]:
    """Resolve which ad groups the plan touches. Returns (targets, warnings)."""
    groups = state.get("ad_groups", [])
    warnings: list[str] = []

    if ad_group_ids:
        wanted = [str(g) for g in ad_group_ids]
        known = {group["ad_group_id"]: group for group in groups}
        unknown = [g for g in wanted if g not in known]
        if unknown:
            warnings.append(
                "ad_group_ids not found in this campaign (or removed): "
                + ", ".join(unknown)
            )
        targets = [known[g] for g in wanted if g in known]
    else:
        targets = list(groups)
        if not include_paused_ad_groups:
            skipped = [g for g in targets if g.get("status") != "ENABLED"]
            targets = [g for g in targets if g.get("status") == "ENABLED"]
            if skipped:
                warnings.append(
                    f"{len(skipped)} paused ad group(s) skipped "
                    "(include_paused_ad_groups=false)"
                )

    for group in targets:
        if group.get("status") == "REMOVED":
            warnings.append(
                f"ad group {group['ad_group_id']} is REMOVED and must never be mutated"
            )
    targets = [g for g in targets if g.get("status") != "REMOVED"]
    return targets, warnings


# ---------------------------------------------------------------------------
# Mutation primitives (all called from the write applier)
# ---------------------------------------------------------------------------


def mutate_ad_group_search_term_matching(
    client: object, cid: str, ad_groups: list[dict], disable: bool
) -> dict:
    """Set ``disable_search_term_matching`` on the given ad groups in one request."""
    from google.protobuf import field_mask_pb2

    service = client.get_service("AdGroupService")
    operations = []
    for group in ad_groups:
        operation = client.get_type("AdGroupOperation")
        update = operation.update
        update.resource_name = service.ad_group_path(cid, group["ad_group_id"])
        update.ai_max_ad_group_setting.disable_search_term_matching = bool(disable)
        operation.update_mask = field_mask_pb2.FieldMask(
            paths=["ai_max_ad_group_setting.disable_search_term_matching"]
        )
        operations.append(operation)

    request = client.get_type("MutateAdGroupsRequest")
    request.customer_id = cid
    request.operations.extend(operations)
    request.partial_failure = True
    response = service.mutate_ad_groups(request=request)
    return _split_partial_failure(
        client, response, ad_groups, key="ad_group_id", partial_key="failed_ad_groups"
    )


def mutate_campaign_ai_max(client: object, cid: str, changes: dict) -> dict:
    """Update campaign-level AI Max settings in a single operation."""
    from google.protobuf import field_mask_pb2

    service = client.get_service("CampaignService")
    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = service.campaign_path(cid, changes["campaign_id"])

    paths: list[str] = []
    if changes.get("enable_ai_max") is not None:
        campaign.ai_max_setting.enable_ai_max = bool(changes["enable_ai_max"])
        paths.append("ai_max_setting.enable_ai_max")

    settings = changes.get("asset_automation_settings") or []
    if settings:
        # proto-plus repeated message fields have no ``add()`` and the nested
        # message classes are not reachable by attribute — but ``append`` takes
        # a plain dict and maps enum names and nested messages itself. Settings
        # read back from the account travel through unchanged, so an update that
        # replaces the list cannot silently drop a configuration we never
        # touched (AUTOMATED_VIDEO_CRAWL carries its own nested setting).
        for item in settings:
            entry = {"asset_automation_type": item["asset_automation_type"]}
            if item.get("asset_automation_status"):
                entry["asset_automation_status"] = item["asset_automation_status"]
            if item.get("automated_video_crawl_setting"):
                entry["automated_video_crawl_setting"] = item[
                    "automated_video_crawl_setting"
                ]
            campaign.asset_automation_settings.append(entry)
        paths.append("asset_automation_settings")

    if not paths:
        return {"attempted": False, "reason": "no campaign-level change requested"}

    operation.update_mask = field_mask_pb2.FieldMask(paths=paths)
    response = service.mutate_campaigns(customer_id=cid, operations=[operation])
    return {
        "attempted": True,
        "update_mask": paths,
        "resource_name": response.results[0].resource_name
        if response.results
        else None,
    }


def _split_partial_failure(
    client: object, response: object, targets: list[dict], *, key: str, partial_key: str
) -> dict:
    """Split a partial-failure mutate response into succeeded/failed entries."""
    from adloop.ads.write import _parse_partial_failure_per_op

    pf_error = getattr(response, "partial_failure_error", None)
    per_op_errors = _parse_partial_failure_per_op(client, pf_error)

    succeeded: list[str] = []
    failed: list[dict] = []
    for index, result in enumerate(response.results):
        target = targets[index] if index < len(targets) else {}
        if getattr(result, "resource_name", ""):
            succeeded.append(str(target.get(key, "")))
        else:
            failed.append(
                {
                    key: str(target.get(key, "")),
                    "operation_index": index,
                    "error": per_op_errors.get(
                        index, "Unknown error (see partial_failure_message)"
                    ),
                }
            )

    out: dict[str, Any] = {
        "succeeded": succeeded,
        "failed": failed,
        "count": len(succeeded),
    }
    if failed:
        out["partial_failure"] = True
        out[partial_key] = failed
        message = getattr(pf_error, "message", "") if pf_error is not None else ""
        if message:
            out["partial_failure_message"] = message
    return out
