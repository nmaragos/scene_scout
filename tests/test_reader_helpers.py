from scenescout.usd_reader import _as_text, _list_op_items, inspect_stage, load_stage_payloads, snapshot_stage
import pytest


def test_truncate():
    assert _as_text("abcdef", 4) == "abcd…"


def test_listop():
    class Op:
        explicitItems = ["x"]
        prependedItems = ["y", "x"]
        appendedItems = []
        addedItems = []
        orderedItems = []
    assert _list_op_items(Op()) == ["x", "y"]


def test_usd_integration_when_installed():
    pytest.importorskip("pxr")
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "samples" / "asset_v001.usda"
    result = inspect_stage(str(path))
    assert "/Asset" in result.prims
    assert "model" in result.prims["/Asset"].variants


def test_load_selected_payloads_without_loading_siblings():
    Usd = pytest.importorskip("pxr.Usd")
    asset = Usd.Stage.CreateInMemory()
    asset.DefinePrim("/Asset/Geometry", "Cube")
    stage = Usd.Stage.CreateInMemory(load=Usd.Stage.LoadNone)
    stage.DefinePrim("/World/Selected").GetPayloads().AddPayload(asset.GetRootLayer().identifier, "/Asset")
    stage.DefinePrim("/World/Sibling").GetPayloads().AddPayload(asset.GetRootLayer().identifier, "/Asset")

    initial = snapshot_stage(stage)
    assert initial.unloaded_payloads == ["/World/Selected", "/World/Sibling"]
    assert not initial.prims["/World/Selected"].loaded

    selected_stage = load_stage_payloads(stage, "/World/Selected")
    selected = snapshot_stage(selected_stage)
    assert selected.unloaded_payloads == ["/World/Sibling"]
    assert "/World/Selected/Geometry" in selected.prims
    assert "/World/Sibling/Geometry" not in selected.prims
    assert not stage.GetPrimAtPath("/World/Selected").IsLoaded()

    cumulative = snapshot_stage(load_stage_payloads(selected_stage, "/World/Sibling"))
    everything = snapshot_stage(load_stage_payloads(stage, "/"))
    assert cumulative.unloaded_payloads == everything.unloaded_payloads == []
    assert cumulative.prims.keys() == everything.prims.keys()


def test_override_only_payload_branch_is_selectable():
    Usd = pytest.importorskip("pxr.Usd")
    asset = Usd.Stage.CreateInMemory()
    asset.DefinePrim("/Asset/Geometry", "Cube")
    stage = Usd.Stage.CreateInMemory(load=Usd.Stage.LoadNone)
    stage.OverridePrim("/World/Override").GetPayloads().AddPayload(asset.GetRootLayer().identifier, "/Asset")
    stage.OverridePrim("/UnrelatedOverride")

    assert set(snapshot_stage(stage).prims) == {"/World", "/World/Override"}
    loaded = snapshot_stage(load_stage_payloads(stage, "/World"))
    assert "/World/Override/Geometry" in loaded.prims


def test_layered_review_traces_authored_changes():
    pytest.importorskip("pxr")
    from pathlib import Path
    from scenescout.diff import compare_scenes
    from scenescout.provenance import inspect_opinions
    from scenescout.usd_reader import open_stage, snapshot_stage

    folder = Path(__file__).resolve().parents[1] / "samples" / "review"
    before = open_stage(str(folder / "pump_v012.usda"))
    after = open_stage(str(folder / "pump_v013.usda"))
    changes = {(change.path, change.field) for change in compare_scenes(snapshot_stage(before), snapshot_stage(after))}
    assert ("/Pump/Housing", "material_binding") in changes
    assert ("/Pump/Bracket", "attributes.xformOp:translate") in changes

    left = inspect_opinions(before, "/Pump/Housing", "material_binding")[1]
    right = inspect_opinions(after, "/Pump/Housing", "material_binding")[1]
    assert Path(left[0].layer).name == "pump_base.usda"
    assert Path(right[0].layer).name == "look_v013.usda"
    assert right[0].spec_path == "/Pump/Housing.material:binding"
    assert "/Pump/Looks/Copper" in right[0].value
    assert Path(right[1].layer).name == "pump_base.usda"

    summary, transform = inspect_opinions(after, "/Pump/Bracket", "attributes.xformOp:translate")
    assert summary.startswith("Winning default first")
    assert Path(transform[0].layer).name == "assembly_v013.usda"
    assert transform[0].spec_path == "/Pump/Bracket.xformOp:translate"
