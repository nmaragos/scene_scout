"""Compare composed attribute values at sample times on a shared timeline."""
from math import isfinite

from .diff import Change
from .usd_reader import _as_text


def _read_sample(attr, time_code):
    if not attr:
        return None, None, ""
    try:
        return attr.GetTypeName(), attr.Get(time_code), ""
    except Exception as exc:
        return None, None, f"<unreadable: {type(exc).__name__}>"


def _sample_text(sample):
    type_name, value, error = sample
    if error:
        return error
    if type_name is None:
        return "<missing attribute>"
    return f"{type_name}: {_as_text(value)}"


def compare_animation(stage_a, snapshot_a, stage_b, snapshot_b, rate_b: float, *,
                      cancelled=lambda: False, progress=None) -> list[Change]:
    """Compare numeric-time values, with stage B time equal to timeline time * rate_b."""
    from pxr import Usd

    if not isfinite(rate_b) or rate_b <= 0:
        raise ValueError("Stage B's timeline rate must be positive and finite.")

    changes = []
    paths = snapshot_a.prims.keys() & snapshot_b.prims.keys()
    total = len(paths)
    if progress:
        progress("Comparing animated attributes", 0, total)
    for index, path in enumerate(paths, 1):
        if cancelled():
            return []
        if progress and index % 128 == 0:
            progress("Comparing animated attributes", index - 1, total)
        samples_a = snapshot_a.prims[path].sample_times
        samples_b = snapshot_b.prims[path].sample_times
        if not samples_a and not samples_b:
            continue
        prim_a = stage_a.GetPrimAtPath(path)
        prim_b = stage_b.GetPrimAtPath(path)
        for name in samples_a.keys() | samples_b.keys():
            if cancelled():
                return []
            attr_a = prim_a.GetAttribute(name)
            attr_b = prim_b.GetAttribute(name)
            # Retain B's original authored time so a divide/multiply round trip
            # cannot turn an exact key read into an interpolation.
            keys_a = set(samples_a.get(name, ()))
            mapped_a = {time * rate_b: time for time in keys_a}
            keys_b = {mapped_a.get(t, t / rate_b): t for t in samples_b.get(name, ())}
            for time in keys_a | keys_b.keys():
                if cancelled():
                    return []
                time_b = keys_b[time] if time in keys_b else time * rate_b
                sample_a = _read_sample(attr_a, Usd.TimeCode(time))
                sample_b = _read_sample(attr_b, Usd.TimeCode(time_b))
                if sample_a[2] or sample_b[2] or sample_a != sample_b:
                    field = f"attributes.{name}"
                    display = _sample_text(sample_a), _sample_text(sample_b)
                    if sample_a[2] or sample_b[2]:
                        change = Change("Unreadable", path, field, *display, time=time, time_b=time_b)
                    else:
                        type_a, value_a, _ = sample_a
                        type_b, value_b, _ = sample_b
                        change = Change.from_values(
                            "Modified", path, field, (str(type_a), value_a), (str(type_b), value_b),
                            display=display, time=time, time_b=time_b,
                        )
                    changes.append(change)
    changes.sort(key=lambda change: (change.time, change.path, change.field))
    if progress:
        progress("Comparing animated attributes", total, total)
    return changes
