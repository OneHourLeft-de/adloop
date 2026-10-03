"""Run a Google Ads plan in validate-only mode for ``confirm_and_apply`` dry runs.

The dry run used to echo the plan back without asking Google anything, so a
change the API would reject only failed at the real apply. Wrapping the Ads
client here sends every mutate the plan would send with ``validate_only=True``
instead: Google checks the full request (field values, policy, references to
existing entities) and executes nothing.

Validate-only responses carry no results, so each validated call answers the
apply code with placeholder resource names. A later call that references one
(a plan whose second step uses what the first would have created) cannot be
validated meaningfully; it is skipped and counted instead of sent.
"""

from __future__ import annotations

from types import SimpleNamespace

PLACEHOLDER = "adloop-validate-only"

_RESPONSE_FIELD_FOR_SERVICE = "campaign_result"


class ValidateOnlyFailure(Exception):
    """Google rejected part of a validate-only request (partial failure)."""


class ValidateOnlyClient:
    """A GoogleAdsClient stand-in whose services only ever validate.

    Everything except ``get_service`` passes through (enums, ``get_type``),
    and every service method except ``mutate*`` passes through too, so the
    reads some apply paths do before mutating still run normally.
    """

    def __init__(self, client: object) -> None:
        self._client = client
        self.validated_calls = 0
        self.skipped_calls = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self._client, name)

    def get_service(self, name: str, *args: object, **kwargs: object) -> object:
        return _ValidateOnlyService(self, name, self._client.get_service(name, *args, **kwargs))


class _ValidateOnlyService:
    def __init__(self, owner: ValidateOnlyClient, name: str, service: object) -> None:
        self._owner = owner
        self._name = name
        self._service = service

    def __getattr__(self, attr: str) -> object:
        target = getattr(self._service, attr)
        if not attr.startswith("mutate"):
            return target

        def validate(request: object = None, **kwargs: object) -> object:
            if request is None:
                request = self._build_request(attr, kwargs)
            request.validate_only = True

            operations = list(_operations_of(request))
            if PLACEHOLDER in str(request):
                self._owner.skipped_calls += 1
            else:
                response = target(request=request)
                self._owner.validated_calls += 1
                failure = getattr(response, "partial_failure_error", None)
                if failure is not None and getattr(failure, "code", 0):
                    raise ValidateOnlyFailure(getattr(failure, "message", "") or str(failure))

            return _placeholder_response(request.customer_id, len(operations), attr == "mutate")

        return validate

    def _build_request(self, method: str, kwargs: dict) -> object:
        request = self._owner._client.get_type(_request_type(self._name, method))
        for key, value in kwargs.items():
            if key in ("operations", "mutate_operations"):
                getattr(request, key).extend(value)
            else:
                setattr(request, key, value)
        return request


def _request_type(service_name: str, method: str) -> str:
    """``mutate_ad_group_criteria`` -> ``MutateAdGroupCriteriaRequest``."""
    if method == "mutate":
        return "Mutate" + service_name.removesuffix("Service") + "Request"
    words = method.removeprefix("mutate_").split("_")
    return "Mutate" + "".join(word.capitalize() for word in words) + "Request"


def _operations_of(request: object) -> object:
    if hasattr(request, "mutate_operations"):
        return request.mutate_operations
    return getattr(request, "operations", [])


def _placeholder_response(customer_id: str, count: int, googleads_mutate: bool) -> object:
    names = [f"customers/{customer_id}/{PLACEHOLDER}/{i}" for i in range(count)]
    if googleads_mutate:
        return SimpleNamespace(
            mutate_operation_responses=[
                SimpleNamespace(**{_RESPONSE_FIELD_FOR_SERVICE: SimpleNamespace(resource_name=n)})
                for n in names
            ],
            partial_failure_error=None,
        )
    return SimpleNamespace(
        results=[SimpleNamespace(resource_name=n) for n in names],
        partial_failure_error=None,
    )
