"""Normalize structured and legacy failure classifications at boundaries."""


def normalize_failure_classification(value) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump(mode="json"))
    as_dict = getattr(value, "dict", None)
    if callable(as_dict):
        return dict(as_dict())
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return dict(attributes)
    return {"value": str(value)}
