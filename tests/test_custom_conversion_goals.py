"""Tests for the custom conversion goal tools (create/update/assign/clear)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.ads.googleads.client import GoogleAdsClient

from adloop.ads import custom_conversion_goals as cg
from adloop.ads import write
from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig

CUSTOMER = "1586230693"
CAMPAIGN = "16454984318"
EXISTING_GOAL = "6459279835"

BUY = "6586227984"
CALL_AD = "7810016065"
CALL_SITE = "7810016851"
LEAD_SITE = "7812006408"
REMOVED_ACTION = "1111111111"


@pytest.fixture
def config() -> AdLoopConfig:
    return AdLoopConfig(ads=AdsConfig(customer_id=CUSTOMER))


def _goal_row(goal_id=EXISTING_GOAL, name="OHL | Kauf + qualifizierte Leads",
              status="ENABLED", actions=(BUY, CALL_AD, CALL_SITE, LEAD_SITE)):
    return SimpleNamespace(
        custom_conversion_goal=SimpleNamespace(
            id=int(goal_id),
            name=name,
            status=status,
            conversion_actions=[f"customers/{CUSTOMER}/conversionActions/{a}" for a in actions],
        )
    )


def _config_row(level="CUSTOMER", goal="", campaign=CAMPAIGN, status="ENABLED"):
    return SimpleNamespace(
        campaign=SimpleNamespace(id=int(campaign), name="Escape Room München (ohne Display)",
                                 status=status),
        conversion_goal_campaign_config=SimpleNamespace(
            goal_config_level=level, custom_conversion_goal=goal
        ),
    )


def _action_row(action_id, *, name="Action", status="ENABLED"):
    return SimpleNamespace(
        conversion_action=SimpleNamespace(
            id=int(action_id), name=name, status=status, type="WEBPAGE"
        )
    )


class _ReadClient:
    def __init__(self, *, goals=None, configs=None, actions=None, fail_part=""):
        self._goals = goals if goals is not None else [_goal_row()]
        self._configs = configs if configs is not None else [_config_row()]
        self._actions = actions if actions is not None else [
            _action_row(BUY), _action_row(CALL_AD), _action_row(CALL_SITE),
            _action_row(LEAD_SITE), _action_row(REMOVED_ACTION, status="REMOVED"),
        ]
        self._fail_part = fail_part
        self.queries: list[str] = []

    def _search(self, customer_id, query):
        self.queries.append(query)
        if "FROM custom_conversion_goal" in query:
            if self._fail_part == "custom_goals":
                raise RuntimeError("goal query failed")
            return self._goals
        if "FROM conversion_goal_campaign_config" in query:
            return self._configs
        if "FROM conversion_action" in query:
            return self._actions
        raise AssertionError(query)

    def get_service(self, name):
        return SimpleNamespace(search=self._search)


def _patch_read(monkeypatch, client):
    monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _config: client)
    return client


class TestReadTool:
    def test_a_non_numeric_campaign_id_never_reaches_gaql(self, config, monkeypatch):
        """The id is interpolated into the query text — same rule as in #77."""

        def _no_client(*_args, **_kwargs):
            raise AssertionError("no client may be built for invalid input")

        monkeypatch.setattr("adloop.ads.client.get_ads_client", _no_client)

        result = cg.get_custom_conversion_goals(
            config, campaign_id="1 OR campaign.id > 0"
        )

        assert result == {"error": "campaign_id must be a numeric ID"}

    def test_read_state_rejects_a_non_numeric_campaign_id(self):
        with pytest.raises(ValueError, match="numeric"):
            cg.read_state(SimpleNamespace(), CUSTOMER, campaign_id="abc")


