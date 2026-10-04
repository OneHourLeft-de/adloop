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

The wrapper fails closed: only path helpers, reads, ``mutate*`` and
``upload*`` calls (both sent validate-only) reach the real service. Any other
method raises, so a write path added later cannot slip past a dry run and
change an account for real.
"""

from __future__ import annotations

from types import SimpleNamespace

PLACEHOLDER = "adloop-validate-only"

# Methods that never change an account and may run for real in a dry run.
_READ_METHODS = frozenset({"search", "search_stream"})


class ValidateOnlyFailure(Exception):
    """Google rejected part of a validate-only request (partial failure).

    Carries the ``partial_failure_error`` proto so callers can turn Google's
    per-conversion errors into per-row messages — the same detail a real apply
    gets from the response.
    """

    def __init__(self, message: str, *, failure: object = None) -> None:
        super().__init__(message)
        self.failure = failure


class ValidateOnlyClient:
    """A GoogleAdsClient stand-in whose services only ever validate.

    Everything except ``get_service`` passes through (enums, ``get_type``),
    and every service method except ``mutate*`` passes through too, so the
    reads some apply paths do before mutating still run normally.

    ``is_validate_only`` marks this client for appliers that phrase their
    errors differently when nothing can have been written.
    """

    is_validate_only = True

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
        if attr in _READ_METHODS or attr.endswith("_path") or attr.startswith("parse_"):
            return getattr(self._service, attr)
        if not (attr.startswith("mutate") or attr.startswith("upload")):
            raise ValidateOnlyFailure(
                f"{self._name}.{attr} has no validate-only mode, so a dry run "
                "cannot check it without changing the account. Refusing."
            )
        target = getattr(self._service, attr)

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
                    raise ValidateOnlyFailure(
                        getattr(failure, "message", "") or str(failure),
                        failure=failure,
                    )

            return _placeholder_response(request.customer_id, len(operations), attr == "mutate")

        return validate

    def _build_request(self, method: str, kwargs: dict) -> object:
        request = self._owner._client.get_type(_request_type(self._name, method))
        for key, value in kwargs.items():
            if key in _LIST_FIELDS:
                getattr(request, key).extend(value)
            else:
                setattr(request, key, value)
        return request


_LIST_FIELDS = ("operations", "mutate_operations", "conversions")


def _request_type(service_name: str, method: str) -> str:
    """``mutate_ad_group_criteria`` -> ``MutateAdGroupCriteriaRequest``,
    ``upload_click_conversions`` -> ``UploadClickConversionsRequest``."""
    if method == "mutate":
        return "Mutate" + service_name.removesuffix("Service") + "Request"
    verb, _, rest = method.partition("_")
    return verb.capitalize() + "".join(word.capitalize() for word in rest.split("_")) + "Request"


def _operations_of(request: object) -> object:
    for field in _LIST_FIELDS:
        if hasattr(request, field):
            return getattr(request, field)
    return []


class _PlaceholderResult:
    """A MutateOperationResponse stand-in: every ``*_result`` field
    (``asset_result``, ``campaign_asset_result``, ...) carries the placeholder,
    whichever one the apply code reads."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str) -> object:
        if attr.endswith("_result"):
            return SimpleNamespace(resource_name=self._name)
        raise AttributeError(attr)


def _placeholder_response(customer_id: str, count: int, googleads_mutate: bool) -> object:
    names = [f"customers/{customer_id}/{PLACEHOLDER}/{i}" for i in range(count)]
    if googleads_mutate:
        return SimpleNamespace(
            mutate_operation_responses=[_PlaceholderResult(n) for n in names],
            partial_failure_error=None,
        )
    return SimpleNamespace(
        results=[SimpleNamespace(resource_name=n) for n in names],
        partial_failure_error=None,
    )
