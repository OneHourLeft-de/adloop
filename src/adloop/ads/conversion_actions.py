"""Conversion-action write tools — Google Ads ConversionActionService.

All operations follow the AdLoop safety pattern:
    1. draft_*  → creates a ChangePlan, stores it, returns plan_id
    2. confirm_and_apply(plan_id) → executes via the Google Ads API

Supported types (conversion_action.type):
    AD_CALL              — calls from Call assets in ads
    WEBSITE_CALL         — Google Forwarding Number calls (uses
                           phone_call_duration_seconds threshold)
    WEBPAGE              — page-load conversions with code-based tracking
    WEBPAGE_CODELESS     — page-load conversions detected by Ads (no snippet)
    GOOGLE_ANALYTICS_4_CUSTOM   — imported from GA4 (custom event)
    GOOGLE_ANALYTICS_4_PURCHASE — imported from GA4 (purchase event)
    UPLOAD_CALLS, UPLOAD_CLICKS — offline imports

NOT supported here (Google manages them — mutations are rejected with
MUTATE_NOT_ALLOWED):
    SMART_CAMPAIGN_*  — auto-created by Smart Campaigns
    GOOGLE_HOSTED     — auto-created by Google Business Profile / LSA links
"""
from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from adloop.ads.enums import enum_names

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


# Pulled dynamically from the google-ads SDK at the API version we're
# pinned to (see adloop.ads.client.GOOGLE_ADS_API_VERSION). Keeps the
# validators in sync with whatever the SDK supports — no hand-maintained
# parallel lists to drift.
_VALID_TYPES = enum_names("ConversionActionTypeEnum")
_VALID_CATEGORIES = enum_names("ConversionActionCategoryEnum")
_VALID_COUNTING_TYPES = enum_names("ConversionActionCountingTypeEnum")
_VALID_ATTRIBUTION_MODELS = enum_names("AttributionModelEnum")

# These types ARE in ConversionActionTypeEnum but Google rejects mutations
# on them with MUTATE_NOT_ALLOWED (they're auto-created by Smart Campaigns,
# Local Services, and Business Profile links). We don't filter them from
# `_VALID_TYPES` — the SDK accepts them syntactically — but warn callers.
_AUTO_MANAGED_TYPES = frozenset({
    "SMART_CAMPAIGN_TRACKED_CALLS",
    "SMART_CAMPAIGN_MAP_DIRECTIONS",
    "SMART_CAMPAIGN_MAP_CLICKS_TO_CALL",
    "SMART_CAMPAIGN_AD_CLICKS_TO_CALL",
    "GOOGLE_HOSTED",
})


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def _validate_create_inputs(
    *,
    name: str,
    type_: str,
    category: str,
    counting_type: str,
    default_value: float,
    currency_code: str,
    phone_call_duration_seconds: int,
    click_through_window_days: int,
    view_through_window_days: int,
    attribution_model: str,
) -> list[str]:
    errors: list[str] = []
    if not name or not name.strip():
        errors.append("name is required")
    if type_ not in _VALID_TYPES:
        errors.append(
            f"type '{type_}' invalid; valid: {sorted(_VALID_TYPES)}"
        )
    if category and category not in _VALID_CATEGORIES:
        errors.append(
            f"category '{category}' invalid; valid: {sorted(_VALID_CATEGORIES)}"
        )
    if counting_type and counting_type not in _VALID_COUNTING_TYPES:
        errors.append(
            f"counting_type '{counting_type}' invalid; valid: "
            f"{sorted(_VALID_COUNTING_TYPES)}"
        )
    if default_value < 0:
        errors.append("default_value must be >= 0")
    if currency_code and len(currency_code) != 3:
        errors.append(
            f"currency_code '{currency_code}' must be a 3-letter ISO code"
        )
    if phone_call_duration_seconds and phone_call_duration_seconds < 0:
        errors.append("phone_call_duration_seconds must be >= 0")
    if (click_through_window_days
            and not (1 <= click_through_window_days <= 90)):
        errors.append(
            "click_through_window_days must be between 1 and 90"
        )
    if (view_through_window_days
            and not (1 <= view_through_window_days <= 30)):
        errors.append(
            "view_through_window_days must be between 1 and 30"
        )
    if attribution_model and attribution_model not in _VALID_ATTRIBUTION_MODELS:
        errors.append(
            f"attribution_model '{attribution_model}' invalid; valid: "
            f"{sorted(_VALID_ATTRIBUTION_MODELS)}"
        )
    return errors


def _validate_update_inputs(
    *,
    counting_type: str,
    default_value: float,
    currency_code: str,
    phone_call_duration_seconds: int,
    click_through_window_days: int,
    view_through_window_days: int,
    attribution_model: str,
) -> list[str]:
    errors: list[str] = []
    if counting_type and counting_type not in _VALID_COUNTING_TYPES:
        errors.append(
            f"counting_type '{counting_type}' invalid; valid: "
            f"{sorted(_VALID_COUNTING_TYPES)}"
        )
    if default_value < 0:
        errors.append("default_value must be >= 0")
    if currency_code and len(currency_code) != 3:
        errors.append(
            f"currency_code '{currency_code}' must be a 3-letter ISO code"
        )
    if phone_call_duration_seconds and phone_call_duration_seconds < 0:
        errors.append("phone_call_duration_seconds must be >= 0")
    if (click_through_window_days
            and not (1 <= click_through_window_days <= 90)):
        errors.append(
            "click_through_window_days must be between 1 and 90"
        )
    if (view_through_window_days
            and not (1 <= view_through_window_days <= 30)):
        errors.append(
            "view_through_window_days must be between 1 and 30"
        )
    if attribution_model and attribution_model not in _VALID_ATTRIBUTION_MODELS:
        errors.append(
            f"attribution_model '{attribution_model}' invalid; valid: "
            f"{sorted(_VALID_ATTRIBUTION_MODELS)}"
        )
    return errors


# ---------------------------------------------------------------------------
# Draft tools (return PREVIEW + plan_id)
# ---------------------------------------------------------------------------


