"""Google Ads brand tools — resolve brand names to Google's brand entities.

Backs the brand picker of the Google Ads UI: ``BrandSuggestionService``
matches a free-text name against Google's brand knowledge graph and answers
with the canonical brand ID, display name, state, and associated URLs.

The ID is what brand targeting actually needs. Brand criteria (brand lists /
``BRAND_HINT``) reference the Commercial Knowledge Graph ID, not the display
name — resolving the name first is what makes "steer the account towards
brand X" possible at all. Read-only: no account state is touched.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

# SuggestBrands resolves a single prefix per round trip, and a bulk check is
# therefore one API call per name. The cap keeps a single tool call inside the
# MCP host's timeout while covering the usual audit batch (a campaign's brand
# shortlist, a competitor shortlist, ...). Longer lists are split by the caller.
MAX_BRAND_BATCH = 25

# BrandStateEnum values that can actually be targeted: ENABLED is the normal
# case, UNVERIFIED is customer-scoped but selectable by that customer, and
# APPROVED is an unverified brand business accepted into the global list.
#
# Ranking them together is deliberate — all three can be targeted — but they are
# not the same thing: ``BrandSuggestion.id`` is documented as "CKG MID for
# verified/global scoped brands", so for UNVERIFIED/APPROVED the id is a
# customer-scoped brand id. The state travels in the output; callers that need a
# sure CKG MID look for ENABLED.
_USABLE_STATES = {"ENABLED", "UNVERIFIED", "APPROVED"}
# States whose brand ID is no longer valid. They must never be offered as the
# best match while a live candidate exists — a dead ID in a brand list is worse
# than no match at all.
_DEAD_STATES = {"DEPRECATED", "CANCELLED", "REJECTED"}


def _state_rank(state: str) -> int:
    """0 for usable brands, 1 for dead ones, 2 for states we cannot judge.

    The last bucket also covers UNSPECIFIED/UNKNOWN and any state Google adds
    later: an unknown state must not outrank a brand known to be good.
    """
    if state in _USABLE_STATES:
        return 0
    if state in _DEAD_STATES:
        return 1
    return 2


def suggest_brands(
    config: AdLoopConfig,
    *,
    brand_prefix: str,
    selected_brand_ids: list[str] | None = None,
    customer_id: str = "",
) -> dict:
    """Resolve one brand name to the brands Google recognizes for it.

    ``selected_brand_ids`` mirrors the API field of the same purpose: IDs the
    caller already picked can be handed back so Google keeps them in the
    suggestion set while the prefix narrows.
    """
    prefix = (brand_prefix or "").strip()
    if not prefix:
        return {"error": "brand_prefix is required — pass the brand name to look up."}

    cid = customer_id or config.ads.customer_id
    brands = _fetch_brand_suggestions(
        config,
        customer_id=cid,
        brand_prefix=prefix,
        selected_brand_ids=selected_brand_ids or [],
    )
    return {
        "customer_id": _digits(cid),
        "brand_prefix": prefix,
        "brand_count": len(brands),
        "brands": brands,
    }


def check_brand_names(
    config: AdLoopConfig,
    *,
    brand_names: list[str],
    customer_id: str = "",
) -> dict:
    """Check several brand names against Google's brand knowledge graph.

    One ``SuggestBrands`` call per name. Names Google does not know come back
    as ``status: "no_match"`` with an empty candidate list — that is a normal
    answer, not an error. An API failure aborts the whole batch and surfaces
    as the regular structured error, so an expired token cannot hide behind a
    half-filled result list.
    """
    names = _clean_brand_names(brand_names)
    if not names:
        return {"error": "brand_names is required — pass the brand names to check."}
    if len(names) > MAX_BRAND_BATCH:
        return {
            "error": (
                f"Too many brand names in one call ({len(names)}). "
                f"SuggestBrands answers one prefix per request — split into "
                f"batches of at most {MAX_BRAND_BATCH}."
            )
        }

    cid = customer_id or config.ads.customer_id
    results: list[dict] = []
    for name in names:
        brands = _fetch_brand_suggestions(
            config, customer_id=cid, brand_prefix=name, selected_brand_ids=[]
        )
        best, exact = _best_match(name, brands)
        results.append(
            {
                "query": name,
                "status": "matched" if best else "no_match",
                "exact_match": exact,
                "brand": best,
                "candidates": brands,
            }
        )

    matched = sum(1 for r in results if r["status"] == "matched")
    return {
        "customer_id": _digits(cid),
        "checked": len(results),
        "matched": matched,
        "no_match": len(results) - matched,
        "results": results,
        "note": (
            "brand.id is the Commercial Knowledge Graph ID that brand "
            "criteria (brand lists / BRAND_HINT) target. Match exact names "
            "first; candidates are Google's own suggestions, not guarantees."
        ),
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _fetch_brand_suggestions(
    config: AdLoopConfig,
    *,
    customer_id: str,
    brand_prefix: str,
    selected_brand_ids: list[str],
) -> list[dict[str, Any]]:
    """One ``BrandSuggestionService.SuggestBrands`` round trip, flattened."""
    from adloop.ads.client import call_with_retry, get_ads_client, normalize_customer_id

    client = get_ads_client(config)
    service = client.get_service("BrandSuggestionService")
    request = client.get_type("SuggestBrandsRequest")
    request.customer_id = normalize_customer_id(customer_id)
    request.brand_prefix = brand_prefix
    for brand_id in selected_brand_ids:
        request.selected_brands.append(str(brand_id))

    response = call_with_retry(service.suggest_brands, request=request)

    brand_state = client.enums.BrandStateEnum
    brands: list[dict[str, Any]] = []
    for suggestion in response.brands:
        state = brand_state(suggestion.state).name
        brands.append(
            {
                "id": suggestion.id,
                "name": suggestion.name,
                "state": state,
                "urls": list(suggestion.urls),
            }
        )
    return brands


def _clean_brand_names(brand_names: list[str] | None) -> list[str]:
    """Trim, drop blanks, and de-duplicate while keeping the caller's order."""
    seen: set[str] = set()
    names: list[str] = []
    for raw in brand_names or []:
        name = (raw or "").strip()
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        names.append(name)
    return names


def _best_match(query: str, brands: list[dict[str, Any]]) -> tuple[dict | None, bool]:
    """Pick the suggestion that best answers *query*.

    Ranking is deliberately shallow, but state comes first: a brand Google has
    retired is never the answer while a live candidate is on the table, even
    when the retired one matches the query character for character. Within the
    same state tier an exact name match wins, then Google's own order. Anything
    deeper would imply a confidence the API does not provide.
    """
    if not brands:
        return None, False

    normalized = _normalize(query)
    ranked = sorted(
        brands,
        key=lambda brand: (
            _state_rank(brand["state"]),
            0 if _normalize(brand["name"]) == normalized else 1,
        ),
    )
    best = ranked[0]
    return best, _normalize(best["name"]) == normalized


def _normalize(name: str) -> str:
    """Case- and punctuation-insensitive form used for exact-match checks."""
    return re.sub(r"[\W_]+", "", (name or "").casefold())


def _digits(customer_id: str) -> str:
    return (customer_id or "").replace("-", "")