class TestCreateDraft:
    def test_creates_a_plan_with_three_actions(self, config, monkeypatch):
        _patch_read(monkeypatch, _ReadClient())

        result = cg.draft_custom_conversion_goal(
            config, name="OHL | Kauf + qualifizierte Anrufe",
            conversion_action_ids=[BUY, CALL_AD, CALL_SITE],
        )

        assert result["status"] == "PENDING_CONFIRMATION"
        assert result["operation"] == "create_custom_conversion_goal"
        assert result["changes"]["conversion_action_ids"] == [BUY, CALL_AD, CALL_SITE]
        assert result["changes"]["conversion_actions"] == [
            f"customers/{CUSTOMER}/conversionActions/{BUY}",
            f"customers/{CUSTOMER}/conversionActions/{CALL_AD}",
            f"customers/{CUSTOMER}/conversionActions/{CALL_SITE}",
        ]
        assert result["changes"]["status"] == "ENABLED"

    def test_duplicate_action_ids_are_collapsed(self, config, monkeypatch):
        _patch_read(monkeypatch, _ReadClient())

        result = cg.draft_custom_conversion_goal(
            config, name="Neu", conversion_action_ids=[BUY, BUY, CALL_AD]
        )

        assert result["changes"]["conversion_action_ids"] == [BUY, CALL_AD]

    def test_unknown_action_is_refused(self, config, monkeypatch):
        _patch_read(monkeypatch, _ReadClient())

        result = cg.draft_custom_conversion_goal(
            config, name="Neu", conversion_action_ids=[BUY, "9999999999"]
        )

        assert "does not exist" in " ".join(result["details"])

    def test_removed_action_is_refused(self, config, monkeypatch):
        _patch_read(monkeypatch, _ReadClient())

        result = cg.draft_custom_conversion_goal(
            config, name="Neu", conversion_action_ids=[REMOVED_ACTION]
        )

        assert "is REMOVED" in " ".join(result["details"])

    def test_empty_action_list_is_refused(self, config, monkeypatch):
        _patch_read(monkeypatch, _ReadClient())

        result = cg.draft_custom_conversion_goal(config, name="Neu")

        assert "At least one conversion_action_id is required" in " ".join(result["details"])

    def test_missing_name_is_refused(self, config, monkeypatch):
        _patch_read(monkeypatch, _ReadClient())

        result = cg.draft_custom_conversion_goal(
            config, name="  ", conversion_action_ids=[BUY]
        )

        assert "name is required" in " ".join(result["details"])

    def test_identical_goal_reports_already_exists(self, config, monkeypatch):
        _patch_read(monkeypatch, _ReadClient())

        result = cg.draft_custom_conversion_goal(
            config, name="OHL | Kauf + qualifizierte Leads",
            conversion_action_ids=[LEAD_SITE, CALL_SITE, CALL_AD, BUY],
        )

        assert result["status"] == "already_exists"
        assert result["custom_conversion_goal_id"] == EXISTING_GOAL
        assert "plan_id" not in result

    def test_same_name_with_other_actions_is_refused(self, config, monkeypatch):
        _patch_read(monkeypatch, _ReadClient())

        result = cg.draft_custom_conversion_goal(
            config, name="OHL | Kauf + qualifizierte Leads",
            conversion_action_ids=[BUY],
        )

        assert "already exists" in " ".join(result["details"])
        assert "draft_update_custom_conversion_goal" in " ".join(result["details"])

    def test_a_removed_goal_with_the_same_name_does_not_block(self, config, monkeypatch):
        """It cannot be updated (the update tool refuses REMOVED), so treating
        it as a name clash would leave the name unusable for ever."""
        _patch_read(monkeypatch, _ReadClient(goals=[
            _goal_row(EXISTING_GOAL, name="OHL | Kauf", status="REMOVED", actions=(BUY,)),
        ]))

        result = cg.draft_custom_conversion_goal(
            config, name="OHL | Kauf", conversion_action_ids=[CALL_AD],
        )

        assert result["status"] == "PENDING_CONFIRMATION"
        assert result["changes"]["name"] == "OHL | Kauf"

    def test_an_enabled_goal_with_the_same_name_still_blocks(self, config, monkeypatch):
        _patch_read(monkeypatch, _ReadClient(goals=[
            _goal_row(EXISTING_GOAL, name="OHL | Kauf", status="ENABLED", actions=(BUY,)),
        ]))

        result = cg.draft_custom_conversion_goal(
            config, name="OHL | Kauf", conversion_action_ids=[CALL_AD],
        )

        assert "already exists" in " ".join(result["details"])

    def test_blocked_operation_is_refused(self, monkeypatch):
        blocked = AdLoopConfig(
            ads=AdsConfig(customer_id=CUSTOMER),
            safety=SafetyConfig(blocked_operations=["create_custom_conversion_goal"]),
        )
        result = cg.draft_custom_conversion_goal(
            blocked, name="Neu", conversion_action_ids=[BUY]
        )

        assert "blocked by configuration" in result["error"]


