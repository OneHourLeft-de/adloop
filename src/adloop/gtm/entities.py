"""The entity kinds a Tag Manager workspace change can carry.

The publish digest, the pending-change summary and the workspace diff all have
to see the same kinds, so the list lives here rather than in each caller.
"""

ENTITY_KINDS = (
    "tag", "trigger", "variable", "folder", "client", "transformation", "zone",
    "builtInVariable", "customTemplate", "gtagConfig",
)

# The field carrying a kind's own id inside the Entity wrapper. A custom
# template names it ``templateId`` (v2 discovery document), every other kind
# follows ``<kind>Id``; a built-in variable has no id and falls back to its
# name.
ENTITY_ID_FIELDS = {"customTemplate": "templateId"}


def entity_id(kind: str, entity: dict) -> str:
    """One entity's id, falling back to its name."""
    field = ENTITY_ID_FIELDS.get(kind, f"{kind}Id")
    return str(entity.get(field) or entity.get("name") or "")
