# Image Editor implementation

The integrated Image Editor keeps RGBA layers, masks, layer offsets and editable MARK-style annotations in versioned `.engimg` projects. Full-resolution compositing supports normal, multiply, screen, overlay, darken, lighten, difference and soft light. Crop preserves off-canvas layer data; resizing, rotation, flip, canvas size, paint, erasure/restoration, clone, healing, selections and mask refinement have bounded undo history. Workers handle expensive pixel tools; failures restore the pre-operation state.

## Composition (2.3.1)

The editor composes several images into one document the way Photoshop does.

**Adding images as layers.** A new layer always goes above the active layer and becomes active, with the Move tool selected.

| Action | Result |
|---|---|
| Drop images on the canvas | New layers, centred where they are dropped (several cascade by 24 px) |
| Drop images on the Layers panel | New layers, centred in the visible part of the canvas |
| Drop on the document tabs, hold Option/Alt while dropping, drop a `.engimg` project, or **Open** | A separate document |
| ⌘V / Ctrl+V, *Edit › Paste as New Layer* | New layers from the clipboard (see Clipboard) |
| *Layer › Import image as layer…*, the image button above the Layers list | New layers from files |

While dragging, the editor outlines the target and says what releasing will do: "Place as New Layer" or "Open as New Document", and how to switch. Placed images keep every original pixel. A layer larger than the canvas is never downscaled automatically; a bar offers **Fit to canvas**, **Expand canvas** (Reveal All) or **Keep size**.

**Move tool (V).** The active layer shows a transform frame.
- Drag inside the frame to move (Shift: one axis). Arrow keys nudge 1 px (Shift: 10 px).
- Drag a corner handle to scale proportionally (Shift: free proportions; Option/Alt: from the centre). Edge handles change one dimension. Dragging past the opposite side mirrors the layer.
- Drag the round knob above the frame, or just outside a corner, to rotate (Shift: 15° steps).
- With *Auto-select layer* (default) a click picks the topmost layer with a visible pixel under the pointer; off, any drag moves the active layer.
- Snapping (View › Snap, default on) aligns the layer's edges and centre with the canvas edges and centre, guides and other layers; magenta lines show the match. Hold Control (macOS) / Ctrl to move freely.
- The tool panel has numeric X, Y, W, H and angle (Keep proportions), and buttons for Fit to canvas, Center, Rotate 90°, Flip horizontally and Reset transform. The Layer menu adds the rest (Transform…, Flip vertical, Bring forward / Send backward, Merge down, Reveal All).
- While dragging, the layers below and above the active one are drawn separately so the layer moves live (preview blending is normal; the applied result uses the real blend mode). Releasing applies one undoable step. Locked layers show a grey frame and do not move.

**Transforms resample once.** `ImageDocument.transform_layer` applies a canvas-space affine matrix in a single premultiplied resample (area-averaged first for strong reductions). The layer keeps its original pixels and the matrix from them (`ImageLayer.source`), so scaling a layer down and up again, or rotating it repeatedly, is re-rendered from the original instead of compounding blur; *Reset transform* returns the exact original pixels. Painting, healing, a direct fill or a cut replaces the layer image and makes the original stale automatically (`layer.original` is valid only while `source.rendered is layer.image`). Masks follow the transform. Annotation layers record the transform as a replayable raster operation so their text stays editable.

**Clipboard and duplication** (Edit menu; one owner per key, enabled only in the Image workspace):

| Keys (macOS / Windows, Linux) | Command |
|---|---|
| ⌘C / Ctrl+C | Copy the active layer, or only its selected pixels when there is a selection |
| ⇧⌘C / Ctrl+Shift+C | Copy Merged: the visible image (or its selected area), without flattening the project |
| ⌘X / Ctrl+X | Cut: copy, then clear the selected pixels (or remove the layer without a selection) |
| ⌘V / Ctrl+V | Paste as new layers; with no document open, as a new document |
| *Edit › Paste as New Document* | Always a new document, sized to the pasted content |
| ⌘J / Ctrl+J | Duplicate the layer; with a selection, Layer via Copy |
| ⇧⌘J (macOS) | Layer via Cut (Ctrl+Shift+J switches workspaces on Windows and Linux) |
| ⌘Z, ⇧⌘Z / Ctrl+Z, Ctrl+Shift+Z or Ctrl+Y | Undo, Redo |

