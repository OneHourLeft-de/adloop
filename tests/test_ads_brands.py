"""Tests for the read-only brand suggestion tools (BrandSuggestionService)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from adloop.ads import brands
from adloop.config import AdLoopConfig, AdsConfig


@pytest.fixture
def config() -> AdLoopConfig:
    return AdLoopConfig(ads=AdsConfig(customer_id="123-456-7890"))


class _BrandState:
    """Stand-in for ``client.enums.BrandStateEnum`` (int -> member)."""

    _NAMES = {
        0: "UNSPECIFIED",
        1: "UNKNOWN",
        2: "ENABLED",
        3: "DEPRECATED",
        4: "UNVERIFIED",
    }

    def __call__(self, value: int):
        return SimpleNamespace(name=self._NAMES.get(value, "UNKNOWN"))


def _suggestion(brand_id, name, *, state=2, urls=()):
    return SimpleNamespace(id=brand_id, name=name, state=state, urls=list(urls))


class _FakeService:
    def __init__(self, responses):
        self._responses = responses
        self.requests = []

    def suggest_brands(self, request):
        self.requests.append(request)
        payload = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(payload, Exception):
            raise payload
        return SimpleNamespace(brands=payload)


class _FakeClient:
    def __init__(self, responses):
        self.service = _FakeService(responses)
        self.enums = SimpleNamespace(BrandStateEnum=_BrandState())

    def get_service(self, name):
        assert name == "BrandSuggestionService"
        return self.service

    def get_type(self, name):
        assert name == "SuggestBrandsRequest"
        return SimpleNamespace(customer_id="", brand_prefix="", selected_brands=[])


def _patch_client(monkeypatch, responses):
    client = _FakeClient(responses)
    monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _cfg: client)
    return client


class TestSuggestBrands:
    def test_flattens_response_and_strips_dashes_from_customer_id(
        self, config, monkeypatch
    ):
        client = _patch_client(monkeypatch, [[
            _suggestion("brand-1", "EscapeGame München", urls=["escapegame-muenchen.de"]),
            _suggestion("brand-2", "Escape Game Berlin", state=4),
        ]])

        result = brands.suggest_brands(config, brand_prefix="EscapeGame München")

        assert result["customer_id"] == "1234567890"
        assert result["brand_prefix"] == "EscapeGame München"
        assert result["brand_count"] == 2
        assert result["brands"][0] == {
            "id": "brand-1",
            "name": "EscapeGame München",
            "state": "ENABLED",
            "urls": ["escapegame-muenchen.de"],
        }
        assert result["brands"][1]["state"] == "UNVERIFIED"
        assert client.service.requests[0].customer_id == "1234567890"

    def test_selected_brand_ids_are_forwarded(self, config, monkeypatch):
        client = _patch_client(monkeypatch, [[]])

        brands.suggest_brands(
            config,
            brand_prefix="NoWayOut",
            selected_brand_ids=["brand-9", "brand-10"],
        )

        assert list(client.service.requests[0].selected_brands) == ["brand-9", "brand-10"]

    def test_blank_prefix_is_rejected_without_touching_the_api(
        self, config, monkeypatch
    ):
        client = _patch_client(monkeypatch, [[]])

        result = brands.suggest_brands(config, brand_prefix="   ")

        assert "brand_prefix is required" in result["error"]
        assert client.service.requests == []

    def test_no_match_returns_an_empty_brand_list(self, config, monkeypatch):
        _patch_client(monkeypatch, [[]])

        result = brands.suggest_brands(config, brand_prefix="NoWayOut")

        assert result["brand_count"] == 0
        assert result["brands"] == []


class TestCheckBrandNames:
    def test_marks_exact_matches_and_unknown_names(self, config, monkeypatch):
        _patch_client(monkeypatch, [
            [_suggestion("brand-1", "EscapeGame München")],
            [],
        ])

        result = brands.check_brand_names(
            config, brand_names=["EscapeGame München", "NoWayOut"]
        )

        assert result["checked"] == 2
        assert result["matched"] == 1
        assert result["no_match"] == 1

        first, second = result["results"]
        assert first["status"] == "matched"
        assert first["exact_match"] is True
        assert first["brand"]["id"] == "brand-1"
        assert first["candidates"][0]["name"] == "EscapeGame München"

        assert second["status"] == "no_match"
        assert second["exact_match"] is False
        assert second["brand"] is None
        assert second["candidates"] == []

    def test_case_and_punctuation_do_not_break_the_exact_match(
        self, config, monkeypatch
    ):
        _patch_client(monkeypatch, [[_suggestion("brand-1", "Mystery-Rooms")]])

        result = brands.check_brand_names(config, brand_names=["mystery rooms"])

        assert result["results"][0]["exact_match"] is True

    def test_usable_state_outranks_a_deprecated_exact_match(
        self, config, monkeypatch
    ):
        _patch_client(monkeypatch, [[
            _suggestion("brand-old", "Hunt4Hint", state=3),
            _suggestion("brand-new", "Hunt4Hint Escape", state=2),
        ]])

        result = brands.check_brand_names(config, brand_names=["Hunt4Hint Escape"])

        entry = result["results"][0]
        assert entry["exact_match"] is True
        assert entry["brand"]["id"] == "brand-new"
        assert len(entry["candidates"]) == 2

    def test_a_retired_brand_is_never_the_best_match(self, config, monkeypatch):
        """Google suggests by prefix, so the dead candidate here is the exact
        name — and it still must not win: a retired ID in a brand list is worse
        than an imperfect name match."""
        _patch_client(monkeypatch, [[
            _suggestion("brand-old", "NoWayOut", state=7),
            _suggestion("brand-new", "No Way Out München", state=2),
        ]])

        result = brands.check_brand_names(config, brand_names=["NoWayOut"])

        entry = result["results"][0]
        assert entry["status"] == "matched"
        assert entry["brand"]["id"] == "brand-new"
        assert entry["exact_match"] is False
        assert [c["id"] for c in entry["candidates"]] == ["brand-old", "brand-new"]

    def test_live_states_are_ordered_before_states_we_cannot_judge(
        self, config, monkeypatch
    ):
        _patch_client(monkeypatch, [[
            _suggestion("brand-unknown", "MysteryRooms", state=1),
            _suggestion("brand-dead", "Mystery Rooms Berlin", state=6),
            _suggestion("brand-live", "Mystery Rooms", state=4),
        ]])

        result = brands.check_brand_names(config, brand_names=["Mystery Rooms"])

        entry = result["results"][0]
        assert entry["brand"]["id"] == "brand-live"
        assert entry["exact_match"] is True

    def test_an_unknown_state_does_not_outrank_a_live_brand(self, config, monkeypatch):
        """A state Google adds later is treated as unknown, never as better
        than ENABLED."""
        _patch_client(monkeypatch, [[
            _suggestion("brand-new-state", "Unlock", state=99),
            _suggestion("brand-live", "Unlock Escape", state=2),
        ]])

        result = brands.check_brand_names(config, brand_names=["Unlock Escape"])

        assert result["results"][0]["brand"]["id"] == "brand-live"

    def test_duplicates_are_collapsed_and_blank_names_dropped(
        self, config, monkeypatch
    ):
        client = _patch_client(monkeypatch, [[_suggestion("brand-1", "Unlock Escape")]])

        result = brands.check_brand_names(
            config, brand_names=["Unlock Escape", "  ", "unlock escape"]
        )

        assert result["checked"] == 1
        assert len(client.service.requests) == 1

    def test_empty_input_is_rejected(self, config, monkeypatch):
        client = _patch_client(monkeypatch, [[]])

        result = brands.check_brand_names(config, brand_names=[])

        assert "brand_names is required" in result["error"]
        assert client.service.requests == []

    def test_batch_cap_is_enforced(self, config, monkeypatch):
        client = _patch_client(monkeypatch, [[]])

        result = brands.check_brand_names(
            config, brand_names=[f"Brand {i}" for i in range(brands.MAX_BRAND_BATCH + 1)]
        )

        assert "Too many brand names" in result["error"]
        assert client.service.requests == []

    def test_api_failure_aborts_the_batch(self, config, monkeypatch):
        _patch_client(monkeypatch, [
            [_suggestion("brand-1", "Unlock Escape")],
            RuntimeError("invalid_grant: Token has been expired or revoked."),
        ])

        with pytest.raises(RuntimeError):
            brands.check_brand_names(
                config, brand_names=["Unlock Escape", "Hunt4Hint"]
            )


class TestBrandToolRegistration:
    @pytest.mark.asyncio
    async def test_tools_are_read_only_and_carry_the_ads_tag(self):
        from adloop.server import mcp

        tools = {t.name: t for t in await mcp.list_tools()}
        for name in ("suggest_brands", "check_brand_names"):
            tool = tools[name]
            assert tool.annotations.readOnlyHint is True, name
            assert set(tool.tags) == {"ads"}, name

    @pytest.mark.asyncio
    async def test_suggest_brands_exposes_customer_id_and_prefix(self):
        from adloop.server import mcp

        tool = {t.name: t for t in await mcp.list_tools()}["suggest_brands"]
        properties = tool.parameters["properties"]
        assert {"brand_prefix", "selected_brand_ids", "customer_id"} <= set(properties)


class TestStateRanking:
    """The tiers of ``_state_rank`` — pinned so a later edit cannot blur them."""

    def test_enabled_customer_scoped_and_dead_are_three_tiers(self):
        assert brands._state_rank("ENABLED") == 0
        assert brands._state_rank("UNVERIFIED") == 0
        assert brands._state_rank("APPROVED") == 0
        for dead in ("DEPRECATED", "CANCELLED", "REJECTED"):
            assert brands._state_rank(dead) == 1, dead
        for unknown in ("UNSPECIFIED", "UNKNOWN", "SOMETHING_NEW"):
            assert brands._state_rank(unknown) == 2, unknown

    def test_a_customer_scoped_brand_can_still_win(self, config, monkeypatch):
        """UNVERIFIED is targeting-capable, so it beats a retired brand."""
        _patch_client(monkeypatch, [[
            _suggestion("brand-dead", "Unlock Escape", state=3),
            _suggestion("brand-scoped", "Unlock", state=4),
        ]])

        entry = brands.check_brand_names(config, brand_names=["Unlock"])["results"][0]

        assert entry["brand"]["id"] == "brand-scoped"
        assert entry["brand"]["state"] == "UNVERIFIED"
