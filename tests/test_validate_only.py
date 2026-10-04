"""Google Ads dry runs: the plan's mutates go to Google with validate_only=True."""

from types import SimpleNamespace

import pytest

from adloop.ads import write
from adloop.ads.validate_only import PLACEHOLDER, ValidateOnlyClient, ValidateOnlyFailure
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety.preview import ChangePlan, get_plan, store_plan


class Bag:
    """Stands in for proto messages: attributes appear on first touch."""

    def __init__(self):
        object.__setattr__(self, "_fields", {})

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self._fields.setdefault(name, Bag())

    def __setattr__(self, name, value):
        self._fields[name] = value

    def __repr__(self):
        return repr(self._fields)


class FakeRequest:
    def __init__(self, list_field):
        setattr(self, list_field, [])
        self.customer_id = ""
        self.validate_only = False
        self.partial_failure = False

    def __str__(self):
        return repr(vars(self))


class FakeService:
    def __init__(self, client, name):
        self._client = client
        self._name = name

    def __getattr__(self, attr):
        if attr.endswith("_path"):
            return lambda *parts: "/".join(str(p) for p in parts)
        if attr.startswith("mutate") or attr.startswith("upload"):
            def mutate(request=None, **kwargs):
                self._client.calls.append((self._name, attr, request))
                if self._client.reject:
                    raise RuntimeError(self._client.reject)
                return SimpleNamespace(results=[], partial_failure_error=self._client.partial_failure)
            return mutate
        raise AttributeError(attr)


class FakeAdsClient:
    def __init__(self, reject=None, partial_failure=None):
        self.calls = []
        self.reject = reject
        self.partial_failure = partial_failure
        self.enums = Bag()

    def get_service(self, name):
        return FakeService(self, name)

    def get_type(self, name):
        if name == "MutateGoogleAdsRequest":
            return FakeRequest("mutate_operations")
        if name.startswith("Upload"):
            return FakeRequest("conversions")
        if name.endswith("Request"):
            return FakeRequest("operations")
        return Bag()


def test_single_mutate_is_sent_validate_only_and_answered_with_a_placeholder():
    fake = FakeAdsClient()
    client = ValidateOnlyClient(fake)

    result = write._apply_update_ad_group(client, "123", {"ad_group_id": "9", "max_cpc": 1.5})

    [(service, method, request)] = fake.calls
    assert (service, method) == ("AdGroupService", "mutate_ad_groups")
    assert request.validate_only is True
    assert request.customer_id == "123"
    assert len(request.operations) == 1
    assert PLACEHOLDER in result["resource_name"]
    assert (client.validated_calls, client.skipped_calls) == (1, 0)


def test_googleads_batch_mutate_validates_every_operation():
    fake = FakeAdsClient()
    client = ValidateOnlyClient(fake)

    result = write._apply_create_ad_group(client, "123", {
        "ad_group_name": "Brand",
        "campaign_id": "42",
        "keywords": [{"text": "adloop", "match_type": "EXACT"}],
    })

    [(service, method, request)] = fake.calls
    assert (service, method) == ("GoogleAdsService", "mutate")
    assert request.validate_only is True
    assert len(request.mutate_operations) == 2
    assert PLACEHOLDER in result["ad_group"]
    assert client.validated_calls == 1


def test_steps_that_build_on_an_earlier_step_are_skipped_not_sent():
    fake = FakeAdsClient()
    client = ValidateOnlyClient(fake)

    write._apply_create_negative_keyword_list(client, "123", {
        "list_name": "Junk",
        "keywords": ["free"],
        "match_type": "BROAD",
        "campaign_id": "42",
    })

    # Only the shared set itself can be checked; adding keywords to it and
    # attaching it both reference the set Google never created.
    assert [m for _, m, _ in fake.calls] == ["mutate_shared_sets"]
    assert (client.validated_calls, client.skipped_calls) == (1, 2)


def test_a_brand_list_is_validated_as_a_whole_in_one_call():
    """Unlike the keyword list, the brand list is create-and-fill in one
    request: the criteria use the temporary name, so the dry run checks the
    real thing instead of a shared set nobody will ever see."""
    fake = FakeAdsClient()
    client = ValidateOnlyClient(fake)

    result = write._apply_create_brand_list(client, "123", {
        "list_name": "Competitors",
        "brand_ids": ["/m/1", "/m/2"],
        "campaign_ids": ["42"],
        "negative": True,
    })

    # One validated call for set + criteria; only the campaign attachment is
    # skipped (it references the set Google never created in a dry run).
    assert [m for _, m, _ in fake.calls] == ["mutate"]
    request = fake.calls[0][2]
    assert request.validate_only is True
    assert len(request.mutate_operations) == 3
    assert (client.validated_calls, client.skipped_calls) == (1, 1)
    assert PLACEHOLDER in result["shared_set_resource"]