class TestUpdateDraft:
    def test_renames_and_replaces_the_action_list(self, config, monkeypatch):
        _patch_read(monkeypatch, _ReadClient())

        result = cg.draft_update_custom_conversion_goal(
            config, custom_conversion_goal_id=EXISTING_GOAL,
            name="Neuer Name", conversion_action_ids=[BUY, CALL_AD],
        )

        changes = result["changes"]
        assert changes["name_before"] == "OHL | Kauf + qualifizierte Leads"
        assert changes["name_after"] == "Neuer Name"
        assert changes["conversion_action_ids_before"] == sorted([BUY, CALL_AD, CALL_SITE, LEAD_SITE])
        assert changes["conversion_action_ids_after"] == sorted([BUY, CALL_AD])

    def test_removing_one_action_is_a_list_replace(self, config, monkeypatch):
        _patch_read(monkeypatch, _ReadClient())

        result = cg.draft_update_custom_conversion_goal(
            config, custom_conversion_goal_id=EXISTING_GOAL,
            conversion_action_ids=[BUY, CALL_AD, CALL_SITE],
        )

        assert result["changes"]["conversion_action_ids_after"] == sorted(
            [BUY, CALL_AD, CALL_SITE]
        )

    def test_no_change_is_reported_without_a_plan(self, config, monkeypatch):
        _patch_read(monkeypatch, _ReadClient())

        result = cg.draft_update_custom_conversion_goal(
            config, custom_conversion_goal_id=EXISTING_GOAL,
            name="OHL | Kauf + qualifizierte Leads",
            conversion_action_ids=[BUY, CALL_AD, CALL_SITE, LEAD_SITE],
        )

        assert result["status"] == "no_change"
        assert "plan_id" not in result

    def test_unknown_goal_is_refused(self, config, monkeypatch):
        _patch_read(monkeypatch, _ReadClient())

        result = cg.draft_update_custom_conversion_goal(
            config, custom_conversion_goal_id="999", name="X"
        )

        assert "does not exist" in result["error"]

    def test_removed_goal_is_refused(self, config, monkeypatch):
        _patch_read(monkeypatch, _ReadClient(
            goals=[_goal_row(status="REMOVED")],
        ))

        result = cg.draft_update_custom_conversion_goal(
            config, custom_conversion_goal_id=EXISTING_GOAL, name="X"
        )

        assert "REMOVED" in result["error"]

    def test_nothing_requested_is_refused(self, config, monkeypatch):
        _patch_read(monkeypatch, _ReadClient())

        result = cg.draft_update_custom_conversion_goal(
            config, custom_conversion_goal_id=EXISTING_GOAL
        )

        assert "Nothing to change" in " ".join(result["details"])


class TestAssignDraft:
    def test_customer_level_campaign_can_be_pointed_at_a_goal(self, config, monkeypatch):
        _patch_read(monkeypatch, _ReadClient())

        result = cg.draft_assign_custom_conversion_goal(
            config, campaign_id=CAMPAIGN, custom_conversion_goal_id=EXISTING_GOAL
        )

        changes = result["changes"]
        assert changes["goal_config_level_before"] == "CUSTOMER"
        assert changes["goal_config_level_after"] == "CAMPAIGN"
        assert changes["custom_conversion_goal_after"] == (
            f"customers/{CUSTOMER}/customConversionGoals/{EXISTING_GOAL}"
        )

    def test_campaign_level_with_another_goal_is_switched(self, config, monkeypatch):
        other = f"customers/{CUSTOMER}/customConversionGoals/555"
        _patch_read(monkeypatch, _ReadClient(
            configs=[_config_row(level="CAMPAIGN", goal=other)],
        ))

        result = cg.draft_assign_custom_conversion_goal(
            config, campaign_id=CAMPAIGN, custom_conversion_goal_id=EXISTING_GOAL
        )

        assert result["changes"]["custom_conversion_goal_before"] == other
        assert result["status"] == "PENDING_CONFIRMATION"

    def test_same_goal_is_already_configured(self, config, monkeypatch):
        same = f"customers/{CUSTOMER}/customConversionGoals/{EXISTING_GOAL}"
        _patch_read(monkeypatch, _ReadClient(
            configs=[_config_row(level="CAMPAIGN", goal=same)],
        ))

        result = cg.draft_assign_custom_conversion_goal(
            config, campaign_id=CAMPAIGN, custom_conversion_goal_id=EXISTING_GOAL
        )

        assert result["status"] == "already_configured"
        assert "plan_id" not in result

    def test_unknown_campaign_is_refused(self, config, monkeypatch):
        _patch_read(monkeypatch, _ReadClient(configs=[]))

        result = cg.draft_assign_custom_conversion_goal(
            config, campaign_id="999", custom_conversion_goal_id=EXISTING_GOAL
        )

        assert "no conversion goal configuration" in result["error"]

    def test_removed_campaign_is_refused(self, config, monkeypatch):
        _patch_read(monkeypatch, _ReadClient(
            configs=[_config_row(status="REMOVED")],
        ))

        result = cg.draft_assign_custom_conversion_goal(
            config, campaign_id=CAMPAIGN, custom_conversion_goal_id=EXISTING_GOAL
        )

        assert "REMOVED" in result["error"]

    def test_unknown_goal_is_refused(self, config, monkeypatch):
        _patch_read(monkeypatch, _ReadClient())

        result = cg.draft_assign_custom_conversion_goal(
            config, campaign_id=CAMPAIGN, custom_conversion_goal_id="999"
        )

        assert "does not exist" in result["error"]

    def test_removed_goal_is_refused(self, config, monkeypatch):
        _patch_read(monkeypatch, _ReadClient(goals=[_goal_row(status="REMOVED")]))

        result = cg.draft_assign_custom_conversion_goal(
            config, campaign_id=CAMPAIGN, custom_conversion_goal_id=EXISTING_GOAL
        )

        assert "only ENABLED goals" in result["error"]

    def test_foreign_goal_id_is_treated_as_unknown(self, config, monkeypatch):
        """Goal IDs are only valid inside their own account."""
        _patch_read(monkeypatch, _ReadClient(goals=[]))

        result = cg.draft_assign_custom_conversion_goal(
            config, campaign_id=CAMPAIGN, custom_conversion_goal_id="9876543210"
        )

        assert "does not exist in this account" in result["error"]


