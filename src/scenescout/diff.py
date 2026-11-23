"""Compare *composed* stage snapshots. Diff is analytical, never modifies USD files."""
from dataclasses import dataclass

from .analysis import check_cancelled
from .comparison_value import value_fingerprint
from .dependencies import authored_edit_text
from .models import SceneSnapshot
from .textures import TEXTURE_FIELD_PREFIX, texture_summary


@dataclass(frozen=True)
class Change:
    kind: str
    path: str
    field: str
    before: str
    after: str
    time: float | None = None
    time_b: float | None = None
    identity: str | None = None

    @classmethod
    def from_values(cls, kind, path, field, left, right, *, display=None, time=None, time_b=None):
        before, after = (str(left), str(right)) if display is None else display
        return cls(kind, path, field, before, after, time, time_b, value_fingerprint((left, right)))


def finding_kind_label(kind: str) -> str:
    return {
        "Removed": "Only in A",
        "Added": "Only in B",
        "Modified": "Differs",
    }.get(kind, kind)


def field_label(field):
    if field.startswith(TEXTURE_FIELD_PREFIX):
        input_name = field.removeprefix(TEXTURE_FIELD_PREFIX).removeprefix("inputs:")
        return "Texture file" if input_name == "file" else f"Texture file · {input_name}"
    labels = {
        "material_binding": "Material binding",
        "time_range": "Authored playback range",
        "dependencies.reference": "Reference target",
        "dependencies.payload": "Payload target",
        "dependencies.composition": "Composition diagnostic",
        "dependencies.payload.status": "Payload coverage",
    }
    if field in labels:
        return labels[field]
    if field.startswith("attributes.xformOp:"):
        return f"Transform · {field.rpartition(':')[2]}"
    category, _, name = field.partition(".")
    if category == "connections":
        return f"Connections · {name}"
    if category == "sample_times":
        return f"Sample times · {name}"
    return name or category.replace("_", " ").capitalize()


def compare_scenes(before: SceneSnapshot, after: SceneSnapshot, *, cancelled=None, progress=None) -> list[Change]:
    check_cancelled(cancelled)
    changes: list[Change] = []
    dependency_changes: list[Change] = []
    unloaded_a, unloaded_b = set(before.unloaded_payloads), set(after.unloaded_payloads)
    loaded_a, loaded_b = set(before.loaded_payloads), set(after.loaded_payloads)
    for field in ("default_prim", "up_axis", "meters_per_unit", "time_codes_per_second"):
        left, right = getattr(before, field), getattr(after, field)
        if left != right:
            changes.append(Change.from_values("Modified", "/", field, left, right))
    if before.time_range != after.time_range:
        changes.append(Change.from_values(
            "Modified", "/", "time_range", before.time_range, after.time_range,
            display=(_time_range_text(before.time_range), _time_range_text(after.time_range)),
        ))
    left_paths, right_paths = set(before.prims), set(after.prims)
    groups = (sorted(right_paths - left_paths), sorted(left_paths - right_paths), sorted(left_paths & right_paths))
    total = sum(len(paths) for paths in groups)
    count = 0
    if progress:
        progress("Comparing structure and defaults", count, total)

    def mark_prim_compared():
        nonlocal count
        count += 1
        check_cancelled(cancelled)
        if progress and (count % 128 == 0 or count == total):
            progress("Comparing structure and defaults", count, total)

    for path in groups[0]:
        right = after.prims[path]
        changes.append(Change.from_values("Added", path, "prim", "", right.type_name))
        changes.extend(_read_error_findings(path, {}, right.property_read_errors))
        dependency_changes.extend(_compare_dependencies(path, None, right, unloaded_a, unloaded_b,
                                                        loaded_a, loaded_b))
        mark_prim_compared()
    for path in groups[1]:
        left = before.prims[path]
        changes.append(Change.from_values("Removed", path, "prim", left.type_name, ""))
        changes.extend(_read_error_findings(path, left.property_read_errors, {}))
        dependency_changes.extend(_compare_dependencies(path, left, None, unloaded_a, unloaded_b,
                                                        loaded_a, loaded_b))
        mark_prim_compared()
    scalar_fields = ("type_name", "active", "loaded", "instance", "material_binding")
    mapping_fields = ("variants", "attributes", "connections", "relationships", "asset_info")
    fingerprint_fields = {"attributes": "attribute_fingerprints", "asset_info": "asset_info_fingerprints"}
    for path in groups[2]:
        left, right = before.prims[path], after.prims[path]
        errors_a, errors_b = left.property_read_errors, right.property_read_errors
        unreadable = errors_a.keys() | errors_b.keys()
        changes.extend(_read_error_findings(path, errors_a, errors_b))
        dependency_changes.extend(_compare_dependencies(path, left, right, unloaded_a, unloaded_b,
                                                        loaded_a, loaded_b))
        for field in scalar_fields + mapping_fields:
            if field == "material_binding" and "relationships.material:binding" in unreadable:
                continue
            l, r = getattr(left, field), getattr(right, field)
            if field in mapping_fields:
                fingerprint_field = fingerprint_fields.get(field)
                fingerprints_a = getattr(left, fingerprint_field) if fingerprint_field else {}
                fingerprints_b = getattr(right, fingerprint_field) if fingerprint_field else {}
                for key in sorted(set(l) | set(r)):
                    change_field = f"{field}.{key}"
                    if change_field in unreadable:
                        continue
                    lv, rv = l.get(key), r.get(key)
                    value_a = (fingerprints_a[key],) if key in fingerprints_a else lv
                    value_b = (fingerprints_b[key],) if key in fingerprints_b else rv
                    if value_a != value_b:
                        changes.append(Change.from_values(
                            "Modified", path, change_field, value_a, value_b,
                            display=(str(lv or ""), str(rv or "")),
                        ))
            elif l != r:
                changes.append(Change.from_values("Modified", path, field, l, r))
        mark_prim_compared()
    for path in sorted((unloaded_a | unloaded_b) - left_paths - right_paths):
        check_cancelled(cancelled)
        dependency_changes.extend(_compare_dependencies(path, None, None, unloaded_a, unloaded_b, loaded_a, loaded_b))
    check_cancelled(cancelled)
    return dependency_changes + changes


