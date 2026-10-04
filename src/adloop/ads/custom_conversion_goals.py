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

# Pointing a campaign at a different goal changes what Smart Bidding optimises
# for, and Google restarts the learning period when that happens. Both previews
# carry the note so the change is not mistaken for a no-op.
LEARNING_PERIOD_WARNING = (
    "Changing what a campaign optimises for restarts its Smart Bidding learning "
    "period — expect a few days of volatile performance before the bidding "
    "settles again."
)

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

    campaign_id = str(campaign_id or "").strip()
    if campaign_id and not campaign_id.isdigit():
        return {"error": "campaign_id must be a numeric ID"}

    cid = normalize_customer_id(customer_id or config.ads.customer_id)
    return read_state(get_ads_client(config), cid, campaign_id=campaign_id)


def read_state(client: object, cid: str, *, campaign_id: str = "") -> dict:
    """Read custom goals, one campaign's goal config and the conversion actions.

    Each query is isolated: a GAQL surprise in one part still returns the other
    parts plus the raw error (this API family has produced
    PROHIBITED_FIELD_IN_SELECT_CLAUSE before).
    """
    campaign_id = str(campaign_id or "").strip()
    if campaign_id and not campaign_id.isdigit():
        # The id goes into the query text; anything else must not reach GAQL.
        raise ValueError("campaign_id must be a numeric ID")
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


def _custom_goal_state(config: AdLoopConfig, customer_id: str, campaign_id: str = "") -> dict:
    """Read the custom goals, one campaign config and the conversion actions."""
    from adloop.ads.client import get_ads_client, normalize_customer_id

    cid = normalize_customer_id(customer_id or config.ads.customer_id)
    return read_state(
        get_ads_client(config), cid, campaign_id=str(campaign_id or "")
    )

def _clean_id_list(values: list[str] | None, label: str) -> tuple[list[str], list[str]]:
    """Trim, drop blanks, de-duplicate and require digits. Returns (errors, ids)."""
    errors: list[str] = []
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in values or []:
        value = str(raw).strip()
        if not value:
            continue
        if not value.isdigit():
            errors.append(f"{label} '{value}' must be a numeric ID")
            continue
        if value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    return errors, cleaned