def draft_create_conversion_action(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    name: str,
    type_: str,
    category: str = "DEFAULT",
    default_value: float = 0,
    currency_code: str = "USD",
    always_use_default_value: bool = False,
    counting_type: str = "ONE_PER_CLICK",
    phone_call_duration_seconds: int = 0,
    primary_for_goal: bool = True,
    include_in_conversions_metric: bool = True,
    click_through_window_days: int = 0,
    view_through_window_days: int = 0,
    attribution_model: str = "",
) -> dict:
    """Draft a new ConversionAction — returns a PREVIEW.

    type_: the ConversionAction.type enum value (AD_CALL, WEBSITE_CALL,
        WEBPAGE, WEBPAGE_CODELESS, GOOGLE_ANALYTICS_4_CUSTOM, etc.).
    category: the conversion category (PHONE_CALL_LEAD, SUBMIT_LEAD_FORM,
        PURCHASE, etc.). Defaults to DEFAULT.
    default_value: monetary value attributed to each conversion.
    always_use_default_value: when True, transaction values from the
        snippet/import are ignored and default_value is used instead. When
        False with a positive default_value, Google treats default_value as
        a fallback ("tag value with fallback"). Passing a positive
        default_value with this flag False is a legal config — the draft
        surfaces a warning (see below) but does NOT flip the flag for you.
    counting_type: ONE_PER_CLICK (recommended for lead gen — one click,
        one conversion no matter how many events fire) or MANY_PER_CLICK
        (better for ecommerce where multiple purchases per click are real).
    phone_call_duration_seconds: ONLY meaningful for PHONE_CALL_LEAD
        category. The call must last at least this many seconds to count.
    primary_for_goal: True = drives Smart Bidding optimization;
        False = Secondary (records but doesn't affect bidding).
    include_in_conversions_metric: True (default) = appears in the
        "Conversions" column; False = "All conversions" only. NOTE: this is
        IMMUTABLE on create — Google derives it from the category and rejects
        any value set in the create mutate. To change it, use
        draft_update_conversion_action after the create succeeds.
    click_through_window_days / view_through_window_days: attribution
        windows. 30/1 is the typical lead-gen pair.
    attribution_model: leave empty for the default. For data-driven,
        pass GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_conversion_action", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors = _validate_create_inputs(
        name=name,
        type_=type_,
        category=category,
        counting_type=counting_type,
        default_value=default_value,
        currency_code=currency_code,
        phone_call_duration_seconds=phone_call_duration_seconds,
        click_through_window_days=click_through_window_days,
        view_through_window_days=view_through_window_days,
        attribution_model=attribution_model,
    )
    if errors:
        return {"error": "Validation failed", "details": errors}

    warnings: list[str] = []

    # A positive default_value paired with always_use_default_value=False is a
    # LEGAL config: Google treats default_value as a fallback when the
    # snippet/import supplies no value ("tag value with fallback"). We used to
    # silently force the flag to True, which turned that fallback config into
    # an unconditional override — a real change in accounting the caller never
    # asked for. Surface it as a preview warning instead and leave the flag
    # exactly as the caller set it.
    if default_value > 0 and not always_use_default_value:
        warnings.append(
            "default_value is set but always_use_default_value is False: "
            "Google will treat default_value as a FALLBACK, used only when "
            "the tag/import provides no value. If you want default_value to "
            "override every conversion's value, set "
            "always_use_default_value=True explicitly."
        )

    if type_ in _AUTO_MANAGED_TYPES:
        warnings.append(
            f"type '{type_}' is auto-managed by Google (Smart Campaigns / "
            "Business Profile). Mutations are rejected with MUTATE_NOT_ALLOWED."
        )

    plan = ChangePlan(
        operation="create_conversion_action",
        entity_type="conversion_action",
        entity_id="",
        customer_id=customer_id,
        changes={
            "name": name.strip(),
            "type": type_,
            "category": category,
            "default_value": float(default_value),
            "currency_code": currency_code.upper(),
            "always_use_default_value": bool(always_use_default_value),
            "counting_type": counting_type,
            "phone_call_duration_seconds": int(phone_call_duration_seconds or 0),
            "primary_for_goal": bool(primary_for_goal),
            "include_in_conversions_metric": bool(include_in_conversions_metric),
            "click_through_window_days": int(click_through_window_days or 0),
            "view_through_window_days": int(view_through_window_days or 0),
            "attribution_model": attribution_model,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


def draft_update_conversion_action(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    conversion_action_id: str,
    name: str = "",
    primary_for_goal: bool | None = None,
    default_value: float = 0,
    currency_code: str = "",
    always_use_default_value: bool | None = None,
    counting_type: str = "",
    phone_call_duration_seconds: int = 0,
    include_in_conversions_metric: bool | None = None,
    click_through_window_days: int = 0,
    view_through_window_days: int = 0,
    attribution_model: str = "",
) -> dict:
    """Draft a partial UPDATE of an existing ConversionAction — returns PREVIEW.

    Only the parameters you pass non-empty/non-default will be sent to the
    API. Use this to rename, demote a Primary to Secondary, change value,
    adjust the call-duration threshold, or change attribution settings.
    include_in_conversions_metric IS mutable here (unlike on create).

    conversion_action_id: numeric ID. Find via:
        SELECT conversion_action.id, conversion_action.name FROM conversion_action

    Note: Google rejects mutations on SMART_CAMPAIGN_* and GOOGLE_HOSTED
    types with MUTATE_NOT_ALLOWED. Catch and report this at apply time.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_conversion_action", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    if not conversion_action_id:
        return {"error": "conversion_action_id is required"}

    errors = _validate_update_inputs(
        counting_type=counting_type,
        default_value=default_value,
        currency_code=currency_code,
        phone_call_duration_seconds=phone_call_duration_seconds,
        click_through_window_days=click_through_window_days,
        view_through_window_days=view_through_window_days,
        attribution_model=attribution_model,
    )
    if errors:
        return {"error": "Validation failed", "details": errors}

    # Track which fields the caller actually wants to update so we build
    # the right field_mask at apply time.
    changes: dict = {"conversion_action_id": str(conversion_action_id)}
    if name:
        changes["name"] = name.strip()
    if primary_for_goal is not None:
        changes["primary_for_goal"] = bool(primary_for_goal)
    if default_value:
        changes["default_value"] = float(default_value)
    if currency_code:
        changes["currency_code"] = currency_code.upper()
    if always_use_default_value is not None:
        changes["always_use_default_value"] = bool(always_use_default_value)
    if counting_type:
        changes["counting_type"] = counting_type
    if phone_call_duration_seconds:
        changes["phone_call_duration_seconds"] = int(phone_call_duration_seconds)
    if include_in_conversions_metric is not None:
        changes["include_in_conversions_metric"] = bool(
            include_in_conversions_metric
        )
    if click_through_window_days:
        changes["click_through_window_days"] = int(click_through_window_days)
    if view_through_window_days:
        changes["view_through_window_days"] = int(view_through_window_days)
    if attribution_model:
        changes["attribution_model"] = attribution_model

    if len(changes) == 1:  # only conversion_action_id
        return {"error": "No fields to update"}

    warnings: list[str] = []
    # Same fallback-vs-override nuance as create: on update, a positive
    # default_value with always_use_default_value explicitly set to False is
    # legal (fallback). Warn rather than silently overriding intent.
    if changes.get("default_value", 0) > 0 and (
        changes.get("always_use_default_value") is False
    ):
        warnings.append(
            "default_value is set but always_use_default_value is False: "
            "Google will treat default_value as a FALLBACK, used only when "
            "the tag/import provides no value. Set "
            "always_use_default_value=True explicitly to override every "
            "conversion's value."
        )

    plan = ChangePlan(
        operation="update_conversion_action",
        entity_type="conversion_action",
        entity_id=str(conversion_action_id),
        customer_id=customer_id,
        changes=changes,
    )
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