class TestClearDraft:
    def test_campaign_level_goes_back_to_customer(self, config, monkeypatch):
        same = f"customers/{CUSTOMER}/customConversionGoals/{EXISTING_GOAL}"
        _patch_read(monkeypatch, _ReadClient(
            configs=[_config_row(level="CAMPAIGN", goal=same)],
        ))

        result = cg.draft_clear_custom_conversion_goal(config, campaign_id=CAMPAIGN)

        changes = result["changes"]
        assert changes["goal_config_level_before"] == "CAMPAIGN"
        assert changes["goal_config_level_after"] == "CUSTOMER"
        assert changes["custom_conversion_goal_before"] == same
        assert changes["custom_conversion_goal_after"] == ""

    def test_already_on_customer_level_is_a_no_op(self, config, monkeypatch):
        _patch_read(monkeypatch, _ReadClient())

        result = cg.draft_clear_custom_conversion_goal(config, campaign_id=CAMPAIGN)

        assert result["status"] == "already_configured"
        assert "plan_id" not in result


class TestLearningPeriodNote:
    """Changing the optimisation goal restarts Smart Bidding's learning."""

    def test_assign_preview_carries_the_note(self, config, monkeypatch):
        _patch_read(monkeypatch, _ReadClient())

        result = cg.draft_assign_custom_conversion_goal(
            config, campaign_id=CAMPAIGN, custom_conversion_goal_id=EXISTING_GOAL
        )

        assert any(
            "learning period" in w for w in result["changes"]["warnings"]
        )

    def test_clear_preview_carries_the_note(self, config, monkeypatch):
        same = f"customers/{CUSTOMER}/customConversionGoals/{EXISTING_GOAL}"
        _patch_read(monkeypatch, _ReadClient(
            configs=[_config_row(level="CAMPAIGN", goal=same)],
        ))

        result = cg.draft_clear_custom_conversion_goal(config, campaign_id=CAMPAIGN)

        assert any(
            "learning period" in w for w in result["changes"]["warnings"]
        )


class TestDraftWritesNothing:
    def test_drafts_only_read(self, config, monkeypatch):
        client = _patch_read(monkeypatch, _ReadClient())

        cg.draft_custom_conversion_goal(
            config, name="Neu", conversion_action_ids=[BUY]
        )
        cg.draft_assign_custom_conversion_goal(
            config, campaign_id=CAMPAIGN, custom_conversion_goal_id=EXISTING_GOAL
        )

        assert all("googleAdsService.search" not in q for q in client.queries)
        assert len(client.queries) >= 4


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