def draft_custom_conversion_goal(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    name: str = "",
    conversion_action_ids: list[str] | None = None,
) -> dict:
    """Draft a new custom conversion goal — returns PREVIEW.

    A custom conversion goal bundles conversion actions into a named set that a
    campaign can then be pointed at. Only the goal itself is created here —
    conversion actions are read for validation and never modified.

    A new goal is always ENABLED: one created as REMOVED would be invisible to
    every other tool, so there is nothing to plan.

    Call ``confirm_and_apply`` with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_custom_conversion_goal", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    from adloop.ads.client import normalize_customer_id

    errors: list[str] = []
    name = (name or "").strip()
    if not name:
        errors.append("name is required")
    id_errors, action_ids = _clean_id_list(conversion_action_ids, "conversion_action_id")
    errors.extend(id_errors)
    if not action_ids and not id_errors:
        errors.append("At least one conversion_action_id is required")
    if errors:
        return {"error": "Validation failed", "details": errors}

    cid = normalize_customer_id(customer_id or config.ads.customer_id)
    state = _custom_goal_state(config, customer_id)
    action_errors, action_resources = validate_conversion_actions(
        state, cid, action_ids
    )
    if action_errors:
        return {"error": "Validation failed", "details": action_errors}

    for goal in state["custom_goals"]:
        # A REMOVED goal with this name is not an obstacle: it cannot be updated
        # (draft_update_custom_conversion_goal refuses REMOVED), so treating it
        # as a clash would leave no way to use the name again.
        if goal.get("status") == REMOVED:
            continue
        if (goal.get("name") or "").strip() != name:
            continue
        if set(goal.get("conversion_action_ids") or []) == set(action_ids):
            return {
                "status": "already_exists",
                "custom_conversion_goal_id": goal["id"],
                "custom_conversion_goal": goal_resource_name(cid, goal["id"]),
                "name": goal.get("name"),
                "conversion_action_ids": sorted(goal.get("conversion_action_ids") or []),
                "note": (
                    "A custom conversion goal with this name and exactly these "
                    "conversion actions already exists — nothing was planned."
                ),
            }
        return {
            "error": "Validation failed",
            "details": [
                f"A custom conversion goal named '{name}' already exists "
                f"(id {goal['id']}) but contains different conversion actions: "
                + ", ".join(sorted(goal.get("conversion_action_ids") or []))
                + ". Use draft_update_custom_conversion_goal to change it.",
            ],
        }

    plan = ChangePlan(
        operation="create_custom_conversion_goal",
        entity_type="custom_conversion_goal",
        entity_id="",
        customer_id=customer_id,
        changes={
            "name": name,
            "status": ENABLED,
            "conversion_action_ids": action_ids,
            "conversion_actions": action_resources,
        },
    )
    store_plan(plan)
    return plan.to_preview()

def draft_update_custom_conversion_goal(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    custom_conversion_goal_id: str = "",
    name: str | None = None,
    conversion_action_ids: list[str] | None = None,
) -> dict:
    """Draft changes to an existing custom conversion goal — returns PREVIEW.

    ``name`` renames; ``conversion_action_ids`` REPLACES the whole list (it is
    not an append). Both are optional — omitting one leaves it untouched.

    Call ``confirm_and_apply`` with the returned plan_id to execute.
    """
    from adloop.ads.client import normalize_customer_id
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_custom_conversion_goal", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors: list[str] = []
    goal_id = str(custom_conversion_goal_id or "").strip()
    if not goal_id:
        errors.append("custom_conversion_goal_id is required")
    elif not goal_id.isdigit():
        errors.append("custom_conversion_goal_id must be a numeric ID")
    new_name = name.strip() if isinstance(name, str) and name.strip() else None
    id_errors, action_ids = _clean_id_list(
        conversion_action_ids, "conversion_action_id"
    )
    errors.extend(id_errors)
    if name is None and conversion_action_ids is None:
        errors.append("Nothing to change — pass name and/or conversion_action_ids")
    if conversion_action_ids is not None and not action_ids and not id_errors:
        errors.append("conversion_action_ids must not be empty when provided")
    if errors:
        return {"error": "Validation failed", "details": errors}

    cid = normalize_customer_id(customer_id or config.ads.customer_id)
    state = _custom_goal_state(config, customer_id)
    goal = find_goal(state["custom_goals"], goal_id)
    if goal is None:
        return {
            "error": (
                f"Custom conversion goal {goal_id} does not exist in this account."
            )
        }
    if goal.get("status") == REMOVED:
        return {"error": f"Custom conversion goal {goal_id} is REMOVED."}

    action_resources: list[str] = []
    if action_ids:
        action_errors, action_resources = validate_conversion_actions(
            state, cid, action_ids
        )
        if action_errors:
            return {"error": "Validation failed", "details": action_errors}

    current_ids = sorted(goal.get("conversion_action_ids") or [])
    target_ids = sorted(action_ids) if action_ids else current_ids
    if (new_name is None or new_name == goal.get("name")) and target_ids == current_ids:
        return {
            "status": "no_change",
            "custom_conversion_goal_id": goal_id,
            "name": goal.get("name"),
            "conversion_action_ids": current_ids,
            "note": "The goal already matches the request — nothing was planned.",
        }

    changes: dict = {
        "custom_conversion_goal_id": goal_id,
        "name_before": goal.get("name"),
        "conversion_action_ids_before": current_ids,
    }
    if new_name is not None:
        changes["name_after"] = new_name
    if action_ids:
        changes["conversion_action_ids_after"] = target_ids
        changes["conversion_actions"] = action_resources

    plan = ChangePlan(
        operation="update_custom_conversion_goal",
        entity_type="custom_conversion_goal",
        entity_id=goal_id,
        customer_id=customer_id,
        changes=changes,
    )
    store_plan(plan)
    return plan.to_preview()

def draft_assign_custom_conversion_goal(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    custom_conversion_goal_id: str = "",
) -> dict:
    """Draft pointing one campaign at a custom conversion goal — returns PREVIEW.

    Sets ``goal_config_level = CAMPAIGN`` and the goal on the campaign's
    conversion goal config. Only the goal configuration changes — conversion
    actions, bidding, budgets and the account-level goal settings stay as they
    are.

    Call ``confirm_and_apply`` with the returned plan_id to execute.
    """
    from adloop.ads.client import normalize_customer_id
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("assign_custom_conversion_goal", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors: list[str] = []
    campaign_id = str(campaign_id or "").strip()
    goal_id = str(custom_conversion_goal_id or "").strip()
    if not campaign_id.isdigit():
        errors.append("campaign_id must be a numeric ID")
    if not goal_id:
        errors.append("custom_conversion_goal_id is required")
    elif not goal_id.isdigit():
        errors.append("custom_conversion_goal_id must be a numeric ID")
    if errors:
        return {"error": "Validation failed", "details": errors}

    cid = normalize_customer_id(customer_id or config.ads.customer_id)
    state = _custom_goal_state(config, customer_id, campaign_id=campaign_id)
    campaign = find_campaign(state, campaign_id)
    if campaign is None:
        return {
            "error": (
                f"Campaign {campaign_id} has no conversion goal configuration in "
                "this account — check the ID."
            )
        }
    if campaign.get("campaign_status") == "REMOVED":
        return {"error": f"Campaign {campaign_id} is REMOVED."}
    goal = find_goal(state["custom_goals"], goal_id)
    if goal is None:
        return {
            "error": (
                f"Custom conversion goal {goal_id} does not exist in this account."
            )
        }
    if goal.get("status") != ENABLED:
        return {
            "error": (
                f"Custom conversion goal {goal_id} is {goal.get('status')} — only "
                "ENABLED goals can be assigned."
            )
        }

    target = goal_resource_name(cid, goal_id)
    if (
        campaign.get("goal_config_level") == CAMPAIGN
        and campaign.get("custom_conversion_goal") == target
    ):
        return {
            "status": "already_configured",
            "campaign_id": campaign_id,
            "campaign_name": campaign.get("campaign_name"),
            "custom_conversion_goal": target,
            "note": "The campaign already uses exactly this goal — nothing was planned.",
        }

    plan = ChangePlan(
        operation="assign_custom_conversion_goal",
        entity_type="conversion_goal_campaign_config",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes={
            "campaign_id": campaign_id,
            "campaign_name": campaign.get("campaign_name"),
            "goal_config_level_before": campaign.get("goal_config_level"),
            "custom_conversion_goal_before": campaign.get("custom_conversion_goal") or "",
            "goal_config_level_after": CAMPAIGN,
            "custom_conversion_goal_after": target,
            "custom_conversion_goal_id": goal_id,
            "warnings": [LEARNING_PERIOD_WARNING],
        },
    )
    store_plan(plan)
    return plan.to_preview()

def draft_clear_custom_conversion_goal(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
) -> dict:
    """Draft putting a campaign back on the account-level goals — returns PREVIEW.

    Sets ``goal_config_level = CUSTOMER`` and clears the custom goal. This is
    the rollback for ``draft_assign_custom_conversion_goal``.

    Call ``confirm_and_apply`` with the returned plan_id to execute.
    """
    from adloop.ads.client import normalize_customer_id
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("clear_custom_conversion_goal", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    campaign_id = str(campaign_id or "").strip()
    if not campaign_id.isdigit():
        return {
            "error": "Validation failed",
            "details": ["campaign_id must be a numeric ID"],
        }

    cid = normalize_customer_id(customer_id or config.ads.customer_id)
    state = _custom_goal_state(config, customer_id, campaign_id=campaign_id)
    campaign = find_campaign(state, campaign_id)
    if campaign is None:
        return {
            "error": (
                f"Campaign {campaign_id} has no conversion goal configuration in "
                "this account — check the ID."
            )
        }
    if (
        campaign.get("goal_config_level") == CUSTOMER
        and not campaign.get("custom_conversion_goal")
    ):
        return {
            "status": "already_configured",
            "campaign_id": campaign_id,
            "campaign_name": campaign.get("campaign_name"),
            "goal_config_level": CUSTOMER,
            "note": "The campaign already uses the account-level goals.",
        }

    plan = ChangePlan(
        operation="clear_custom_conversion_goal",
        entity_type="conversion_goal_campaign_config",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes={
            "campaign_id": campaign_id,
            "campaign_name": campaign.get("campaign_name"),
            "goal_config_level_before": campaign.get("goal_config_level"),
            "custom_conversion_goal_before": campaign.get("custom_conversion_goal") or "",
            "goal_config_level_after": CUSTOMER,
            "custom_conversion_goal_after": "",
            "warnings": [LEARNING_PERIOD_WARNING],
        },
    )
    store_plan(plan)
    return plan.to_preview()

def _custom_goal_readback(client: object, cid: str, goal_id: str) -> dict:

    state = read_state(client, cid)
    goal = find_goal(state["custom_goals"], goal_id)
    return {
        "custom_conversion_goal": goal,
        "custom_conversion_goals": state["custom_goals"],
        "errors": state["errors"],
    }

def _campaign_config_readback(client: object, cid: str, campaign_id: str) -> dict:

    state = read_state(client, cid, campaign_id=campaign_id)
    return {
        "campaign": find_campaign(state, campaign_id),
        "errors": state["errors"],
    }

def _apply_create_custom_conversion_goal(
    client: object, cid: str, changes: dict
) -> dict:
    """Create the custom conversion goal, then read it back."""
    service = client.get_service("CustomConversionGoalService")
    operation = client.get_type("CustomConversionGoalOperation")
    goal = operation.create
    goal.name = changes["name"]
    goal.conversion_actions.extend(changes["conversion_actions"])
    # Always ENABLED, never taken from the plan: a goal created as REMOVED is
    # invisible to every other tool, so there is no useful variant to allow.
    goal.status = client.enums.CustomConversionGoalStatusEnum.ENABLED

    response = service.mutate_custom_conversion_goals(
        customer_id=cid, operations=[operation]
    )
    resource_name = response.results[0].resource_name
    goal_id = resource_name.rsplit("/", 1)[-1]
    return {
        "custom_conversion_goal": resource_name,
        "custom_conversion_goal_id": goal_id,
        "conversion_action_ids": changes["conversion_action_ids"],
        "readback": _custom_goal_readback(client, cid, goal_id),
    }

def _apply_update_custom_conversion_goal(
    client: object, cid: str, changes: dict
) -> dict:
    """Rename and/or replace the action list of a custom conversion goal."""
    from google.protobuf import field_mask_pb2


    service = client.get_service("CustomConversionGoalService")
    operation = client.get_type("CustomConversionGoalOperation")
    goal = operation.update
    goal.resource_name = goal_resource_name(cid, changes["custom_conversion_goal_id"])

    paths: list[str] = []
    if changes.get("name_after"):
        goal.name = changes["name_after"]
        paths.append("name")
    if changes.get("conversion_actions"):
        goal.conversion_actions.extend(changes["conversion_actions"])
        paths.append("conversion_actions")
    operation.update_mask = field_mask_pb2.FieldMask(paths=paths)

    response = service.mutate_custom_conversion_goals(
        customer_id=cid, operations=[operation]
    )
    return {
        "custom_conversion_goal": response.results[0].resource_name,
        "update_mask": paths,
        "readback": _custom_goal_readback(
            client, cid, changes["custom_conversion_goal_id"]
        ),
    }

def _conversion_goal_campaign_config_update(
    client: object, cid: str, campaign_id: str, level: str, goal_resource: str
) -> dict:
    """Write goal_config_level + custom_conversion_goal for one campaign."""
    from google.protobuf import field_mask_pb2


    service = client.get_service("ConversionGoalCampaignConfigService")
    operation = client.get_type("ConversionGoalCampaignConfigOperation")
    config = operation.update
    config.resource_name = campaign_config_resource_name(cid, campaign_id)
    config.goal_config_level = getattr(client.enums.GoalConfigLevelEnum, level)
    config.custom_conversion_goal = goal_resource
    operation.update_mask = field_mask_pb2.FieldMask(
        paths=["goal_config_level", "custom_conversion_goal"]
    )

    response = service.mutate_conversion_goal_campaign_configs(
        customer_id=cid, operations=[operation]
    )
    return {
        "conversion_goal_campaign_config": response.results[0].resource_name
        if response.results
        else campaign_config_resource_name(cid, campaign_id),
        "goal_config_level": level,
        "custom_conversion_goal": goal_resource,
    }

def _apply_assign_custom_conversion_goal(
    client: object, cid: str, changes: dict
) -> dict:
    """Point the campaign at the custom goal, then read the config back."""

    outcome = _conversion_goal_campaign_config_update(
        client, cid, changes["campaign_id"], CAMPAIGN,
        changes["custom_conversion_goal_after"],
    )
    outcome["readback"] = _campaign_config_readback(client, cid, changes["campaign_id"])
    return outcome

def _apply_clear_custom_conversion_goal(
    client: object, cid: str, changes: dict
) -> dict:
    """Put the campaign back on the account-level goals, then read it back."""

    outcome = _conversion_goal_campaign_config_update(
        client, cid, changes["campaign_id"], CUSTOMER, ""
    )
    outcome["readback"] = _campaign_config_readback(client, cid, changes["campaign_id"])
    return outcome