def draft_remove_conversion_action(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    conversion_action_id: str,
) -> dict:
    """Draft a REMOVAL of a ConversionAction — returns PREVIEW.

    Removed conversion actions stop counting and disappear from goal lists.
    Historical data is preserved. SMART_CAMPAIGN_* and GOOGLE_HOSTED types
    cannot be removed via API (Google manages them); the apply will fail
    with MUTATE_NOT_ALLOWED for those.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("remove_conversion_action", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    if not conversion_action_id:
        return {"error": "conversion_action_id is required"}

    plan = ChangePlan(
        operation="remove_conversion_action",
        entity_type="conversion_action",
        entity_id=str(conversion_action_id),
        customer_id=customer_id,
        changes={"conversion_action_id": str(conversion_action_id)},
        requires_double_confirm=True,
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = [
        "Removing a ConversionAction is irreversible. Smart Campaign / GBP-"
        "managed types reject mutation with MUTATE_NOT_ALLOWED."
    ]
    return preview


# ---------------------------------------------------------------------------
# Apply handlers
# ---------------------------------------------------------------------------


def _apply_create_conversion_action(client: object, cid: str, changes: dict) -> dict:
    """Create a new ConversionAction."""
    svc = client.get_service("ConversionActionService")
    op = client.get_type("ConversionActionOperation")
    ca = op.create
    ca.name = changes["name"]
    ca.type_ = getattr(client.enums.ConversionActionTypeEnum, changes["type"])
    ca.category = getattr(
        client.enums.ConversionActionCategoryEnum, changes["category"]
    )
    ca.status = client.enums.ConversionActionStatusEnum.ENABLED
    ca.counting_type = getattr(
        client.enums.ConversionActionCountingTypeEnum, changes["counting_type"]
    )
    ca.value_settings.default_value = changes["default_value"]
    ca.value_settings.default_currency_code = changes["currency_code"]
    ca.value_settings.always_use_default_value = changes["always_use_default_value"]
    ca.primary_for_goal = changes["primary_for_goal"]
    # NOTE: include_in_conversions_metric is IMMUTABLE on create — Google
    # derives it from the conversion category and rejects any value set in
    # the create mutate (IMMUTABLE_FIELD). To change it, use
    # draft_update_conversion_action after the create succeeds.
    if changes.get("phone_call_duration_seconds"):
        ca.phone_call_duration_seconds = changes["phone_call_duration_seconds"]
    if changes.get("click_through_window_days"):
        ca.click_through_lookback_window_days = changes["click_through_window_days"]
    if changes.get("view_through_window_days"):
        ca.view_through_lookback_window_days = changes["view_through_window_days"]
    if changes.get("attribution_model"):
        ca.attribution_model_settings.attribution_model = getattr(
            client.enums.AttributionModelEnum, changes["attribution_model"]
        )

    response = svc.mutate_conversion_actions(
        customer_id=cid, operations=[op]
    )
    return {"resource_name": response.results[0].resource_name}


def _apply_update_conversion_action(client: object, cid: str, changes: dict) -> dict:
    """Partial update of an existing ConversionAction.

    Builds a FieldMask listing only the fields the caller wanted to update.
    """
    from google.protobuf import field_mask_pb2

    svc = client.get_service("ConversionActionService")
    op = client.get_type("ConversionActionOperation")
    ca = op.update
    ca.resource_name = svc.conversion_action_path(
        cid, changes["conversion_action_id"]
    )

    paths: list[str] = []

    if "name" in changes:
        ca.name = changes["name"]
        paths.append("name")
    if "primary_for_goal" in changes:
        ca.primary_for_goal = changes["primary_for_goal"]
        paths.append("primary_for_goal")
    if "default_value" in changes:
        ca.value_settings.default_value = changes["default_value"]
        paths.append("value_settings.default_value")
    if "currency_code" in changes:
        ca.value_settings.default_currency_code = changes["currency_code"]
        paths.append("value_settings.default_currency_code")
    if "always_use_default_value" in changes:
        ca.value_settings.always_use_default_value = changes["always_use_default_value"]
        paths.append("value_settings.always_use_default_value")
    if "counting_type" in changes:
        ca.counting_type = getattr(
            client.enums.ConversionActionCountingTypeEnum, changes["counting_type"]
        )
        paths.append("counting_type")
    if "phone_call_duration_seconds" in changes:
        ca.phone_call_duration_seconds = changes["phone_call_duration_seconds"]
        paths.append("phone_call_duration_seconds")
    if "include_in_conversions_metric" in changes:
        ca.include_in_conversions_metric = changes["include_in_conversions_metric"]
        paths.append("include_in_conversions_metric")
    if "click_through_window_days" in changes:
        ca.click_through_lookback_window_days = changes["click_through_window_days"]
        paths.append("click_through_lookback_window_days")
    if "view_through_window_days" in changes:
        ca.view_through_lookback_window_days = changes["view_through_window_days"]
        paths.append("view_through_lookback_window_days")
    if "attribution_model" in changes:
        ca.attribution_model_settings.attribution_model = getattr(
            client.enums.AttributionModelEnum, changes["attribution_model"]
        )
        paths.append("attribution_model_settings.attribution_model")

    op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
    response = svc.mutate_conversion_actions(
        customer_id=cid, operations=[op]
    )
    return {"resource_name": response.results[0].resource_name}


def _apply_remove_conversion_action(client: object, cid: str, changes: dict) -> dict:
    """Remove a ConversionAction (sets status=REMOVED)."""
    svc = client.get_service("ConversionActionService")
    op = client.get_type("ConversionActionOperation")
    op.remove = svc.conversion_action_path(
        cid, changes["conversion_action_id"]
    )
    response = svc.mutate_conversion_actions(
        customer_id=cid, operations=[op]
    )
    return {"resource_name": response.results[0].resource_name}


# ===========================================================================
# Offline conversion uploads — ConversionUploadService
# ===========================================================================
#
# Two upload paths live here:
#
#   1. Call conversions (UploadCallConversions) — matches phone calls back to
#      ad clicks by caller_id (E.164 phone). The caller_id is REQUIRED raw by
#      Google for matching and CANNOT be hashed. It lives in the plan's
#      apply-only payload so apply can rebuild the upload without re-reading
#      the CSV, and the preview/audit surfaces never see it — the summary they
#      show carries redacted ids only (see _redact_caller_id).
#
#   2. Enhanced Conversions for Leads (UploadClickConversions with
#      user_identifiers) — matches hashed PII (email / phone / name) back to
#      logged-in Google users who clicked our ads. PII is normalized and
#      SHA-256-hashed AT PREVIEW TIME; only the hashes are stored in the plan.
#      Raw PII never lands in plan.changes and never reaches the audit log.
#
# Security invariant shared by both: apply builds the upload protos from
# ``plan.changes["rows"]`` (frozen at preview time), NOT by re-reading the
# CSV. What you previewed is exactly what gets uploaded, and no raw PII is
# re-read at apply time.
# ---------------------------------------------------------------------------


def _sha256_hex(value: str) -> str:
    """SHA-256 a UTF-8 string, return lowercase hex. Empty in → empty out."""
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_email(email: str) -> str:
    """Normalize an email for Enhanced Conversions: trim + lowercase.

    Google's canonicalization for EC is trim + lowercase. (Gmail dot/plus
    stripping is NOT applied by Google's EC matcher — it matches on the
    literal normalized address — so we deliberately do NOT strip dots or
    +tags. Doing so would REDUCE the match rate.)
    """
    return (email or "").strip().lower()


def _normalize_name(name: str) -> str:
    """Normalize a first/last name for EC: trim + lowercase."""
    return (name or "").strip().lower()


def _normalize_phone_e164(phone: str) -> str:
    """Best-effort E.164 normalization for a phone number.

    Rules (deliberately conservative — Google requires E.164 for EC phone
    hashing and CallConversion.caller_id):
      * Strip spaces, hyphens, parens, dots.
      * A leading "00" is the international-access prefix → replace with "+".
      * A single leading domestic trunk "0" (common in EU national format,
        e.g. UK "020 7946 0018") is dropped — but ONLY one zero, and ONLY
        when there's no "+" already. We do NOT strip every leading zero.
      * Italy is the notable exception: Italian fixed-line numbers KEEP their
        leading 0 in E.164 (e.g. Rome "+39 06 …"). We can't reliably detect
        country from a bare national number, so the safe, documented rule is:
        if the number already carries a country code (starts with "+"), we
        never touch interior digits. A bare Italian number passed without a
        "+" can't be disambiguated here — callers should pass Italian numbers
        in full "+39…" form. This keeps the common EU trunk-zero case correct
        without corrupting Italy's retained-zero numbers that arrive as "+39…".

    Returns the number with a leading "+" when we could infer one; otherwise
    returns the cleaned digits unchanged (Google will reject a non-E.164
    number, surfaced as a per-row failure rather than silently mangled).
    """
    s = (phone or "").strip()
    if not s:
        return ""
    has_plus = s.startswith("+")
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return ""
    if has_plus:
        # Already carries a country code — trust it verbatim (this is the
        # path that preserves Italy's retained leading zero, e.g. +3906…).
        return "+" + digits
    # "00" international access prefix → "+"
    if digits.startswith("00"):
        return "+" + digits[2:]
    # Strip EXACTLY ONE domestic trunk zero (national dialing format).
    if digits.startswith("0"):
        return digits[1:]
    return digits


def _gaql_escape(s: str) -> str:
    """Escape a string literal for interpolation into a GAQL WHERE clause.

    GAQL uses BACKSLASH escaping (NOT SQL-style doubled quotes). Escape the
    backslash first, then the single quote. Handles names like ``O'Brien``.
    """
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _consent_from_param(consent: dict | None) -> dict | None:
    """Validate + normalize the ``consent`` tool parameter.

    Accepts ``{"ad_user_data": "GRANTED"|"DENIED"|"UNSPECIFIED",
    "ad_personalization": ...}``. Missing keys default to UNSPECIFIED.
    Returns a plain dict stored in the plan (JSON-safe, non-PII), or None
    if no consent was supplied at all.
    """
    if not consent:
        return None
    valid = {"UNSPECIFIED", "UNKNOWN", "GRANTED", "DENIED"}
    out: dict[str, str] = {}
    for key in ("ad_user_data", "ad_personalization"):
        raw = str(consent.get(key, "UNSPECIFIED") or "UNSPECIFIED").upper()
        if raw not in valid:
            raise ValueError(
                f"consent.{key}='{raw}' is invalid. Use one of: "
                "GRANTED, DENIED, UNSPECIFIED."
            )
        out[key] = raw
    return out


def _apply_consent(client: object, conversion: object, consent: dict | None) -> None:
    """Set conversion.consent.{ad_user_data,ad_personalization} from a plan dict.

    Maps the stored string values to the ConsentStatus enum. A None/empty
    consent leaves the proto default (UNSPECIFIED) — which is the correct
    "not provided" signal for Google.
    """
    if not consent:
        return
    status_enum = client.enums.ConsentStatusEnum
    aud = consent.get("ad_user_data", "UNSPECIFIED")
    ap = consent.get("ad_personalization", "UNSPECIFIED")
    conversion.consent.ad_user_data = getattr(status_enum, aud)
    conversion.consent.ad_personalization = getattr(status_enum, ap)


# ---------------------------------------------------------------------------
# Timestamp + CSV parsing shared with the call-conversion path
# ---------------------------------------------------------------------------

_EXPECTED_CALL_HEADERS = [
    "Caller's Phone Number",
    "Call Start Time",
    "Conversion Name",
    "Conversion Time",
    "Conversion Value",
    "Conversion Currency",
]


def _normalize_call_timestamp(ts: str) -> str:
    """Google Ads API wants 'yyyy-mm-dd HH:MM:SS+|-HH:MM'.

    Our CSV writes ISO 8601 with 'T' separator and trailing 'Z'
    (e.g. '2026-02-26T16:49:44.567Z'). Convert: strip fractional
    seconds, replace 'T' with space, replace 'Z' with '+00:00'.
    """
    s = (ts or "").strip()
    if not s:
        return s
    if "." in s:
        head, tail = s.split(".", 1)
        tz = ""
        for marker in ("+", "-", "Z"):
            idx = tail.find(marker)
            if idx >= 0:
                tz = tail[idx:]
                break
        s = head + (tz or "")
    s = s.replace("T", " ")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return s


# Google's own upload templates start with a "Parameters:TimeZone=..." row and
# use "#" for comments; both are skipped for every upload CSV.
_CSV_SKIP_PREFIXES = ("Parameters:", "#")

# A 2,000-row upload — the API's per-request cap — is well under 1 MB. The cap
# only stops a stray multi-gigabyte path from being read into memory; it is not
# a row limit.
_MAX_CSV_BYTES = 5 * 1024 * 1024


def _read_upload_csv(csv_path: str) -> tuple[list[list[str]], list[str]]:
    """Read an upload CSV into rows (header first); returns ``(rows, errors)``.

    Local-only by design: the path is read from the machine running AdLoop, so
    the tool refuses in server mode before it gets here. Errors name the file
    and the schema, never file *content* — a hosted runtime can read files the
    caller is not allowed to see.
    """
    import csv
    from pathlib import Path

    path = Path(csv_path).expanduser()
    if not path.is_file():
        return [], [f"CSV not found or not a regular file: {path}"]
    if path.suffix.lower() != ".csv":
        return [], [f"CSV must be a .csv file, got: {path.name}"]
    try:
        size = path.stat().st_size
    except OSError as exc:
        return [], [f"CSV could not be read: {exc.strerror or exc}"]
    if size > _MAX_CSV_BYTES:
        return [], [
            f"CSV is {size / 1_048_576:.1f} MB; the limit is "
            f"{_MAX_CSV_BYTES // 1_048_576} MB. Split the file — Google accepts "
            "at most 2,000 rows per upload request anyway."
        ]

    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            rows = [
                row
                for row in csv.reader(handle)
                if row
                # Only fully blank rows are blank: a row whose *first* cell is
                # empty (e.g. a lead without an email) is real data.
                and any((cell or "").strip() for cell in row)
                and not (row[0] or "").strip().startswith(_CSV_SKIP_PREFIXES)
            ]
    except OSError as exc:
        return [], [f"CSV could not be read: {exc.strerror or exc}"]
    except UnicodeDecodeError:
        return [], ["CSV is not valid UTF-8."]

    if not rows:
        return [], ["CSV is empty (no header row found)"]
    return rows, []


def _column_map(
    header: list[str], expected: list[str]
) -> tuple[dict[str, int], list[str]]:
    """Map required column names to indexes; report what is missing.

    The expected list is our own schema text, so it is safe in an error — the
    header the caller actually sent is not (it is file content).
    """
    columns = [cell.strip() for cell in header]
    missing = [name for name in expected if name not in columns]
    if missing:
        return {}, [
            f"CSV is missing required column(s): {', '.join(missing)}. "
            f"Expected columns: {', '.join(expected)}"
        ]
    return {name: columns.index(name) for name in expected}, []


def _parse_call_conversion_csv(csv_path: str) -> tuple[list[dict], list[str]]:
    """Read the call-conversions CSV (local file) and normalize each row.

    Returns (rows, errors). Rows are dicts keyed by canonical column name; the
    optional ``Parameters:TimeZone=...`` row at the top is skipped, as are
    comment lines.

    The ``caller_id`` (E.164 phone) is retained RAW — Google requires it for
    call-to-click matching and it cannot be hashed. The draft stores it in
    ``ChangePlan.apply_only_payload``, which no preview or audit surface shows.
    """
    rows, errors = _read_upload_csv(csv_path)
    if errors:
        return [], errors

    col, errors = _column_map(rows[0], _EXPECTED_CALL_HEADERS)
    if errors:
        return [], errors

    out: list[dict] = []
    for row_num, raw in enumerate(rows[1:], start=1):
        try:
            value_str = raw[col["Conversion Value"]].strip()
            value = float(value_str) if value_str else 0.0
        except (ValueError, IndexError):
            errors.append(f"Row {row_num}: invalid Conversion Value")
            continue
        out.append({
            "caller_id": _normalize_phone_e164(raw[col["Caller's Phone Number"]]),
            "call_start_time": _normalize_call_timestamp(raw[col["Call Start Time"]]),
            "conversion_name": raw[col["Conversion Name"]].strip(),
            "conversion_time": _normalize_call_timestamp(raw[col["Conversion Time"]]),
            "conversion_value": value,
            "currency_code": (raw[col["Conversion Currency"]].strip().upper() or "USD"),
        })
    return out, errors
def _redact_caller_id(caller_id: str) -> str:
    """Mask an E.164 phone for display/logging: keep the leading digits and
    the last 4, star the middle. e.g. '+15555550142' -> '+155***0142'.
    """
    s = (caller_id or "").strip()
    if not s:
        return ""
    if len(s) <= 6:
        return "***"
    head = s[:4]
    tail = s[-4:]
    return f"{head}***{tail}"


def draft_upload_call_conversions(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    csv_path: str,
    consent: dict | None = None,
) -> dict:
    """Draft an upload of call conversions from CSV — returns a PREVIEW.

    Reads any CSV matching Google Ads' call-upload schema and previews what
    would be sent to ConversionUploadService.UploadCallConversions.

    Required CSV columns: Caller's Phone Number, Call Start Time, Conversion
    Name, Conversion Time, Conversion Value, Conversion Currency. An optional
    ``Parameters:TimeZone=...`` row at the top is ignored.

    The ``Conversion Name`` value MUST exactly match an existing conversion
    action whose type is UPLOAD_CALLS — checked against the account here, so a
    typo fails the preview rather than the upload.

    Rows whose ``caller_id`` is empty or not E.164 are skipped and listed in
    ``skipped_rows``; they could never match. Batches of 2,000 rows are sent
    one request at a time, with partial failure always on (Google requires it),
    and the result carries a per-batch ledger.

    ``consent`` (GDPR/EEA): a dict like
    ``{"ad_user_data": "GRANTED", "ad_personalization": "DENIED"}``. Values:
    GRANTED / DENIED / UNSPECIFIED. Required for EEA traffic. Defaults to
    UNSPECIFIED when omitted.

    PII note: the caller phone number is required raw by Google for matching,
    so the rows live in the plan's ``apply_only_payload`` — apply needs them,
    and neither the preview, the dry-run response nor the audit log ever see
    them. The preview shows counts and redacted sample rows. Call
    confirm_and_apply with the returned plan_id.
    """
    from adloop.runtime import deployment_mode
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    if deployment_mode() == "server":
        return {
            "error": (
                "This tool uploads conversions from a CSV file on the machine "
                "running AdLoop and is not available on the hosted server. "
                "Use the self-hosted AdLoop MCP server for conversion uploads."
            )
        }

    try:
        check_blocked_operation("upload_call_conversions", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    try:
        consent_norm = _consent_from_param(consent)
    except ValueError as e:
        return {"error": str(e)}

    rows, parse_errors = _parse_call_conversion_csv(csv_path)
    if parse_errors and not rows:
        return {"error": "CSV parse failed", "details": parse_errors}
    if not rows:
        return {"error": "CSV contained zero conversion rows"}

    # A call upload without a usable E.164 caller id cannot match anything —
    # Google fails such a row. Report it here instead of uploading a no-op.
    usable: list[dict] = []
    skipped: list[dict] = []
    for row_num, row in enumerate(rows, start=1):
        caller = (row.get("caller_id") or "").strip()
        if caller.startswith("+"):
            usable.append(row)
            continue
        skipped.append({
            "row": row_num,
            "reason": (
                "caller_id is empty"
                if not caller
                else "caller_id is not E.164 (no leading '+'); Google rejects "
                "such rows"
            ),
        })
    if not usable:
        return {
            "error": (
                "No row carries a usable E.164 caller_id. Nothing was planned."
            ),
            "skipped_rows": skipped,
        }
    rows = usable

    distinct_actions = sorted({r["conversion_name"] for r in rows})
    total_value = sum(r["conversion_value"] for r in rows)

    # Validate the action names against the account now, not after the upload
    # ran: a typo in the CSV should not cost a confirmed plan. The resource
    # names travel in the plan, so apply does not query a second time.
    from adloop.ads.client import get_ads_client, normalize_customer_id

    cid = normalize_customer_id(customer_id or config.ads.customer_id)
    try:
        action_resources = _resolve_upload_action(
            get_ads_client(config), cid, distinct_actions,
            expected_type="UPLOAD_CALLS",
        )
    except ValueError as e:
        return {"error": str(e)}

    # Freeze the exact rows apply will upload (caller_id RAW — required by
    # Google). They go into the plan's apply-only payload, which no preview or
    # audit surface shows; the audit log gets the summary below.
    frozen_rows = [
        {
            "caller_id": r["caller_id"],
            "call_start_time": r["call_start_time"],
            "conversion_name": r["conversion_name"],
            "conversion_time": r["conversion_time"],
            "conversion_value": r["conversion_value"],
            "currency_code": r["currency_code"],
        }
        for r in rows
    ]

    plan = ChangePlan(
        operation="upload_call_conversions",
        entity_type="call_conversion_batch",
        entity_id=str(len(rows)),
        customer_id=customer_id,
        # Signal only: on the Google path nothing enforces this flag — it tells
        # the model the upload cannot be undone. The enforced brake is
        # ``safety.two_phase_apply``.
        requires_double_confirm=True,
        changes={
            "row_count": len(rows),
            "total_value": round(total_value, 2),
            "currency_hint": rows[0]["currency_code"] if rows else "USD",
            "skipped_count": len(skipped),
            "skipped_rows": skipped,
            "distinct_conversion_actions": distinct_actions,
            # Resolved at draft time; apply reads them instead of querying again.
            "conversion_actions": action_resources,
            "partial_failure": True,
            "consent": consent_norm,
            "parse_warnings": parse_errors,
            # Display sample uses REDACTED caller ids only.
            "sample_rows": [
                {
                    "caller_id": _redact_caller_id(r["caller_id"]),
                    "call_start_time": r["call_start_time"],
                    "conversion_name": r["conversion_name"],
                    "conversion_value": r["conversion_value"],
                }
                for r in rows[:3]
            ],
        },
        # RAW caller_id lives here (apply needs it, Google cannot hash it);
        # a preview never shows `apply_only_payload`.
        apply_only_payload={"rows": frozen_rows},
    )
    store_plan(plan)
    return plan.to_preview()


def _resolve_upload_action(
    client: object, cid: str, names: list[str], *, expected_type: str
) -> dict[str, str]:
    """Map conversion-action names to resource names, enforcing the type.

    Both uploads look actions up by the name in the CSV's ``Conversion Name``
    column, which is why this is validated at draft time: a typo would
    otherwise surface only after the upload ran. The resource names are stored
    in the plan, so apply does not query again.

    ``UPLOAD_CALLS`` for call uploads, ``UPLOAD_CLICKS`` for Enhanced
    Conversions for Leads (which layers identifier matching on top of click
    conversions).
    """
    if not names:
        return {}

    ga_service = client.get_service("GoogleAdsService")
    quoted = ", ".join(f"'{_gaql_escape(n)}'" for n in names)
    query = (
        "SELECT conversion_action.id, conversion_action.name, "
        "conversion_action.resource_name, conversion_action.type, "
        "conversion_action.status "
        "FROM conversion_action "
        f"WHERE conversion_action.name IN ({quoted}) "
        "AND conversion_action.status != 'REMOVED'"
    )
    response = ga_service.search(customer_id=cid, query=query)

    mapping: dict[str, str] = {}
    wrong_type: list[str] = []
    for row in response:
        ca = row.conversion_action
        ca_type = ca.type_.name if hasattr(ca.type_, "name") else str(ca.type_)
        if ca_type != expected_type:
            wrong_type.append(f"{ca.name} (type={ca_type})")
            continue
        mapping[ca.name] = ca.resource_name

    if wrong_type:
        raise ValueError(
            f"Conversion action(s) are not of type {expected_type}, which this "
            f"upload requires: {wrong_type}. Use "
            f"draft_create_conversion_action(type_='{expected_type}', ...) or an "
            "existing action of that type."
        )
    missing = [n for n in names if n not in mapping]
    if missing:
        raise ValueError(
            f"Conversion action(s) not found: {missing}. Verify the "
            "'Conversion Name' column in the CSV matches an existing action "
            "name exactly."
        )
    return mapping


# Google rejects a single upload request above 2,000 conversions with
# TOO_MANY_CONVERSIONS_IN_REQUEST, so a CSV larger than that is split here.
_MAX_ROWS_PER_REQUEST = 2000


def _upload_in_batches(rows: list[dict], send) -> dict:
    """Send ``rows`` in API-sized batches and report progress per batch.

    ``send(chunk)`` builds the protos for one chunk, calls the upload service
    and returns ``(payload, response)``.

    A failure in batch 3 leaves batches 1-2 uploaded, so the error has to say
    which rows are already in: call uploads have no dedup key at all, and click
    uploads only dedupe on an order id, so a blind retry double-counts whatever
    went through. The error names the first row of the failed batch as the
    resume point.
    """
    total = len(rows)
    batch_total = (total + _MAX_ROWS_PER_REQUEST - 1) // _MAX_ROWS_PER_REQUEST
    ledger: list[dict] = []
    row_errors: list[dict] = []
    success_total = 0

    for index in range(batch_total):
        start = index * _MAX_ROWS_PER_REQUEST
        chunk = rows[start:start + _MAX_ROWS_PER_REQUEST]
        try:
            payload, response = send(chunk)
        except Exception as exc:  # noqa: BLE001 — re-raised with the ledger
            done = sum(batch["uploaded"] for batch in ledger)
            raise RuntimeError(
                f"Upload failed in batch {index + 1} of {batch_total} "
                f"(rows {start + 1}-{start + len(chunk)} of {total}): {exc} "
                f"{done} row(s) from {len(ledger)} batch(es) are already "
                "uploaded and must not be sent again — resume the CSV at row "
                f"{start + 1}."
            ) from exc

        results = list(response.results)
        # Google populates the result row's ``conversion_action`` only for rows
        # that actually matched; echoed identifiers come back for failed rows
        # too, so they are not a success signal.
        success = sum(1 for r in results if getattr(r, "conversion_action", ""))
        success_total += success
        ledger.append({
            "batch": index + 1,
            "first_row": start + 1,
            "last_row": start + len(chunk),
            "uploaded": len(payload),
            "success_count": success,
            "failure_count": len(results) - success,
        })
        partial = getattr(response, "partial_failure_error", None)
        if partial and partial.message:
            row_errors.append({
                "type": "partial_failure",
                "batch": index + 1,
                "message": partial.message,
                "code": getattr(partial, "code", None),
            })

    uploaded_total = sum(batch["uploaded"] for batch in ledger)
    return {
        "uploaded_total": uploaded_total,
        "success_count": success_total,
        "failure_count": uploaded_total - success_total,
        "batch_count": len(ledger),
        "batches": ledger,
        "row_errors": row_errors,
    }


def _apply_upload_call_conversions(
    client: object, cid: str, changes: dict
) -> dict:
    """Execute the call-conversion upload via ConversionUploadService.

    Builds the upload protos from the frozen rows (``apply_only_payload``, put
    there at preview time) — the CSV is NOT re-read. Batches are sent one
    request at a time; a failure says which rows are already uploaded.
    """
    rows = changes.get("rows") or []
    if not rows:
        expected = int(changes.get("row_count") or 0)
        if expected:
            # row_count > 0 without the payload means the plan store dropped
            # ``apply_only_payload``. Fail loudly; an empty upload that reports
            # success is the one outcome nobody would notice.
            raise RuntimeError(
                f"This plan expects {expected} row(s) but carries none: the plan "
                "store did not persist ChangePlan.apply_only_payload. Nothing "
                "was uploaded — draft the upload again."
            )
        return {"error": "Plan contained zero call-conversion rows"}

    action_resources = changes.get("conversion_actions") or {}
    if not action_resources:
        raise RuntimeError(
            "This plan carries no resolved conversion actions; it was created "
            "before the draft validated them. Draft the upload again."
        )
    consent = changes.get("consent")
    upload_service = client.get_service("ConversionUploadService")

    def _send(chunk: list[dict]):
        payload: list = []
        for r in chunk:
            cc = client.get_type("CallConversion")
            cc.caller_id = r["caller_id"]
            cc.call_start_date_time = r["call_start_time"]
            cc.conversion_action = action_resources[r["conversion_name"]]
            cc.conversion_date_time = r["conversion_time"]
            cc.conversion_value = float(r["conversion_value"])
            cc.currency_code = r["currency_code"]
            _apply_consent(client, cc, consent)
            payload.append(cc)
        response = upload_service.upload_call_conversions(
            customer_id=cid,
            conversions=payload,
            # The API requires partial failure on uploads; leaving rows out of
            # the request because one is malformed would be worse.
            partial_failure=True,
        )
        return payload, response

    ledger = _upload_in_batches(rows, _send)
    ledger["conversion_actions_used"] = action_resources
    return ledger


# ---------------------------------------------------------------------------
# Enhanced Conversions for Leads — UploadClickConversions w/ user_identifiers
#
# The CSV here carries RAW PII (email / phone / name). We normalize and
# SHA-256-hash it AT PREVIEW TIME and store only the hashes in the plan.
# Raw PII is never persisted and never reaches the audit log.
# ---------------------------------------------------------------------------

_EXPECTED_EC_HEADERS = [
    "Email",
    "Phone Number",
    "First Name",
    "Last Name",
    "Conversion Name",
    "Conversion Time",
    "Conversion Value",
    "Conversion Currency",
]

# Optional CSV columns — parsed when present, omitted otherwise. Order ID is
# Google's dedup key for ClickConversion uploads: if set, re-uploading the
# same (conversion_action, order_id) pair is idempotent; if absent, re-uploads
# double-count.
_OPTIONAL_EC_HEADERS = [
    "Order ID",
    # Enhanced Conversions for Leads matches name-only rows far better with an
    # address than without: Google expects at least country + postal code next
    # to hashed names. Both are plain values (the proto hashes only the names).
    "Postal Code",
    "Country Code",
]


def _parse_ec_for_leads_csv(csv_path: str) -> tuple[list[dict], list[str]]:
    """Parse the EC-for-Leads CSV (local file) and hash PII at parse time.

    Required columns: Email, Phone Number, First Name, Last Name,
    Conversion Name, Conversion Time, Conversion Value, Conversion Currency.
    Optional: Order ID (Google's dedup key — strongly recommended so
    re-uploads of the same source row don't double-count).

    The Email / Phone Number / First Name / Last Name columns hold RAW PII.
    Each is normalized (email→trim+lowercase, phone→E.164, names→trim+
    lowercase) and then SHA-256-hashed here. Returned rows contain ONLY the
    hashes (``*_sha256`` keys) plus non-PII fields — the raw values never
    leave this function.
    """
    rows, errors = _read_upload_csv(csv_path)
    if errors:
        return [], errors

    col, errors = _column_map(rows[0], _EXPECTED_EC_HEADERS)
    if errors:
        return [], errors
    header = [cell.strip() for cell in rows[0]]
    optional_col = {n: header.index(n) for n in _OPTIONAL_EC_HEADERS if n in header}

    out: list[dict] = []
    for row_num, raw in enumerate(rows[1:], start=1):
        try:
            value_str = raw[col["Conversion Value"]].strip()
            value = float(value_str) if value_str else 0.0
        except (ValueError, IndexError):
            errors.append(f"Row {row_num}: invalid Conversion Value")
            continue
        order_id = ""
        if "Order ID" in optional_col:
            try:
                order_id = raw[optional_col["Order ID"]].strip()
            except IndexError:
                pass
        # Normalize THEN hash. Raw values are discarded immediately.
        email_norm = _normalize_email(raw[col["Email"]])
        raw_phone = raw[col["Phone Number"]]
        phone_norm = _normalize_phone_e164(raw_phone)
        # A phone that is not E.164 hashes to a value Google can never match —
        # sending it would only pad the payload. Keep the row (email/address
        # may still match) but drop the identifier and say so.
        phone_usable = phone_norm.startswith("+")
        first_norm = _normalize_name(raw[col["First Name"]])
        last_norm = _normalize_name(raw[col["Last Name"]])

        def _optional(name: str) -> str:
            return raw[optional_col[name]].strip() if name in optional_col else ""

        out.append({
            "email_sha256": _sha256_hex(email_norm),
            "phone_sha256": _sha256_hex(phone_norm) if phone_usable else "",
            "phone_was_given": bool((raw_phone or "").strip()),
            "phone_usable": phone_usable,
            "first_name_sha256": _sha256_hex(first_norm),
            "last_name_sha256": _sha256_hex(last_norm),
            "postal_code": _optional("Postal Code"),
            "country_code": _optional("Country Code").upper(),
            "conversion_name": raw[col["Conversion Name"]].strip(),
            "conversion_time": _normalize_call_timestamp(
                raw[col["Conversion Time"]]
            ),
            "conversion_value": value,
            "currency_code": (
                raw[col["Conversion Currency"]].strip().upper() or "USD"
            ),
            "order_id": order_id,
        })
    return out, errors


def _match_warnings(
    with_address: int,
    unusable_phones: list[int],
    name_only: list[int],
) -> list[str]:
    """Warnings about identifiers that will not match, said up front.

    Every one of these rows is still uploaded — dropping them silently is how
    an upload reports 100% success and matches nothing.
    """
    warnings: list[str] = []
    if unusable_phones:
        warnings.append(
            f"{len(unusable_phones)} row(s) carry a phone number that is not "
            "E.164 (no leading '+'), so the hashed value cannot match. Those "
            "rows are uploaded without the phone identifier; first affected "
            f"rows: {unusable_phones[:5]}."
        )
    if name_only:
        warnings.append(
            f"{len(name_only)} row(s) have only hashed names — Google usually "
            "needs country and postal code next to them (and email or phone "
            "match better still). Add a 'Country Code' and 'Postal Code' "
            f"column; first affected rows: {name_only[:5]}."
        )
    if with_address == 0:
        warnings.append(
            "No row carries a country + postal code. Enhanced Conversions for "
            "Leads matches far better with them, especially for name-based rows."
        )
    return warnings


def draft_upload_enhanced_conversions_for_leads(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    csv_path: str,
    consent: dict | None = None,
) -> dict:
    """Draft an Enhanced Conversions for Leads upload — returns PREVIEW.

    Reads a CSV of RAW lead PII, normalizes + SHA-256-hashes the
    Email / Phone / First Name / Last Name columns, and previews what will
    be pushed via ConversionUploadService.UploadClickConversions with
    user_identifiers populated. Only the hashes are stored in the plan —
    raw PII never lands in plan.changes or the audit log.

    The target conversion action must be of type UPLOAD_CLICKS (EC for Leads
    layers user-identifier matching on top of click conversions). Works
    retroactively — no "action must exist before the call" constraint like
    UPLOAD_CALLS has.

    Optional columns: ``Order ID`` (Google's dedup key for ClickConversion —
    without it, re-uploads double-count matched conversions), ``Country Code``
    and ``Postal Code`` (sent plain in the address identifier — Enhanced
    Conversions for Leads matches name-based rows far better with them).

    ``consent`` (GDPR/EEA): a dict like
    ``{"ad_user_data": "GRANTED", "ad_personalization": "DENIED"}``. Values:
    GRANTED / DENIED / UNSPECIFIED. Required for EEA traffic. Defaults to
    UNSPECIFIED when omitted.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.runtime import deployment_mode
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    if deployment_mode() == "server":
        return {
            "error": (
                "This tool uploads conversions from a CSV file on the machine "
                "running AdLoop and is not available on the hosted server. "
                "Use the self-hosted AdLoop MCP server for conversion uploads."
            )
        }

    try:
        check_blocked_operation(
            "upload_enhanced_conversions_for_leads", config.safety
        )
    except SafetyViolation as e:
        return {"error": str(e)}

    try:
        consent_norm = _consent_from_param(consent)
    except ValueError as e:
        return {"error": str(e)}

    rows, parse_errors = _parse_ec_for_leads_csv(csv_path)
    if parse_errors and not rows:
        return {"error": "CSV parse failed", "details": parse_errors}
    if not rows:
        return {"error": "CSV contained zero conversion rows"}

    # A row with nothing Google can match on would be uploaded to no effect and
    # counted as a success later. Report it instead of sending it.
    usable: list[dict] = []
    skipped: list[dict] = []
    for row_num, row in enumerate(rows, start=1):
        has_address = bool(row["postal_code"] and row["country_code"])
        has_names = bool(row["first_name_sha256"] and row["last_name_sha256"])
        # Names alone are weak but not useless — Google can match on them, so
        # such a row is uploaded and warned about rather than dropped.
        if row["email_sha256"] or row["phone_sha256"] or has_address or has_names:
            usable.append(row)
            continue
        skipped.append({
            "row": row_num,
            "reason": (
                "phone is not E.164 and the row has no other identifier"
                if row["phone_was_given"]
                else (
                    "no usable identifier — the row has no email, no E.164 "
                    "phone, no country+postal code and no first/last name"
                )
            ),
        })
    if not usable:
        return {
            "error": (
                "No row carries a usable identifier (email, E.164 phone, or "
                "country + postal code). Nothing was planned."
            ),
            "skipped_rows": skipped,
        }
    rows = usable

    distinct_actions = sorted({r["conversion_name"] for r in rows})
    total_value = sum(r["conversion_value"] for r in rows)

    from adloop.ads.client import get_ads_client, normalize_customer_id

    cid = normalize_customer_id(customer_id or config.ads.customer_id)
    try:
        action_resources = _resolve_upload_action(
            get_ads_client(config), cid, distinct_actions,
            expected_type="UPLOAD_CLICKS",
        )
    except ValueError as e:
        return {"error": str(e)}

    with_email = sum(1 for r in rows if r["email_sha256"])
    with_phone = sum(1 for r in rows if r["phone_sha256"])
    with_order_id = sum(1 for r in rows if r.get("order_id"))
    with_address = sum(
        1 for r in rows if r["postal_code"] and r["country_code"]
    )
    unusable_phones = [
        row_num
        for row_num, r in enumerate(rows, start=1)
        if r["phone_was_given"] and not r["phone_usable"]
    ]
    name_only = [
        row_num
        for row_num, r in enumerate(rows, start=1)
        if not r["email_sha256"]
        and not r["phone_sha256"]
        and r["first_name_sha256"]
        and r["last_name_sha256"]
        and not (r["postal_code"] and r["country_code"])
    ]

    dedup_warnings: list[str] = []
    if with_order_id == 0:
        dedup_warnings.append(
            "No Order ID column present. Re-uploading this CSV will "
            "double-count any matched conversions because Google has no "
            "dedup key. Add an Order ID column (e.g. a stable source row "
            "identifier) so re-uploads are idempotent."
        )
    elif with_order_id < len(rows):
        dedup_warnings.append(
            f"Only {with_order_id} of {len(rows)} rows have an Order ID. "
            "Rows without one will double-count on re-upload."
        )

    # Freeze the hashed rows apply will upload. These are already SHA-256
    # hashes — NO raw PII. Safe to persist in the plan and (order_id/value/
    # currency/time/action only) surface in the audit log.
    frozen_rows = [
        {
            "email_sha256": r["email_sha256"],
            "phone_sha256": r["phone_sha256"],
            "first_name_sha256": r["first_name_sha256"],
            "last_name_sha256": r["last_name_sha256"],
            "postal_code": r["postal_code"],
            "country_code": r["country_code"],
            "conversion_name": r["conversion_name"],
            "conversion_time": r["conversion_time"],
            "conversion_value": r["conversion_value"],
            "currency_code": r["currency_code"],
            "order_id": r.get("order_id", ""),
        }
        for r in rows
    ]

    plan = ChangePlan(
        operation="upload_enhanced_conversions_for_leads",
        entity_type="ec_for_leads_batch",
        entity_id=str(len(rows)),
        customer_id=customer_id,
        # Signal only, like the call upload — see the note there.
        requires_double_confirm=True,
        changes={
            "row_count": len(rows),
            "total_value": round(total_value, 2),
            "currency_hint": rows[0]["currency_code"] if rows else "USD",
            "rows_with_email": with_email,
            "rows_with_phone": with_phone,
            "rows_with_order_id": with_order_id,
            "rows_with_address": with_address,
            "skipped_count": len(skipped),
            "skipped_rows": skipped,
            "distinct_conversion_actions": distinct_actions,
            # Resolved at draft time; apply reads them instead of querying again.
            "conversion_actions": action_resources,
            "partial_failure": True,
            "consent": consent_norm,
            "parse_warnings": parse_errors,
            "dedup_warnings": dedup_warnings,
            "match_warnings": _match_warnings(
                with_address, unusable_phones, name_only
            ),
            "sample_rows": [
                {
                    "email_sha256": (r["email_sha256"][:16] + "...")
                    if r["email_sha256"] else "",
                    "phone_sha256": (r["phone_sha256"][:16] + "...")
                    if r["phone_sha256"] else "",
                    "conversion_name": r["conversion_name"],
                    "conversion_value": r["conversion_value"],
                    "conversion_time": r["conversion_time"],
                    "order_id": r.get("order_id", ""),
                }
                for r in rows[:3]
            ],
        },
        # Hash-only rows, but still apply-only payload: the preview summarises
        # them, and the row set is noise in a model's context.
        apply_only_payload={"rows": frozen_rows},
    )
    store_plan(plan)
    return plan.to_preview()


def _apply_upload_enhanced_conversions_for_leads(
    client: object, cid: str, changes: dict
) -> dict:
    """Execute the EC-for-Leads upload via ConversionUploadService.

    Builds the upload protos from the frozen, already-hashed rows
    (``apply_only_payload``) — the CSV is NOT re-read, so no raw PII is touched
    here. Batched like the call upload.
    """
    rows = changes.get("rows") or []
    if not rows:
        expected = int(changes.get("row_count") or 0)
        if expected:
            raise RuntimeError(
                f"This plan expects {expected} row(s) but carries none: the plan "
                "store did not persist ChangePlan.apply_only_payload. Nothing "
                "was uploaded — draft the upload again."
            )
        return {"error": "Plan contained zero EC-for-leads rows"}

    action_resources = changes.get("conversion_actions") or {}
    if not action_resources:
        raise RuntimeError(
            "This plan carries no resolved conversion actions; it was created "
            "before the draft validated them. Draft the upload again."
        )
    consent = changes.get("consent")
    upload_service = client.get_service("ConversionUploadService")

    def _send(chunk: list[dict]):
        payload: list = []
        for r in chunk:
            cc = client.get_type("ClickConversion")
            cc.conversion_action = action_resources[r["conversion_name"]]
            cc.conversion_date_time = r["conversion_time"]
            cc.conversion_value = float(r["conversion_value"])
            cc.currency_code = r["currency_code"]
            if r.get("order_id"):
                cc.order_id = r["order_id"]
            _apply_consent(client, cc, consent)

            # Hashed identifiers only; Google matches them to logged-in users
            # who clicked the ads. A row with no usable identifier never gets
            # here — the draft reports those instead (see `skipped_rows`).
            if r["email_sha256"]:
                uid = client.get_type("UserIdentifier")
                uid.hashed_email = r["email_sha256"]
                cc.user_identifiers.append(uid)
            if r["phone_sha256"]:
                uid = client.get_type("UserIdentifier")
                uid.hashed_phone_number = r["phone_sha256"]
                cc.user_identifiers.append(uid)
            # Address info: Google hashes only the names; country and postal
            # code go in as plain values. A country+postal pair is a usable
            # identifier on its own, which is why name-only rows are worth
            # completing rather than dropping.
            has_address = bool(r.get("postal_code") and r.get("country_code"))
            if (r["first_name_sha256"] and r["last_name_sha256"]) or has_address:
                uid = client.get_type("UserIdentifier")
                if r["first_name_sha256"] and r["last_name_sha256"]:
                    uid.address_info.hashed_first_name = r["first_name_sha256"]
                    uid.address_info.hashed_last_name = r["last_name_sha256"]
                if r.get("postal_code"):
                    uid.address_info.postal_code = r["postal_code"]
                if r.get("country_code"):
                    uid.address_info.country_code = r["country_code"]
                cc.user_identifiers.append(uid)
            payload.append(cc)
        response = upload_service.upload_click_conversions(
            customer_id=cid,
            conversions=payload,
            partial_failure=True,
        )
        return payload, response

    ledger = _upload_in_batches(rows, _send)
    ledger["conversion_actions_used"] = action_resources
    return ledger
