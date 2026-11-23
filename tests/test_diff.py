from scenescout.diff import compare_scenes
from scenescout.models import PrimInfo, SceneSnapshot


def make_scene(prims, fps="24"):
    return SceneSnapshot("sample.usda", "/Asset", "Y", "1", fps, [], {p.path: p for p in prims})


def prim(path, **kwargs):
    return PrimInfo(path, path.split("/")[-1], kwargs.get("type_name", "Xform"), True, True, False,
                    variants=kwargs.get("variants", {}), attributes=kwargs.get("attributes", {}),
                    material_binding=kwargs.get("material_binding", ""))


def test_add_remove_and_change():
    before = make_scene([prim("/Asset", attributes={"size": "2"}), prim("/Asset/Old")])
    after = make_scene([prim("/Asset", attributes={"size": "3"}), prim("/Asset/New")], fps="25")
    changes = compare_scenes(before, after)
    assert {(c.kind, c.path, c.field) for c in changes} == {
        ("Added", "/Asset/New", "prim"), ("Removed", "/Asset/Old", "prim"),
        ("Modified", "/Asset", "attributes.size"), ("Modified", "/", "time_codes_per_second")
    }


def test_variant_material_and_type_changes():
    before = make_scene([prim("/Asset", variants={"lod": {"selected": "low"}}, material_binding="/Old")])
    after = make_scene([prim("/Asset", variants={"lod": {"selected": "high"}}, material_binding="/New", type_name="Mesh")])
    fields = {c.field for c in compare_scenes(before, after)}
    assert fields == {"variants.lod", "material_binding", "type_name"}


def test_no_changes():
    scene = make_scene([prim("/Asset")])
    assert compare_scenes(scene, scene) == []
