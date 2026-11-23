"""Stable fingerprints for the typed values used by comparisons."""
import hashlib
import json


def _typed_value(value):
    kind = type(value)
    if kind in (str, bool, int, float) or value is None:
        return kind.__name__, value
    if kind is dict:
        return "dict", [(key, _typed_value(item)) for key, item in sorted(value.items())]
    if kind in (list, tuple):
        return kind.__name__, [_typed_value(item) for item in value]

    name = f"{kind.__module__}.{kind.__name__}"
    if name == "pxr.Sdf.AssetPath":
        return name, (getattr(value, "authoredPath", value.path),
                      getattr(value, "evaluatedPath", ""), value.resolvedPath)
    if name.startswith("pxr.Gf.Quat"):
        return name, _typed_value((value.GetReal(), value.GetImaginary()))
    if name.startswith("pxr.Gf.Range"):
        return name, _typed_value((value.GetMin(), value.GetMax()))
    if name.endswith(".AssetPathArray") or name.startswith(("pxr.Gf.Vec", "pxr.Gf.Matrix")):
        return name, [_typed_value(item) for item in value]
    # OpenUSD's native array repr includes every element at round-trip precision,
    # including legacy array shape, without visiting large geometry arrays in Python.
    return name, repr(value)


def value_fingerprint(value):
    encoded = json.dumps(_typed_value(value), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()
