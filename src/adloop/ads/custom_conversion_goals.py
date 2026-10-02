"""Custom conversion goals — named goal sets, and which campaign uses them.

Google Ads lets you bundle arbitrary conversion actions into a named goal and
point a campaign at it. The v25 resources (verified against the SDK):

    customers/{customer_id}/customConversionGoals/{goal_id}
        name, conversion_actions[], status (ENABLED / REMOVED)
    customers/{customer_id}/conversionGoalCampaignConfigs/{campaign_id}
        goal_config_level (CUSTOMER / CAMPAIGN), custom_conversion_goal

The custom goal is created/updated/removed through
``CustomConversionGoalService``; the campaign side is update-only via
``ConversionGoalCampaignConfigService`` — a config row exists per campaign, so
switching a campaign back to the account default means setting
``goal_config_level = CUSTOMER`` and clearing ``custom_conversion_goal``.

Neither mutate request has a ``partial_failure`` field, so a batch is
all-or-nothing per service. The drafts validate against the live account before
they produce a plan, which is why unknown or removed conversion actions are
refused there rather than at apply time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

ENABLED = "ENABLED"
REMOVED = "REMOVED"
CUSTOMER = "CUSTOMER"
CAMPAIGN = "CAMPAIGN"

_CUSTOM_GOAL_QUERY = """
    SELECT custom_conversion_goal.id, custom_conversion_goal.name,
           custom_conversion_goal.status,
           custom_conversion_goal.conversion_actions
    FROM custom_conversion_goal
    ORDER BY custom_conversion_goal.name
"""

_CAMPAIGN_CONFIG_QUERY = """
    SELECT campaign.id, campaign.name, campaign.status,
           conversion_goal_campaign_config.goal_config_level,
           conversion_goal_campaign_config.custom_conversion_goal
    FROM conversion_goal_campaign_config
    {where}
    ORDER BY campaign.id
"""

_CONVERSION_ACTION_QUERY = """
    SELECT conversion_action.id, conversion_action.name,
           conversion_action.status, conversion_action.type
    FROM conversion_action
    ORDER BY conversion_action.name
"""


def get_custom_conversion_goals(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
) -> dict:
    """Read-only view of the custom goals and the campaign goal configuration."""
    from adloop.ads.client import get_ads_client, normalize_customer_id

    cid = normalize_customer_id(customer_id or config.ads.customer_id)
    return read_state(
        get_ads_client(config), cid, campaign_id=str(campaign_id or "").strip()
    )


def read_state(client: object, cid: str, *, campaign_id: str = "") -> dict:
    """Read custom goals, one campaign's goal config and the conversion actions.

    Each query is isolated: a GAQL surprise in one part still returns the other
    parts plus the raw error (this API family has produced
    PROHIBITED_FIELD_IN_SELECT_CLAUSE before).
    """
    service = client.get_service("GoogleAdsService")
    where = f"WHERE campaign.id = {campaign_id}" if campaign_id else ""

    result: dict = {"custom_goals": [], "campaigns": [], "conversion_actions": [], "errors": []}
    goal_rows = _search_part(service, cid, _CUSTOM_GOAL_QUERY, result, "custom_goals")
    config_rows = _search_part(
        service, cid, _CAMPAIGN_CONFIG_QUERY.format(where=where), result, "campaign_config"
    )
    action_rows = _search_part(
        service, cid, _CONVERSION_ACTION_QUERY, result, "conversion_actions"
    )

    for row in goal_rows:
        actions = row.get("custom_conversion_goal.conversion_actions")
        if not isinstance(actions, list):
            actions = [actions] if actions else []
        result["custom_goals"].append(
            {
                "id": str(row.get("custom_conversion_goal.id", "")),
                "name": row.get("custom_conversion_goal.name"),
                "status": row.get("custom_conversion_goal.status"),
                "conversion_actions": [str(action) for action in actions],
                "conversion_action_ids": [
                    str(action).rsplit("/", 1)[-1] for action in actions
                ],
            }
        )

    for row in config_rows:
        result["campaigns"].append(
            {
                "campaign_id": str(row.get("campaign.id", "")),
                "campaign_name": row.get("campaign.name"),
                "campaign_status": row.get("campaign.status"),
                "goal_config_level": row.get(
                    "conversion_goal_campaign_config.goal_config_level"
                ),
                "custom_conversion_goal": row.get(
                    "conversion_goal_campaign_config.custom_conversion_goal"
                ),
            }
        )

    result["conversion_actions"] = [
        {
            "id": str(row.get("conversion_action.id", "")),
            "name": row.get("conversion_action.name"),
            "status": row.get("conversion_action.status"),
            "type": row.get("conversion_action.type"),
        }
        for row in action_rows
    ]
    return result


def _search_part(
    service: object, cid: str, query: str, result: dict, label: str
) -> list[dict]:
    from adloop.ads.gaql import _extract_field, _parse_select_fields

    try:
        fields = _parse_select_fields(query)
        return [
            {field: _extract_field(row, field) for field in fields}
            for row in service.search(customer_id=cid, query=query)
        ]
    except Exception as exc:  # noqa: BLE001 — one failing query must not hide the rest
        result["errors"].append({"part": label, "error": str(exc)})
        return []


def goal_resource_name(customer_id: str, goal_id: str) -> str:
    return f"customers/{customer_id}/customConversionGoals/{goal_id}"


def campaign_config_resource_name(customer_id: str, campaign_id: str) -> str:
    return f"customers/{customer_id}/conversionGoalCampaignConfigs/{campaign_id}"


def conversion_action_resource_name(customer_id: str, action_id: str) -> str:
    return f"customers/{customer_id}/conversionActions/{action_id}"


def find_goal(goals: list[dict], goal_id: str) -> dict | None:
    for goal in goals:
        if goal.get("id") == str(goal_id):
            return goal
    return None


def find_campaign(state: dict, campaign_id: str) -> dict | None:
    for campaign in state.get("campaigns", []):
        if campaign.get("campaign_id") == str(campaign_id):
            return campaign
    return None


def validate_conversion_actions(
    state: dict, customer_id: str, action_ids: list[str]
) -> tuple[list[str], list[str]]:
    """Check the requested conversion actions. Returns (errors, resource_names)."""
    known = {action["id"]: action for action in state.get("conversion_actions", [])}
    errors: list[str] = []
    resources: list[str] = []
    for action_id in action_ids:
        action = known.get(str(action_id))
        if action is None:
            errors.append(
                f"conversion action {action_id} does not exist in this account"
            )
            continue
        if action.get("status") == REMOVED:
            errors.append(
                f"conversion action {action_id} ({action.get('name')}) is REMOVED "
                "and cannot be used in a goal"
            )
            continue
        resources.append(conversion_action_resource_name(customer_id, action_id))
    return errors, resources
