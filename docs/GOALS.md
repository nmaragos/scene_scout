# SceneScout 0.3 Publish Review and Sign-off

Artists and leads review two local OpenUSD publishes on Windows. The current workstation uses PySide6 and the NVIDIA OpenUSD 25.08 SDK with Hydra Storm. Review work is read-only with respect to the USD files. Kitsu, AYON and production data are outside the milestone.

The first usable increment is Compare dependency evidence. A chair version swap must appear even when it produces identical geometry. A finding should show both authored targets, their layers and spec paths, the resolver result, and the effective targets USD confirms. A missing reference and an intentionally unloaded payload must be distinguishable.

The release acceptance case uses a local pair with that dependency swap, missing reference, unloaded payload and an animated change. The reviewer can filter findings, jump to the prim and time, read the supporting evidence, record notes and a decision, reopen the saved review and export an HTML report for someone without SceneScout. The hierarchy should be usable during analysis, the viewport should stay interactive, and cancelled work must never replace current findings.

Verification uses portable local fixtures and the available Kitchen Set files as a baseline. The supplied Windows workstation and SDK are the target for native viewport checks.

## Aurora ray tracing on Windows

Artists and leads need to drag the existing A/B wipe and see both publishes ray traced from the same camera. First render `Kitchen_set.usd` locally with Autodesk HdAurora and DirectX Raytracing. Then add `Kitchen_set_variant.usd` and verify that each side of the wipe shows its own Aurora render. Check orbit, time navigation, and image settling after camera moves. Record observed GPU memory and settling time where practical.

Storm remains the default and the fallback, with a visible status if Aurora is unavailable or fails to initialize. Add the renderer choice after proving a local Aurora render. Preserve inspection, frame controls, payload toggles, Storm workflows, and viewport picking where the renderer supports it.

The target workstation has an RTX 4070 Ti with 12 GB of VRAM and a working NVIDIA OpenUSD 25.08 SDK. Use an isolated scratch build and local USD fixtures. Installer packaging and verification on a second Windows machine are outside this milestone.

## Saved visual review

The first delivery is for reviews on the current Windows workstation. Pin the shared camera, both stage times, and wipe position to a Compare finding. Saving and resuming the review should recover that view for a matching finding. Existing review files must remain readable.

Capture compact A/B thumbnails automatically as findings settle during review. Include them in the saved review and standalone HTML, with a 2 MiB embedded image budget per file. Blink comparison shows one stage at full width while its key is held and restores the wipe on release. Prim isolation previews one branch across both stages using temporary session layers.

Missing texture checks start with filesystem paths and UDIM patterns resolved by OpenUSD in the stage's context. Stage B issues show the texture's authored path, source layer, and property spec. Selecting a resolver for studio asset IDs is a later increment.

## Shared browser review

A second reviewer opens a shared URL, reads the exported comparison evidence and available A/B thumbnails, adds finding and review notes, and records the final sign-off. Changes made in the browser sync back to SceneScout. The first increment is complete when the desktop reviewer can see the second reviewer's notes and decision on the same review.

SceneScout hosts one shared review on a selected IPv4 address on the current workstation. The reviewer connects over the same trusted LAN or studio VPN using a private link. The browser needs no USD runtime or access to the source publishes. The desktop uses its existing local review files for durable storage.

## GitHub browser demo

GitHub visitors should be able to open the demo without installing SceneScout and explain a publish change from its authored evidence within a minute. The first release uses the bundled pump v012 and v013 stages, a category filter, composed values and authored opinions, and a draggable wipe over fixed-camera previews. Visitors can trace the copper material binding to `look_v013.usda` and the bracket move to `assembly_v013.usda`.

The local folder contains the demo and a GitHub Actions workflow for Pages. The repository owner creates the remote, chooses its visibility, pushes the project, and enables deployment. The demo is static and runs in the browser. Live 3D, user uploads, syncing edits, and production integrations are outside this release.