class _MutateClient:
    """Real proto types with fake services, and a read side for the readback."""

    def __init__(self, *, goals=None, configs=None):
        base = GoogleAdsClient(
            credentials=None, developer_token="test-token",
            use_proto_plus=True, version=GOOGLE_ADS_API_VERSION,
        )
        self.enums = base.enums
        self.get_type = base.get_type
        self.goal_request = None
        self.config_request = None
        self._goals = goals if goals is not None else [_goal_row()]
        self._configs = configs if configs is not None else [_config_row()]
        self._services = {
            "CustomConversionGoalService": SimpleNamespace(
                mutate_custom_conversion_goals=self._mutate_goals
            ),
            "ConversionGoalCampaignConfigService": SimpleNamespace(
                mutate_conversion_goal_campaign_configs=self._mutate_config
            ),
            "GoogleAdsService": SimpleNamespace(search=self._search),
        }

    def get_service(self, name):
        return self._services[name]

    def _mutate_goals(self, customer_id=None, operations=None):
        self.goal_request = SimpleNamespace(customer_id=customer_id, operations=operations)
        operation = operations[0]
        resource = (
            operation.update.resource_name
            if getattr(operation, "update", None) and operation.update.resource_name
            else f"customers/{customer_id}/customConversionGoals/6459279836"
        )
        return SimpleNamespace(results=[SimpleNamespace(resource_name=resource)])

    def _mutate_config(self, customer_id=None, operations=None):
        self.config_request = SimpleNamespace(customer_id=customer_id, operations=operations)
        return SimpleNamespace(
            results=[
                SimpleNamespace(
                    resource_name=(
                        f"customers/{customer_id}/conversionGoalCampaignConfigs/{CAMPAIGN}"
                    )
                )
            ]
        )

    def _search(self, customer_id, query):
        if "FROM custom_conversion_goal" in query:
            return self._goals
        if "FROM conversion_goal_campaign_config" in query:
            return self._configs
        if "FROM conversion_action" in query:
            return [
                _action_row(BUY), _action_row(CALL_AD), _action_row(CALL_SITE),
                _action_row(LEAD_SITE),
            ]
        raise AssertionError(query)


class TestApplyCreate:
    def test_create_uses_one_operation_with_name_and_actions(self):
        client = _MutateClient()

        result = cg._apply_create_custom_conversion_goal(
            client,
            CUSTOMER,
            {
                "name": "OHL | Kauf + qualifizierte Anrufe",
                "status": "ENABLED",
                "conversion_action_ids": [BUY, CALL_AD, CALL_SITE],
                "conversion_actions": [
                    f"customers/{CUSTOMER}/conversionActions/{BUY}",
                    f"customers/{CUSTOMER}/conversionActions/{CALL_AD}",
                    f"customers/{CUSTOMER}/conversionActions/{CALL_SITE}",
                ],
            },
        )

        assert len(client.goal_request.operations) == 1
        created = client.goal_request.operations[0].create
        assert created.name == "OHL | Kauf + qualifizierte Anrufe"
        assert list(created.conversion_actions) == [
            f"customers/{CUSTOMER}/conversionActions/{BUY}",
            f"customers/{CUSTOMER}/conversionActions/{CALL_AD}",
            f"customers/{CUSTOMER}/conversionActions/{CALL_SITE}",
        ]
        assert result["custom_conversion_goal_id"].isdigit()
        assert "readback" in result

    def test_create_always_sends_enabled_whatever_the_plan_says(self):
        """A goal created as REMOVED would be invisible everywhere else, so a
        status smuggled into the plan must not reach Google."""
        client = _MutateClient()

        cg._apply_create_custom_conversion_goal(
            client,
            CUSTOMER,
            {
                "name": "OHL | Kauf",
                "status": "REMOVED",
                "conversion_action_ids": [BUY],
                "conversion_actions": [f"customers/{CUSTOMER}/conversionActions/{BUY}"],
            },
        )

        created = client.goal_request.operations[0].create
        assert created.status == client.enums.CustomConversionGoalStatusEnum.ENABLED


class TestApplyUpdate:
    def test_update_mask_lists_only_the_requested_fields(self):
        client = _MutateClient()

        cg._apply_update_custom_conversion_goal(
            client,
            CUSTOMER,
            {
                "custom_conversion_goal_id": EXISTING_GOAL,
                "name_after": "Neuer Name",
                "conversion_actions": [
                    f"customers/{CUSTOMER}/conversionActions/{BUY}"
                ],
            },
        )

        operation = client.goal_request.operations[0]
        assert operation.update.resource_name == (
            f"customers/{CUSTOMER}/customConversionGoals/{EXISTING_GOAL}"
        )
        assert list(operation.update_mask.paths) == ["name", "conversion_actions"]
        assert list(operation.update.conversion_actions) == [
            f"customers/{CUSTOMER}/conversionActions/{BUY}"
        ]

    def test_rename_only_leaves_the_actions_out_of_the_mask(self):
        client = _MutateClient()

        cg._apply_update_custom_conversion_goal(
            client, CUSTOMER,
            {"custom_conversion_goal_id": EXISTING_GOAL, "name_after": "Neu"},
        )

        operation = client.goal_request.operations[0]
        assert list(operation.update_mask.paths) == ["name"]
        assert len(operation.update.conversion_actions) == 0