Copies put two things on the clipboard. Other applications get the visible pixels in the remembered clipboard format (Copy to Clipboard › *Clipboard format and options…*: PNG/JPEG/TIFF/BMP, transparency or background, scale, DPI) plus PNG and TIFF on macOS, PNG, DIBV5 and DIB on Windows, PNG on Linux. The editor also gets a private copy (`com.ca-works.eng-drawing-viewer.layers`, an ENGIMG archive read with the same validation as a project file), so pasting back keeps masks, opacity, blend mode, name and position: in place when the canvas size matches (same or same-size document), otherwise centred in view. Pasting reads, in order, the private layers, PNG, TIFF, HEIC, JPEG, WebP, GIF, BMP and copied image files (Finder), with the same size and decompression limits as opening a file. The *Copy to Clipboard* button keeps its 2.3.0 meaning: the merged image in the chosen format.

**Projects.** Saving never flattens. ENGIMG version 2 adds, for each transformed layer, its original pixels (`sources/<i>.png`) and matrix, so a reopened project still transforms from the originals. Projects without such layers are written as version 1 and open in 2.3.0; version 2 projects make 2.3.0 report an unsupported version. Export (PNG, JPEG and the rest) always writes the flattened composite.

## Paint Bucket (G)

Click to fill the colour area under the pointer with the foreground colour (the swatch in the tool panel, shared with the brush, text and shapes). Options: Tolerance and Contiguous (the Magic Wand's perceptual scale), Anti-alias edges, Opacity, *Sample all layers* (find the area in the visible image, fill the active layer) and *Fill on a new layer* (non-destructive). An active selection limits the fill. Clicking a transparent area fills transparency, up to the soft edge of an anti-aliased outline. Locked layers are never filled; annotation layers always get the fill on a new layer so their drawing stays editable. Each fill is one undo step.

## Magic Eraser (X), Magic Wand (W) and colour tolerance

One click hides the colour area through the layer mask; the layer's pixels never change, so Restore, *Remove mask* or Undo bring them back. Options: Tolerance, Contiguous, **Stop at edges**, Anti-alias edges, Opacity, and *Remove background automatically*.

Diagnosis of the 2.3.0 behaviour (person in light clothing on a beige backdrop): pixels were compared with the single clicked pixel by their largest RGB channel difference, and contiguous mode flooded through any chain of within-tolerance pixels. A photographed backdrop varies more across the frame (lighting falloff, floor shading) than light clothing differs from it, so any tolerance that took the whole backdrop also took the clothing. On a synthetic reproduction with ground truth, 2.3.0 at its default tolerance erased 47 % of the figure, and no tolerance separated figure from backdrop (best IoU 74 %).

Now (`core/image_tools.py`, `color_region`):
- **Colour difference** is CIE ΔE\*94 (graphic-arts weights) in CIELAB, combined with the alpha difference, against the median of the visible pixels in a 5 × 5 patch around the click. Every pixel is compared with that sample, never with its neighbour, so a region cannot drift step by step into another colour.
- **Tolerance** is 0–100 on that perceptual scale (default 16): about 5 takes in only near-identical shades, 10–25 related shades, 40 and more a broad range. A larger tolerance never selects less (tested).
- **Stop at edges** (contiguous mode, Magic Eraser): visible boundaries become walls the 4-connected flood cannot cross. Walls are Canny edges on the steepest of the L, a and b gradients (so hue and saturation edges count as much as brightness), plus every pixel above the strong threshold (Canny drops the weaker edge where two meet), closed with a 5 × 5 closing and thickened by one pixel. Thresholds start at 2.5 × the backdrop's own gradient noise near the click (at least 0.8 ΔE per pixel), so grain and JPEG noise do not wall off a backdrop; small enclosed cells of within-tolerance colour (noise rings) join the region. Off: colour alone decides.
- **Anti-aliasing** gives a three-pixel band around the region partial coverage: each band pixel is a mix of the nearby region colour and the nearby other colour, and its coverage is the projection between them, which stays right under uneven lighting.
- Already-invisible pixels count as matches for the Magic Eraser only, so a backdrop split by earlier erasing still goes in one click; for the Magic Wand and Paint Bucket an opaque click never takes in transparent pixels.
- Contiguous off erases the colour everywhere in the layer; when that hides more than a third of the layer, the status bar and a notice say so, with the Undo key.

Measured on the synthetic scenes (`tests/image_scenes.py`; default tolerance, one click in the corner):

| Scene | 2.3.0 (default 32, RGB) IoU | Now IoU at 0.5 / 2 / 12 MP |
|---|---|---|
| Figure in light clothing on beige | 52.7 % (47 % of the figure lost) | 98.0 / 99.4 / 99.8 % |
| White box and mug on beige | 97.4 % | 96.0 / 96.7 / 97.9 % (backdrop specks, nothing of the subject lost) |
| Light tile on a slightly warmer white | 90.3 % | 99.3 / 99.8 / 99.9 % |
| Product on white with contact shadow | 85.5 % | 94.2 / 94.9 / 98.7 % |
| Grey bracket with holes (contiguous keeps the holes) | 90.9 % | 90.7 / 91.1 / 91.4 % |
| Polished cylinder on light grey | 69.6 % | 98.9 / 78.3 / 77.9 % |

12 MP takes about 0.6 s on an M3 Max. The cylinder at camera resolution is the honest limit: where a highlight matches the backdrop with no visible boundary, a colour tool cannot see the edge; lower the tolerance, click again, or use automatic removal (which separates it fully).

## Automatic background removal

*Remove background automatically* (Magic Eraser panel and the Offline subject selection menu) is model-free and offline. It seeds GrabCut from the image border's dominant colours on a ≤ 1024 px working copy; the seeding now stops at the same edge walls as the Magic Eraser, so a light subject on a light backdrop is not handed to the backdrop through a soft gap. Backdrop-coloured regions walled off from the backdrop at the subject's rim (a white cap on a white sweep) become definite subject, which GrabCut's colour model alone flipped. Regions enclosed by the subject (gaps between arms) remain GrabCut's to decide.

`refine_mask` then re-estimates the boundary at full resolution: pixels near the mask's 50 % contour are treated as a mix of the nearby backdrop and subject colours, and pixels unlike both (a dark rim, an outline) count as subject. This replaced the 2.3.0 bilinear upscale of the working mask, and it is also applied to offline model output. *Clean edge colours on a new layer* (Offline subject selection menu) solves edge pixels for the subject colour without the old backdrop's tint and puts that copy on a new layer, hiding the unchanged original; otherwise the result is the layer mask, refinable with Restore Mask and the Mask Eraser.

| Scene (IoU, 1× / 3× size) | 2.3.0 | Now | Boundary alpha error 2.3.0 → now (3×) |
|---|---|---|---|
| Figure in light clothing on beige | 55.0 / 57.9 % | 98.4 / 99.7 % | 27.7 → 7.2 % |
| White box and mug on beige | 98.9 / 99.1 % | 99.4 / 99.8 % | 17.8 → 3.8 % |
| Polished cylinder | 99.6 / 90.2 % | 100 / 100 % | 9.2 → 0.5 % |
| Product with white cap and contact shadow | 82.8 / 77.7 % | 90.1 / 85.2 % | 30.6 → 15.8 % |
| Bracket with holes and thin ribs | 98.6 / 98.8 % | 99.0 / 99.5 % | 13.8 → 6.2 % |
| Light tile on light backdrop | 96.8 / 99.1 % | 99.4 / 99.8 % | 33.4 → 6.8 % |

The product's remaining gap is its soft contact shadow, which stays with the product (the ground truth counts it as backdrop); erase it with the Magic Eraser or the Mask Eraser. This is not semantic subject detection: a subject that shares the border's colours without a visible boundary, cluttered backgrounds, hair and fur, and transparent objects need the optional offline model or manual refinement. Timing is unchanged (0.3–1.3 s).

## Offline AI subject selection

Optional subject selection uses a user-selected, hash-verified local ONNX pack. No download, account or runtime network is needed. The model's mask is now upsampled and then refined at full resolution with `refine_mask` (on a simulated 8× upsampled model mask: boundary error 31.7 % → 19.8 %, IoU 94.7 % → 96.8 %). No model pack was available on the Mac used for 2.3.1, so real-model quality with the refinement is unmeasured. The optional BiRefNet FP32 pack was measured earlier on an Intel Mac: 12.83 seconds model load, 9.78 seconds first inference, 6.77–6.85 seconds warm inference and about 9.82 GB peak process RSS. Three synthetic mask IoUs were 0.776–0.829. These are limited fixtures, not proof of professional matting quality. CoreML/DirectML performance is unverified. See image-ai-model-packs.md for the manifest contract.

## Clipboard dialog and export

Copy to Clipboard opens PNG/JPEG/TIFF/BMP settings with quality, dimensions, DPI, full-image/selection and alpha/background controls. Remembered settings can be reopened from the adjacent menu or Preferences. macOS publishes the selected native pasteboard type plus interoperable representations; Windows uses registered formats, CF_TIFF where relevant, DIBV5 and legacy DIB. Receiving applications choose their preferred representation. JPEG uses the chosen opaque background. Native macOS pasteboard encoding is tested (on a private pasteboard, never the user's clipboard); actual Office paste and Windows clipboard execution still require platform testing. Copy does not alter editable layers.

Export measures actual encoded bytes and can search quality and optionally reduce dimensions to meet a size target. PNG/JPEG/TIFF/WebP/BMP/GIF/AVIF/HEIF codecs were exercised in the frozen smoke run. Formats have different alpha, animation and colour limitations; export is a composite, not a layered interchange file. Keep the `.engimg` project. ICC metadata preservation is not a complete colour-managed editing pipeline.

## Interface

- Menu buttons (Recent, Image, Select, Layer, the layer "more" menu, Copy to Clipboard, the Vector Editor's Geometry) are `ui/widgets.DropdownButton`s, which paint the label and a vector chevron in separate space; short labels never shrink and long ones show a compact label or elide. On Windows and Linux combo boxes reserve their arrow area and draw an SVG chevron; macOS keeps native pop-up buttons.
- The status strip is `ui/view_options.ViewOptionsBar`, shared with the Vector Editor: status text gives way first, then the inline Grid/Snap/Rulers/units toggles hide; the View menu always lists them.
- Layers: drag to reorder, double-click to rename (text layers: edit), checkerboard thumbnails with lock and mask badges, tooltips with size, mask, transform and blend state, opacity slider.

## Limits and next steps

Current limits: 80 megapixels per image/layer, 64 layers, bounded archive assets and 256 MiB history (transformed layers also keep their originals, which count towards it). Rendering is not tiled and some operations remain synchronous on the GUI thread: splitting the preview at the start of a Move drag takes 39–71 ms on a 12 MP document with 2–5 layers (M3 Max), the auto-select hit test under 1 ms. Layers are always axis-aligned pixel arrays; a rotated layer's frame comes from its original pixels. Live preview of a dragged layer uses normal blending. There is no free distort/perspective transform of a layer, no vector/smart-object layers, no layer groups or clipping masks, and no adjustment layers. Advanced frequency separation, liquify, RAW development, advanced typography and Photoshop feature parity are not implemented.

Worth doing next: transform handles for the selection itself (move selected pixels without a separate layer), layer groups, a mask view/disable toggle, a brush to give automatic removal keep/remove hints, and validating the refinement with a real offline model on Apple Silicon (CoreML).