def test_a_partial_failure_in_validation_raises():
    fake = FakeAdsClient(partial_failure=SimpleNamespace(code=3, message="campaign not found"))
    client = ValidateOnlyClient(fake)

    with pytest.raises(ValidateOnlyFailure, match="campaign not found"):
        client.get_service("CampaignSharedSetService").mutate_campaign_shared_sets(
            customer_id="123", operations=[Bag()], partial_failure=True,
        )


@pytest.fixture
def real_validation(monkeypatch):
    """Undo the suite's offline stub and hand the dry run a fake Ads client."""
    monkeypatch.setattr(write, "_validate_with_google", _real_validate)

    def use(fake):
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _config: fake)

    return use


def _real_validate(config, plan):
    return write._execute_plan(config, plan, validate_only=True)


def _config(tmp_path, **safety):
    return AdLoopConfig(
        ads=AdsConfig(customer_id="123-456-7890"),
        safety=SafetyConfig(log_file=str(tmp_path / "audit.log"), **safety),
    )


def _plan():
    plan = ChangePlan(
        operation="update_ad_group",
        entity_type="ad_group",
        entity_id="9",
        customer_id="1234567890",
        changes={"ad_group_id": "9", "max_cpc": 2.0},
    )
    store_plan(plan)
    return plan


def test_dry_run_reports_what_google_validated(tmp_path, real_validation):
    fake = FakeAdsClient()
    real_validation(fake)
    plan = _plan()

    result = write.confirm_and_apply(_config(tmp_path), plan_id=plan.plan_id, dry_run=True)

    assert result["status"] == "DRY_RUN_SUCCESS"
    assert result["checks"] == {"validated_calls": 1, "skipped_calls": 0}
    assert "validate_only" in result["note"]
    assert fake.calls[0][2].validate_only is True


def test_a_rejected_dry_run_fails_and_keeps_two_phase_closed(tmp_path, real_validation):
    fake = FakeAdsClient(reject="RESOURCE_NOT_FOUND: ad group 9")
    real_validation(fake)
    plan = _plan()
    config = _config(tmp_path, two_phase_apply=True, require_dry_run=False)

    dry = write.confirm_and_apply(config, plan_id=plan.plan_id, dry_run=True)

    assert dry["status"] == "DRY_RUN_FAILED"
    assert "RESOURCE_NOT_FOUND" in dry["error"]
    assert "validate-only" in dry["message"]
    assert get_plan(plan.plan_id).dry_run_result is None

    real = write.confirm_and_apply(config, plan_id=plan.plan_id, dry_run=False)
    assert real["status"] == "DRY_RUN_REQUIRED"
    # Nothing but the one validate-only attempt ever reached the client.
    assert all(request.validate_only for _, _, request in fake.calls)


def test_asset_dry_runs_read_their_own_result_field(tmp_path, real_validation):
    """Asset applies read asset_result / campaign_asset_result, not
    campaign_result; the placeholder must answer whichever they read."""
    fake = FakeAdsClient()
    real_validation(fake)
    plan = ChangePlan(
        operation="create_callouts",
        entity_type="campaign_asset",
        customer_id="1234567890",
        changes={"campaign_id": "42", "callouts": ["Free shipping", "24/7 support"]},
    )
    store_plan(plan)

    result = write.confirm_and_apply(_config(tmp_path), plan_id=plan.plan_id, dry_run=True)

    assert result["status"] == "DRY_RUN_SUCCESS", result
    assert result["checks"]["validated_calls"] >= 1
    assert all(request.validate_only for _, _, request in fake.calls)


def test_methods_without_a_validate_only_mode_are_refused():
    fake = FakeAdsClient()
    client = ValidateOnlyClient(fake)

    with pytest.raises(ValidateOnlyFailure, match="no validate-only mode"):
        client.get_service("KeywordPlanIdeaService").generate_keyword_ideas

    assert fake.calls == []


def test_uploads_are_sent_validate_only():
    fake = FakeAdsClient()
    client = ValidateOnlyClient(fake)

    client.get_service("ConversionUploadService").upload_click_conversions(
        customer_id="123", conversions=[Bag(), Bag()], partial_failure=True,
    )

    [(service, method, request)] = fake.calls
    assert method == "upload_click_conversions"
    assert request.validate_only is True
    assert len(request.conversions) == 2
    assert client.validated_calls == 1