class TestApplyCampaignConfig:
    def test_assign_sets_campaign_level_and_the_goal(self):
        client = _MutateClient()
        target = f"customers/{CUSTOMER}/customConversionGoals/{EXISTING_GOAL}"

        result = cg._apply_assign_custom_conversion_goal(
            client, CUSTOMER,
            {"campaign_id": CAMPAIGN, "custom_conversion_goal_after": target},
        )

        operation = client.config_request.operations[0]
        assert operation.update.resource_name == (
            f"customers/{CUSTOMER}/conversionGoalCampaignConfigs/{CAMPAIGN}"
        )
        assert operation.update.goal_config_level == client.enums.GoalConfigLevelEnum.CAMPAIGN
        assert operation.update.custom_conversion_goal == target
        assert list(operation.update_mask.paths) == [
            "goal_config_level", "custom_conversion_goal",
        ]
        assert "readback" in result

    def test_clear_sets_customer_level_and_empties_the_goal(self):
        client = _MutateClient()

        result = cg._apply_clear_custom_conversion_goal(
            client, CUSTOMER, {"campaign_id": CAMPAIGN}
        )

        operation = client.config_request.operations[0]
        assert operation.update.goal_config_level == client.enums.GoalConfigLevelEnum.CUSTOMER
        assert operation.update.custom_conversion_goal == ""
        assert result["goal_config_level"] == "CUSTOMER"
        assert "readback" in result


class TestReadback:
    def test_assign_reads_back_the_new_configuration(self):
        target = f"customers/{CUSTOMER}/customConversionGoals/{EXISTING_GOAL}"
        client = _MutateClient(
            configs=[_config_row(level="CAMPAIGN", goal=target)]
        )

        result = cg._apply_assign_custom_conversion_goal(
            client, CUSTOMER,
            {"campaign_id": CAMPAIGN, "custom_conversion_goal_after": target},
        )

        config = result["readback"]["campaign"]
        assert config["goal_config_level"] == "CAMPAIGN"
        assert config["custom_conversion_goal"] == target

    def test_create_reads_back_the_new_goal(self):
        client = _MutateClient(goals=[
            _goal_row("6459279836", "OHL | Kauf + qualifizierte Anrufe",
                      actions=(BUY, CALL_AD, CALL_SITE))
        ])

        result = cg._apply_create_custom_conversion_goal(
            client, CUSTOMER,
            {
                "name": "OHL | Kauf + qualifizierte Anrufe",
                "status": "ENABLED",
                "conversion_action_ids": [BUY, CALL_AD, CALL_SITE],
                "conversion_actions": [
                    f"customers/{CUSTOMER}/conversionActions/{BUY}",
                    f"customers/{CUSTOMER}/conversionActions/{CALL_AD}",
                    f"customers/{CUSTOMER}/conversionActions/{CALL_SITE}",
                ],
            },
        )

        # The mutation fake returns 6459279836, so the readback must show it.
        assert result["readback"]["custom_conversion_goal"]["id"] == "6459279836"
        assert result["readback"]["custom_conversion_goal"]["conversion_action_ids"] == [
            BUY, CALL_AD, CALL_SITE,
        ]


class TestToolRegistration:
    @pytest.mark.asyncio
    async def test_annotations_schema_and_tags(self):
        from adloop.server import mcp

        tools = {t.name: t for t in await mcp.list_tools()}
        assert tools["get_custom_conversion_goals"].annotations.read_only_hint is True
        for name in ("draft_custom_conversion_goal", "draft_update_custom_conversion_goal",
                     "draft_assign_custom_conversion_goal",
                     "draft_clear_custom_conversion_goal"):
            assert tools[name].annotations.read_only_hint is False, name
            assert tools[name].annotations.destructive_hint is False, name

        properties = tools["draft_custom_conversion_goal"].parameters["properties"]
        for param in ("name", "conversion_action_ids"):
            assert properties[param].get("description"), param
        assert "status" not in properties
