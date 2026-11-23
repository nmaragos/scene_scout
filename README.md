# SceneScout — OpenUSD Publish Review

Windows-first, read-only desktop application for inspecting composed OpenUSD stages and reviewing two publishes. Built with PySide6 and Pixar's OpenUSD. Interactive 3D defaults to Hydra Storm. A compatible Windows runtime also supports Autodesk Aurora ray tracing.

## Try the browser demo

Explore the bundled pump comparison, move the A/B wipe, and inspect the authored layers behind the material and transform changes. For a quick glance, open [web demo](https://nmaragos.github.io/scene_scout/). The demo uses pre-rendered images and requires no installation. It is a very simplified version of the end result.


## Start on Windows 11

Open PowerShell in a fresh source checkout and start with inspection and comparison. The locked setup uses 64-bit Python 3.13 and needs no separate imaging runtime. Install uv before running these commands. [uv installation](https://docs.astral.sh/uv/getting-started/installation/)

```powershell
uv sync --locked --python 3.13
uv run --locked scenescout
```

uv creates and manages the local `.venv` automatically and installs the Qt Essentials modules used by SceneScout. You can pass a different supported version to `--python` if needed. The project supports Python 3.11–3.14, subject to wheel availability.

Open two USD files to enter **Compare**. Select a finding to see the values, authoring layers, and spec paths for Stage A and Stage B. The layered pump sample shows a material binding from `look_v013.usda` and a bracket translation from `assembly_v013.usda`.

```powershell
uv run --locked scenescout .\samples\review\pump_v012.usda .\samples\review\pump_v013.usda
```

### Optional native viewport

Obtain an x64 Windows SDK with OpenUSD imaging, or build a separate installation with CMake, Visual Studio's desktop C++ tools and the Windows SDK. Enable Python bindings, UsdImagingGL, Storm and usdview. Use a matching 64-bit Python version supported by SceneScout, plus PySide6 with Qt OpenGL support and PyOpenGL. Check that usdview starts in the SDK environment before launching SceneScout. [OpenUSD 25.08 build instructions](https://github.com/PixarAnimationStudios/OpenUSD/blob/v25.08/BUILDING.md)

For a packaged SDK with `python\python.exe` and USD modules under `lib\python`, set the absolute SDK directory in PowerShell. The launcher starts the bundled interpreter with the SDK libraries and plug-ins on its search path.

```powershell
$env:SCENESCOUT_USD_SDK = 'C:\path\to\your\openusd-sdk'
uv run --locked scenescout .\samples\review\pump_v012.usda .\samples\review\pump_v013.usda
```

Alternatively, create `scenescout.local.toml` at the project root with the SDK path. `SCENESCOUT_USD_SDK` takes precedence.

```toml
usd_sdk = 'C:\path\to\your\openusd-sdk'
```

For a custom OpenUSD build with an external Python installation, use the same Python and environment that starts usdview. Add the SceneScout source directory to that environment and start the module directly. Leave the launcher SDK setting unset for this route.

```powershell
$python = 'C:\path\to\your\python.exe'
$env:PYTHONPATH = "$env:PYTHONPATH;$(Join-Path (Get-Location) src)"
& $python -m scenescout
```

The status bar identifies the runtime or explains why the app opened in inspection-only mode. Hover over the runtime indicator to see the startup message after it has been replaced by progress or load status.

### Use Aurora ray tracing

Aurora requires Windows with a DXR-capable GPU and driver. Build the v25.08-beta plug-in in a separate runtime using Autodesk's pinned dependencies, then select that runtime using the SDK setting above. Use the SceneScout build notes and native patch for the Windows toolchain and Hydra presentation details. [Aurora build instructions](https://github.com/Autodesk/Aurora/blob/v25.08-beta/Doc/Build.md) · [SceneScout build notes](docs/adr/0001-aurora-dxr.md)

Open the layered pump sample and select **Aurora (DXR)** in the viewport renderer dropdown.

Both sides of the wipe use Aurora and share the current camera. The image refines after camera and time changes. Select prims in the hierarchy while Aurora is active. Switch to **Storm** for viewport picking and selection highlighting. SceneScout starts with Storm and returns to it if Aurora is unavailable or fails its startup check.

Revisiting a recent Compare finding restores its last camera and both Aurora images when the stage times and viewport size match. If you left before an image finished, the saved preview appears first and Aurora starts a new refinement pass for that side. Dragging the wipe works with the saved images. The cache is held in memory for the current comparison, up to 16 views or 128 MiB. Stage changes such as payload and variant edits clear it.

If Aurora is unavailable, check the runtime shown in the status bar. Set `SCENESCOUT_USD_SDK` to the Aurora SDK directory or clear it to use the local TOML setting at the next launch.

Drag to orbit, middle-drag to pan, and scroll to zoom. Select a change or click a prim in the viewport to inspect it. When both publishes are open, drag the thin A/B divider anywhere along its length or grab the circle near its base. The letters beneath the circle identify Stage A on the left and Stage B on the right. Both views share a camera. In **Prim Inspector**, pick a variant to preview it temporarily and use **Reset stage preview** to clear the overrides for that stage. Comparison and JSON export use the published stages.

For blink comparison, focus the viewport and hold **A** or **B** to show that stage at full width. Release the key to return to the previous wipe position.

Use the **Stage A** and **Stage B** buttons to inspect the same prim path across publishes. If that path is absent from the active snapshot, the hierarchy remembers it when you switch back.

Select a layer in **Layer Stack** to highlight its contributions on the active stage’s side of the A/B wipe. The evidence panel lists prims and authored opinions. For a sublayer or referenced layer, choose **Preview layer mute** to compare the active stage with that layer enabled and muted. **Exit preview** restores the A/B camera and divider.

## Features now

### Review a publish and record a decision

```powershell
uv run scenescout .\samples\review\signoff_v001.usda .\samples\review\signoff_v002.usda
```

Leave **Load payloads on open** unchecked for this fixture. In **Compare**, filter by **Dependency** and select **Reference target** for `/Publish/Chair`. The two versions have identical geometry. The evidence shows the authored list edits, resolver results and effective targets for A and B. **Open issues** shows the missing lamp reference. The **Payload coverage** row for `/Publish/Extras` records intentionally unloaded contents.

Choose **Animation** and select the `/Publish/AnimatedProp` row to jump to time code 48 and its authored samples. Choose **Structural** to see the **Only in A** and **Only in B** marker findings. Mark findings **Reviewed**, flag open issues and leave notes in the evidence pane. **Sign-off…** records the review decision and overall notes.

Use **Review → Save review…** to capture the findings and evidence in local JSON. **Open saved review…** opens the recorded review even when the stages are offline. **Resume in Compare** opens the original pair and restores the review position and matching notes. If the findings or coverage differ, the decision returns to **Pending**. Incomplete reads also keep the decision **Pending** and disable sign-off. You can still save or export the partial findings. **Export review as HTML…** creates a standalone report with the decision, open issues and evidence.

Select a finding and choose **Pin view** to save the shared camera, both stage times, and wipe position. **Restore view** returns to that view while reviewing. Save the review with the pinned finding selected to recover the view automatically when you resume it in Compare. Exit temporary variant previews before pinning.

SceneScout captures small A/B thumbnails automatically when a visited finding finishes rendering. The **Rendering…** badge stays visible while Aurora is still settling either side. Saved reviews and standalone HTML reports include the captured images. Thumbnails are at most 320 pixels on their longest side, with a 2 MiB image budget per file. Findings you skip or leave before the viewport settles can have evidence without thumbnails.

Select a prim and click **Isolate selection** below the viewport to show its branch in each stage. A side without the prim is empty. The isolated branch stays visible as you browse findings. Click **Show all** to restore the full scene. Isolation is a temporary viewport preview, and automatic thumbnail capture pauses while it is active.

### Share a review with a second reviewer

1. With a comparison open, choose **Review → Share review on network…** and select the workstation's address on your trusted LAN or studio VPN. Choose `127.0.0.1` to try the workflow on this computer.
2. Send the copied private link to the reviewer. They can open it in a browser, read the evidence and available thumbnails, edit notes and finding flags, pick a sign-off decision, and click **Save review** on the page. Anyone with the link can edit while sharing is active.
3. SceneScout displays the saved browser changes. Use **Review → Sign-off…** to read the latest review notes. Use **Review → Save review…** to keep the result on disk.
4. Use **Review → Stop sharing review** to revoke the link. Leaving the comparison or closing SceneScout also ends the session.

The link uses HTTP and the port shown in the URL. A second computer needs network access to that address and port.

Thumbnails captured in SceneScout after sharing starts appear the next time the reviewer loads the same page. The reviewer can save any edits before refreshing.

Missing filesystem textures appear under **Dependency → Open issues** in Compare. Select **Texture file** to see the authored path, layer, spec path, and resolver result. For UDIM patterns, SceneScout reports an issue when OpenUSD resolves zero tiles. A set can use non-consecutive tile numbers.

The hierarchy appears as the inspector reads each publish. The status bar shows inspection and comparison progress. **Cancel analysis** stops the request and discards any results still in flight. Opening a stage or changing alignment starts a new comparison.

### Review animation

Open the two animated pump publishes together:

```powershell
uv run scenescout .\samples\review\pump_motion_v001.usda .\samples\review\pump_motion_v002.usda
```

In the **Compare** findings table, click the **Transform · translate** row for `/Pump/Bracket` with **48** in the **Time (A)** column. The timeline jumps to that time and the A/B wipe shows the two poses from the same camera. **Finding evidence** shows the evaluated translation on each stage and its authored layer and spec. At 48 the bracket moves 0.3 units along X in A and 0.9 in B. Scrub back to 1 to see their poses coincide. Click that Compare row again to return to 48. The Prim Hierarchy selects a prim independently of time.

The selected stage owns the editable time field. Switch between **Stage A** and **Stage B** to edit either stage at the same review instant. The adjacent readout shows the other stage’s time. **Match by seconds** aligns stages from time code zero, so A at 24 on a 24-time-code/s stage matches B at 30 on a 30-time-code/s stage. Choose **Match by frame number** to compare 24 with 24. Switching the alignment recalculates the animation changes. Drag the slider to snap to whole frames of the selected stage, hold **Shift** to scrub to the subframe under the pointer, or type an exact time in the field.

For each sampled attribute on a shared prim, Compare checks the composed values at the union of its sample times from both stages. Extra keys that evaluate to the same values add no rows. Differences solely between those times can go undetected. Prims present in only one stage have structural rows. Payload coverage applies to animation as well.

### Stage inspection

- Open `.usd`, `.usda`, `.usdc`, `.usdz` using real OpenUSD composition.
- Hierarchy tree, prim type, filter by prim path/type.
- Prim attributes, composed attribute connections, relationships, variant choices and selections, asset info, and authored references and payloads from contributing prim specs.
- Used layers, default prim, units, up axis, time codes per second, and authored playback range.
- Read-only A/B composed snapshot comparison.
- On-demand layer and spec evidence for selected changes.
- Optional GPU-accelerated viewport with shared-camera wipe, picking, framing, and temporary variant previews.
- Export complete scene snapshot to JSON.
- USD reading on background Qt thread to avoid blocking interaction.
- Payload loading off by default (enable only for trusted assets and accessible dependencies).
- In **Prim Hierarchy**, Ctrl-click individual prims or Shift-click a range. Right-click a selected prim and choose **Load selected payloads** or **Unload selected payloads** to apply the operation to those branches. The **Payloads** menu also offers actions for the selection or the entire active stage. For comparison, use matching load rules on both stages.

## Known limitations

- The viewport requires a separate OpenUSD imaging runtime. It uses raw stage coordinates, and Compare flags differing units or up axes.
- Compare evaluates full default and sampled values and may shorten their display. It also compares composed attribute connections and the authored playback range. Clip-based evidence identifies clip resolution where USD reports it. Complete composition diagnostics remain roadmap work.
- Dependency evidence covers reference and payload list edits, composition errors, and filesystem textures in composed shader and material inputs. Texture checks cover default values. Unloaded branches, time-sampled textures, image decoding, studio asset identifiers, and value clip dependencies are outside this check.
- Large stages can consume substantial memory. Full snapshot inspection reads attribute defaults and indexes sample times. Cancellation takes effect between USD calls.
- The USD environment and resolver context are taken from the launching process; custom asset resolvers may need studio configuration.
- Opening an external USD file can trigger asset resolution and file I/O. Inspect only trusted assets and use a sandboxed environment for untrusted scenes.
- The native viewport is a Windows development configuration. A redistributable installer and broader GPU compatibility testing are roadmap work.

## Testing

```powershell
uv run --extra dev pytest -q
```

## Architecture

```
src/scenescout/
    models.py      # immutable data structures, no Qt dependency
    usd_reader.py  # OpenUSD import, stage composition, read-only snapshot
    diff.py        # deterministic semantic snapshot comparisons
    app.py         # desktop UI, background loader, filtering/export
    review.py      # change list, viewport controls, and evidence panel
    finding_queue.py # sortable and filterable comparison findings
    dependencies.py  # authored list edits, resolver results and effective arcs
    signoff.py       # review capture and resume in Compare
    review_document.py # saved review format and standalone HTML
    provenance.py  # authored opinions for selected changes
    viewport.py    # optional Hydra/Storm preview and GPU wipe
samples/           # versioned OpenUSD fixtures, including a layered review pair
```

## Roadmap

1. **0.2**: Visual review with native Hydra/Storm, scene framing, selection sync, temporary variants, and A/B wipe. Explore glTF as a portable preview format.
2. **0.3**: publish review and sign-off, including reference and payload diagnostics, animation findings, local saved reviews and standalone HTML.
3. **0.4**: AYON integration via adapter, published asset version browser, compare history.
4. **0.5**: asset validation rule packs and a dependency graph.
5. **1.0**: signed Windows installer, Linux package, job handling, tests with real studio fixtures, profile-based performance.

## Relationship to UPD

SceneScout remains a standalone project. It may later consume a stable, versioned UPD adapter; it does not modify or depend on existing UPD code.
