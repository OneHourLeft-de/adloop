"""Tests for the read-only brand suggestion tools (BrandSuggestionService)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.ads.googleads.client import GoogleAdsClient

from adloop.ads import brands, write
from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig


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


# ---------------------------------------------------------------------------
# Brand lists — read side
# ---------------------------------------------------------------------------


def _patch_query(monkeypatch, rows):
    import adloop.ads.gaql as gaql

    seen = {}

    def _fake(_config, customer_id, query):
        seen["customer_id"] = customer_id
        seen["query"] = query
        return [dict(row) for row in rows]

    monkeypatch.setattr(gaql, "execute_query", _fake)
    return seen


class TestBrandListReads:
    def test_get_brand_lists_asks_for_the_brands_shared_set_type(self, config, monkeypatch):
        seen = _patch_query(monkeypatch, [
            {"shared_set.id": 42, "shared_set.name": "Competitors", "shared_set.member_count": 3}
        ])

        result = brands.get_brand_lists(config)

        assert result["total_lists"] == 1
        assert result["brand_lists"][0]["shared_set.name"] == "Competitors"
        assert "shared_set.type = 'BRANDS'" in seen["query"]

    def test_get_brand_list_brands_adds_the_removal_handle(self, config, monkeypatch):
        _patch_query(monkeypatch, [
            {
                "shared_set.id": 42,
                "shared_criterion.criterion_id": 777,
                "shared_criterion.brand.entity_id": "/m/01n5j",
                "shared_criterion.brand.display_name": "NoWayOut",
            }
        ])

        result = brands.get_brand_list_brands(config, shared_set_id="42")

        assert result["total_brands"] == 1
        assert result["brands"][0]["resource_id"] == "42~777"

    def test_get_brand_list_brands_rejects_a_non_numeric_id(self, config, monkeypatch):
        seen = _patch_query(monkeypatch, [])

        result = brands.get_brand_list_brands(config, shared_set_id="/m/1")

        assert "must be a numeric ID" in result["error"]
        assert seen == {}

    def test_get_brand_list_campaigns_filters_and_labels_the_role(
        self, config, monkeypatch
    ):
        _patch_query(monkeypatch, [
            {
                "campaign.id": 111,
                "campaign.name": "Search Brand",
                "campaign_criterion.criterion_id": 555,
                "campaign_criterion.negative": True,
                "campaign_criterion.brand_list.shared_set": (
                    "customers/1234567890/sharedSets/42"
                ),
            },
            {
                "campaign.id": 222,
                "campaign.name": "Other",
                "campaign_criterion.criterion_id": 556,
                "campaign_criterion.negative": False,
                "campaign_criterion.brand_list.shared_set": (
                    "customers/1234567890/sharedSets/99"
                ),
            },
        ])

        result = brands.get_brand_list_campaigns(config, shared_set_id="42")

        assert result["total_attachments"] == 1
        entry = result["campaigns"][0]
        assert entry["campaign.id"] == 111
        assert entry["resource_id"] == "111~555"
        assert entry["role"] == "excluded"


# ---------------------------------------------------------------------------
# Brand lists — write side (drafts)
# ---------------------------------------------------------------------------


class TestBrandListDrafts:
    def test_propose_brand_list_builds_a_preview_plan(self, config):
        result = write.propose_brand_list(
            config, list_name="Competitors", brand_ids=["/m/1", "/m/1", "/m/2"]
        )

        assert result["status"] == "PENDING_CONFIRMATION"
        assert result["operation"] == "create_brand_list"
        assert result["changes"]["brand_ids"] == ["/m/1", "/m/2"]
        assert result["changes"]["negative"] is True
        assert result["changes"]["campaign_ids"] == []

    def test_propose_brand_list_requires_a_name_and_at_least_one_brand(self, config):
        result = write.propose_brand_list(config, list_name="", brand_ids=[])

        assert result["error"] == "Validation failed"
        assert "list_name is required" in result["details"]
        assert "At least one brand_id is required" in result["details"]

    def test_propose_brand_list_rejects_non_numeric_campaign_ids(self, config):
        result = write.propose_brand_list(
            config, list_name="L", brand_ids=["/m/1"], campaign_ids=["abc"]
        )

        assert "must be numeric" in " ".join(result["details"])

    def test_propose_brand_list_carries_deduped_campaigns_and_the_role(self, config):
        result = write.propose_brand_list(
            config,
            list_name="L",
            brand_ids=["/m/1"],
            campaign_ids=["111", "111", "222"],
            negative=False,
        )

        assert result["changes"]["campaign_ids"] == ["111", "222"]
        assert result["changes"]["negative"] is False

    def test_blocked_operation_is_refused(self):
        blocked = AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(blocked_operations=["create_brand_list"]),
        )

        result = write.propose_brand_list(blocked, list_name="L", brand_ids=["/m/1"])

        assert "blocked by configuration" in result["error"]

    def test_add_to_brand_list_requires_a_numeric_shared_set(self, config):
        result = write.add_to_brand_list(
            config, shared_set_id="Competitors", brand_ids=["/m/1"]
        )

        assert "must be a numeric ID" in " ".join(result["details"])

    def test_remove_from_brand_list_validates_criterion_ids(self, config):
        plan = write.remove_from_brand_list(
            config, shared_set_id="42", criterion_ids=["7"]
        )
        assert plan["operation"] == "remove_from_brand_list"
        assert plan["changes"]["criterion_ids"] == ["7"]

        bad = write.remove_from_brand_list(
            config, shared_set_id="42", criterion_ids=["abc"]
        )
        assert "must be numeric" in " ".join(bad["details"])

    def test_attach_and_detach_need_campaigns(self, config):
        attach = write.attach_brand_list_to_campaigns(config, shared_set_id="42")
        detach = write.detach_brand_list_from_campaigns(config, shared_set_id="42")

        assert "At least one campaign_id is required" in attach["details"]
        assert "At least one campaign_id is required" in detach["details"]

    def test_attach_defaults_to_exclusion(self, config):
        result = write.attach_brand_list_to_campaigns(
            config, shared_set_id="42", campaign_ids=["111"]
        )

        assert result["operation"] == "attach_brand_list_to_campaigns"
        assert result["changes"]["negative"] is True


# ---------------------------------------------------------------------------
# Brand lists — write side (appliers)
# ---------------------------------------------------------------------------


class _FakeWriteClient:
    """Real proto types, fake services — mirrors tests/test_ads_write.py."""

    def __init__(self, search_rows=None, criterion_results=None):
        base = GoogleAdsClient(
            credentials=None,
            developer_token="test-token",
            use_proto_plus=True,
            version=GOOGLE_ADS_API_VERSION,
        )
        self.enums = base.enums
        self.get_type = base.get_type
        self.shared_set_operations = []
        self.criterion_requests = []
        self.campaign_criterion_requests = []
        self.queries = []
        self._search_rows = search_rows or []
        self._criterion_results = criterion_results
        # Built once: tests patch individual methods on these objects, so
        # handing out a fresh namespace per call would silently lose the patch.
        self._services = {
            "SharedSetService": SimpleNamespace(
                mutate_shared_sets=self._mutate_shared_sets,
                shared_set_path=lambda cid, sid: f"customers/{cid}/sharedSets/{sid}",
            ),
            "SharedCriterionService": SimpleNamespace(
                mutate_shared_criteria=self._mutate_shared_criteria
            ),
            "CampaignCriterionService": SimpleNamespace(
                mutate_campaign_criteria=self._mutate_campaign_criteria
            ),
            "CampaignService": SimpleNamespace(
                campaign_path=lambda cid, campaign_id: (
                    f"customers/{cid}/campaigns/{campaign_id}"
                )
            ),
            "GoogleAdsService": SimpleNamespace(search=self._search),
        }

    def get_service(self, name):
        return self._services[name]

    def _mutate_shared_sets(self, customer_id, operations):
        self.shared_set_operations.extend(operations)
        return SimpleNamespace(
            results=[
                SimpleNamespace(
                    resource_name=f"customers/{customer_id}/sharedSets/999"
                )
            ]
        )

    def _mutate_shared_criteria(self, customer_id=None, operations=None, request=None):
        if request is not None:
            self.criterion_requests.append(request)
            operations = list(request.operations)
        else:
            self.criterion_requests.append(operations)
        if self._criterion_results is not None:
            return SimpleNamespace(results=self._criterion_results)
        return SimpleNamespace(
            results=[
                SimpleNamespace(
                    resource_name=f"customers/{customer_id}/sharedCriteria/999~{i}"
                )
                for i, _ in enumerate(operations)
            ]
        )

    def _mutate_campaign_criteria(self, request):
        self.campaign_criterion_requests.append(request)
        return SimpleNamespace(
            results=[
                SimpleNamespace(
                    resource_name=f"customers/{request.customer_id}/campaignCriteria/{i}"
                )
                for i, _ in enumerate(request.operations)
            ]
        )

    def _search(self, customer_id, query):
        self.queries.append(query)
        return list(self._search_rows)


class TestBrandListAppliers:
    def test_create_brand_list_writes_a_brands_shared_set(self):
        client = _FakeWriteClient()

        result = write._apply_create_brand_list(
            client,
            "1234567890",
            {
                "list_name": "Competitors",
                "brand_ids": ["/m/1", "/m/2"],
                "campaign_ids": [],
                "negative": True,
            },
        )

        shared_set = client.shared_set_operations[0].create
        assert shared_set.name == "Competitors"
        assert shared_set.type_ == client.enums.SharedSetTypeEnum.BRANDS

        criteria = client.criterion_requests[0]
        assert [op.create.brand.entity_id for op in criteria] == ["/m/1", "/m/2"]
        assert all(
            op.create.shared_set == "customers/1234567890/sharedSets/999"
            for op in criteria
        )

        assert result["shared_set_resource"] == "customers/1234567890/sharedSets/999"
        assert result["brand_count"] == 2
        assert "attachment" not in result

    def test_create_brand_list_attaches_with_the_negative_flag(self):
        client = _FakeWriteClient()

        result = write._apply_create_brand_list(
            client,
            "1234567890",
            {
                "list_name": "Own brand",
                "brand_ids": ["/m/1"],
                "campaign_ids": ["111"],
                "negative": False,
            },
        )

        criterion = client.campaign_criterion_requests[0].operations[0].create
        assert criterion.campaign == "customers/1234567890/campaigns/111"
        assert criterion.brand_list.shared_set == "customers/1234567890/sharedSets/999"
        assert criterion.negative is False
        assert result["attachment"]["negative"] is False

    def test_attach_reports_per_campaign_partial_failure(self):
        client = _FakeWriteClient()

        def _mutate(request):
            client.campaign_criterion_requests.append(request)
            return SimpleNamespace(
                results=[
                    SimpleNamespace(resource_name="customers/1/campaignCriteria/111~1"),
                    SimpleNamespace(resource_name=""),
                ]
            )

        client.get_service("CampaignCriterionService").mutate_campaign_criteria = _mutate

        result = write._apply_attach_brand_list_to_campaigns(
            client,
            "1234567890",
            {"shared_set_id": "42", "campaign_ids": ["111", "222"], "negative": True},
        )

        assert result["campaign_count"] == 1
        assert result["partial_failure"] is True
        assert result["failed_campaigns"][0]["campaign_id"] == "222"

    def test_add_to_brand_list_writes_shared_criteria(self):
        client = _FakeWriteClient()

        result = write._apply_add_to_brand_list(
            client,
            "1234567890",
            {"shared_set_id": "42", "brand_ids": ["/m/7"]},
        )

        operations = client.criterion_requests[0]
        assert operations[0].create.shared_set == "customers/1234567890/sharedSets/42"
        assert operations[0].create.brand.entity_id == "/m/7"
        assert result["brand_count"] == 1

    def test_remove_from_brand_list_removes_shared_criteria(self):
        client = _FakeWriteClient()

        result = write._apply_remove_from_brand_list(
            client,
            "1234567890",
            {"shared_set_id": "42", "criterion_ids": ["555"]},
        )

        request = client.criterion_requests[0]
        assert request.operations[0].remove == "customers/1234567890/sharedCriteria/42~555"
        assert result["removed_count"] == 1

    def test_detach_only_removes_the_matching_brand_list(self):
        row_match = SimpleNamespace(
            campaign=SimpleNamespace(id=111),
            campaign_criterion=SimpleNamespace(
                criterion_id=555,
                brand_list=SimpleNamespace(shared_set="customers/1/sharedSets/42"),
            ),
        )
        row_other_list = SimpleNamespace(
            campaign=SimpleNamespace(id=111),
            campaign_criterion=SimpleNamespace(
                criterion_id=556,
                brand_list=SimpleNamespace(shared_set="customers/1/sharedSets/99"),
            ),
        )
        client = _FakeWriteClient(search_rows=[row_match, row_other_list])

        result = write._apply_detach_brand_list_from_campaigns(
            client,
            "1234567890",
            {"shared_set_id": "42", "campaign_ids": ["111", "222"]},
        )

        operation = client.campaign_criterion_requests[0].operations[0]
        assert operation.remove == "customers/1234567890/campaignCriteria/111~555"
        assert result["removed_count"] == 1
        assert result["not_attached"] == ["222"]

    def test_detach_without_matches_does_not_call_the_api(self):
        client = _FakeWriteClient(search_rows=[])

        result = write._apply_detach_brand_list_from_campaigns(
            client,
            "1234567890",
            {"shared_set_id": "42", "campaign_ids": ["111"]},
        )

        assert result["removed_count"] == 0
        assert result["not_attached"] == ["111"]
        assert client.campaign_criterion_requests == []


class TestBrandListToolRegistration:
    @pytest.mark.asyncio
    async def test_read_write_split_and_tags(self):
        from adloop.server import mcp

        tools = {t.name: t for t in await mcp.list_tools()}
        read_only = {
            "get_brand_lists", "get_brand_list_brands", "get_brand_list_campaigns",
        }
        writes = {
            "propose_brand_list", "add_to_brand_list",
            "attach_brand_list_to_campaigns",
        }
        destructive = {
            "remove_from_brand_list", "detach_brand_list_from_campaigns",
        }

        for name in read_only:
            assert tools[name].annotations.read_only_hint is True, name
        for name in writes:
            assert tools[name].annotations.read_only_hint is False, name
            assert tools[name].annotations.destructive_hint is False, name
        for name in destructive:
            assert tools[name].annotations.destructive_hint is True, name

        for name in read_only | writes | destructive:
            assert set(tools[name].tags) == {"ads"}, name

    @pytest.mark.asyncio
    async def test_schema_documents_the_parameters_that_matter(self):
        """FastMCP only lifts an `Args:` section into the JSON schema.

        Without it a client sees ``brand_ids: array<string>`` with no hint that
        the values are Commercial Knowledge Graph MIDs — the one mistake that
        makes every brand-list write fail.
        """
        from adloop.server import mcp

        expected = {
            "get_brand_list_brands": ["shared_set_id"],
            "propose_brand_list": ["list_name", "brand_ids", "campaign_ids", "negative"],
            "add_to_brand_list": ["shared_set_id", "brand_ids"],
            "remove_from_brand_list": ["shared_set_id", "criterion_ids"],
            "attach_brand_list_to_campaigns": ["shared_set_id", "campaign_ids", "negative"],
            "detach_brand_list_from_campaigns": ["shared_set_id", "campaign_ids"],
        }
        tools = {t.name: t for t in await mcp.list_tools()}

        for tool_name, params in expected.items():
            properties = tools[tool_name].parameters["properties"]
            for param in params:
                assert properties[param].get("description"), f"{tool_name}.{param}"

        brand_ids = tools["propose_brand_list"].parameters["properties"]["brand_ids"]
        assert "MID" in brand_ids["description"]


class TestBrandListSafety:
    """Requests from the review of PR #75."""

    def test_remove_from_brand_list_asks_for_double_confirmation(self, config):
        result = write.remove_from_brand_list(
            config, shared_set_id="42", criterion_ids=["7"]
        )

        assert result["requires_double_confirm"] is True
