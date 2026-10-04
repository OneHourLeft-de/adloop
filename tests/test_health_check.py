"""health_check probes the GA4 Data API, not just the Admin API (issue #60)."""

import pytest

from adloop import runtime, server
from adloop.config import AdLoopConfig, GA4Config


@pytest.fixture
def stub_other_services(monkeypatch):
    """Keep the Ads and Reddit probes out of the way."""
    import adloop.ads.gaql as gaql

    monkeypatch.setattr(gaql, "execute_query", lambda *_a, **_k: [])
    yield
    runtime.set_default_config(None)


def _summaries(*properties):
    return {
        "accounts": [{"properties": [{"property": p} for p in properties]}],
        "total_properties": len(properties),
    }


def _health(monkeypatch, *, config, summaries, probe):
    import adloop.ga4.reports as reports

    runtime.set_default_config(config)
    monkeypatch.setattr(reports, "get_account_summaries", lambda _c: summaries)
    monkeypatch.setattr(reports, "probe_data_api", probe)
    return server.health_check()


def test_data_api_disabled_is_not_ok(monkeypatch, stub_other_services):
    def disabled(_config, _prop):
        raise RuntimeError("403 SERVICE_DISABLED: Google Analytics Data API has not been used")

    status = _health(
        monkeypatch,
        config=AdLoopConfig(ga4=GA4Config(property_id="123")),
        summaries=_summaries("properties/123"),
        probe=disabled,
    )

    assert status["ga4"] == "error"
    assert status["ga4_admin"] == "ok"
    assert status["ga4_data"] == "error"
    assert "SERVICE_DISABLED" in status["ga4_error"]


def test_both_surfaces_ok(monkeypatch, stub_other_services):
    probed = []
    status = _health(
        monkeypatch,
        config=AdLoopConfig(),
        summaries=_summaries("properties/987"),
        probe=lambda _c, prop: probed.append(prop),
    )

    assert (status["ga4"], status["ga4_admin"], status["ga4_data"]) == ("ok", "ok", "ok")
    # No configured property: the first accessible one is probed.
    assert probed == ["properties/987"]


def test_no_property_to_probe(monkeypatch, stub_other_services):
    status = _health(
        monkeypatch,
        config=AdLoopConfig(),
        summaries=_summaries(),
        probe=lambda *_a: pytest.fail("nothing to probe"),
    )

    assert status["ga4"] == "ok"
    assert status["ga4_data"] == "not_checked"