def _time_range_text(interval):
    if interval is None:
        return "(no authored range)"
    start, end = interval
    return f"{start} to {end}"


def _read_error_findings(path, left, right):
    return [
        Change("Unreadable", path, field, left.get(field, ""), right.get(field, ""))
        for field in sorted(left.keys() | right.keys())
    ]


def _compare_dependencies(path, left, right, unloaded_a, unloaded_b, loaded_a, loaded_b):
    changes = []
    errors_a = left.composition_errors if left else ()
    errors_b = right.composition_errors if right else ()
    if errors_a or errors_b:
        changes.append(Change.from_values(
            "Issue" if errors_b else "Modified", path, "dependencies.composition",
            (left is not None, tuple((error.kind, error.message) for error in errors_a)),
            (right is not None, tuple((error.kind, error.message) for error in errors_b)),
            display=(_error_summary(left, errors_a), _error_summary(right, errors_b)),
        ))
    payload_unloaded = path in unloaded_a or path in unloaded_b
    if payload_unloaded:
        changes.append(Change.from_values(
            "Coverage", path, "dependencies.payload.status",
            _payload_status(path, unloaded_a, loaded_a), _payload_status(path, unloaded_b, loaded_b),
        ))
    for kind, legacy_field in (("reference", "references"), ("payload", "payloads")):
        a = tuple(edit for edit in left.dependencies if edit.kind == kind) if left else ()
        b = tuple(edit for edit in right.dependencies if edit.kind == kind) if right else ()
        if a or b:
            effective_comparable = kind != "payload" or not payload_unloaded
            signature_a = _dependency_signature(a, effective_comparable)
            signature_b = _dependency_signature(b, effective_comparable)
            if signature_a != signature_b:
                changes.append(Change.from_values(
                    "Modified", path, f"dependencies.{kind}", signature_a, signature_b,
                    display=(_dependency_summary(a), _dependency_summary(b)),
                ))
        elif left and right and getattr(left, legacy_field) != getattr(right, legacy_field):
            changes.append(Change.from_values("Modified", path, legacy_field, getattr(left, legacy_field),
                                              getattr(right, legacy_field)))
    textures_a = left.texture_inputs if left else {}
    textures_b = right.texture_inputs if right else {}
    for name, assets_b in sorted(textures_b.items()):
        assets_a = textures_a.get(name, ())
        missing_b = any(not asset.resolved_path for asset in assets_b)
        if missing_b or any(not asset.resolved_path for asset in assets_a):
            changes.append(Change.from_values(
                "Issue" if missing_b else "Modified", path, TEXTURE_FIELD_PREFIX + name,
                tuple((asset.authored_path, asset.resolved_path, asset.udim) for asset in assets_a),
                tuple((asset.authored_path, asset.resolved_path, asset.udim) for asset in assets_b),
                display=(texture_summary(assets_a), texture_summary(assets_b)),
            ))
    return changes


def _error_summary(prim, errors) -> str:
    if errors:
        return "\n".join(error.message for error in errors)
    return "No dependency composition error reported" if prim else "Prim absent from this snapshot"


def _payload_status(path, unloaded, loaded) -> str:
    if path in unloaded:
        return "Unloaded by stage load rules. Payload contents are outside this snapshot."
    if path in loaded:
        return "Included in the stage load set"
    return "No loadable payload reported at this path"


def _dependency_signature(edits, effective_comparable):
    def target_signature(target):
        authored = target.asset_path, target.prim_path, target.offset, target.scale, target.resolved_path
        if not effective_comparable:
            return authored
        layer = target.composed_layer if target.asset_path else ""
        return (*authored, layer, target.composed_prim_path, target.composed_order,
                target.composed_offset, target.composed_scale)

    return tuple((edit.operation, tuple(map(target_signature, edit.targets))) for edit in edits)


def _dependency_summary(edits) -> str:
    if not edits:
        return "No authored dependency opinions"
    summary = [authored_edit_text(edit) for edit in edits]
    composed = sorted(
        (target for edit in edits for target in edit.targets if target.composed_order is not None),
        key=lambda target: target.composed_order,
    )
    for target in composed:
        layer = f"{target.composed_layer} · " if target.asset_path else ""
        text = f"Composed {layer}{target.composed_prim_path}"
        if target.composed_offset or target.composed_scale != 1:
            text += f" · stage offset {target.composed_offset:g}, scale {target.composed_scale:g}"
        summary.append(text)
    if not composed:
        summary.append("No contributing target confirmed in current composition")
    return "\n".join(summary)
