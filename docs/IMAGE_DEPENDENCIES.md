# Image and vector dependency/license inventory

Reviewed 2026-10-26 and updated 2026-10-27 against installed package metadata, shipped notices and linked primary sources. This is an engineering inventory, not a completed release clearance. No external editor source or trained model weights are included in the app source or installer. An optional separately downloaded BiRefNet model pack has its own provenance, hashes, MIT license and benchmark record. The application remains under its existing AGPL-3.0-or-later license.

| Added dependency | Pinned build version | Purpose | License evidence and distribution considerations |
|---|---|---|---|
| NumPy | 2.5.4 | Mask/pixel operations | Installed metadata: BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0. Preserve the complete bundled license tree; native numerical components have notices of their own. |
| opencv-python-headless | 5.0.0.93 | Traditional selection, morphology, contour tracing, inpainting | Wheel metadata identifies Apache-2.0. `LICENSE-3RD-PARTY.txt` includes native dependencies; it must accompany the binary, including applicable FFmpeg/LGPL and codec notices. The headless package avoids a conflicting second Qt runtime. |
| pillow-heif | 1.8.1 | HEIF decoding/encoding where capability-tested | Python wrapper is BSD-3-Clause. Its wheel notice lists libheif 1.23.6 LGPLv3, libde265 1.1.3 LGPLv3 and x265 4.3 GPLv2. The [x265 4.3 source header](https://raw.githubusercontent.com/Multicorewareinc/x265/4.3/source/x265.h) expressly permits GPLv2-or-later. Preserve all notices and corresponding sources; do not treat the entire wheel as BSD. HEVC patent considerations are separate from copyright licensing and remain a public-release review item. |
| ONNX | 1.23.2 | Validate local graphs and reject external tensors | Installed metadata: Apache-2.0; preserve LICENSE and NOTICE. Imported only on AI use. |
| ONNX Runtime | 1.23.2 | Local model inference | Installed metadata: MIT; preserve `ThirdPartyNotices.txt` as well as LICENSE. Native runtime telemetry is disabled before session creation. No model is included. |
| ezdxf | 1.4.5 | Read/write explicitly supported DXF entities | [Official documentation](https://ezdxf.readthedocs.io/en/stable/introduction.html): MIT. Includes additional dependencies such as fontTools and pyparsing, whose notices are collected. It is not a DWG converter or CAD kernel. |
| defusedxml | 0.7.1 | Reject XML entity expansion in SVG parsing | Installed metadata: PSFL, with license file retained. Safe XML parsing does not by itself make every SVG feature safe; external references, scripting, limits and unsupported constructs need separate validation. |

Pillow, PySide6/Qt, PyMuPDF, python-pptx and other pre-existing dependencies retain their existing notices and obligations. Pinned build locks enumerate transitive dependencies. Model weight licensing is distinct from runtime licensing: each trained package needs its exact checkpoint license, origin/hash, conversion provenance and benchmark record. See `docs/image-ai-model-packs.md`.

## Preserved notices

`packaging/collect_licenses.py` copies complete installed package license/notice files and metadata license text into `build/legal-notices/`. Both platform specs embed this under `legal/dependencies/`. It also writes `BUILD-ENVIRONMENT-INVENTORY.json`, containing package names, versions, license expressions and notice filenames without local installation paths. That inventory describes the isolated build environment; it is not represented as a complete binary SBOM.

Additional exact-version HEIF native-library license texts and the x265 copyright/license grant are stored under `packaging/licenses/` and embedded under `legal/codec-licenses/`. `packaging/licenses/SOURCES.json` records their source URLs and SHA-256 hashes. The wheel's own bundled-library notices remain preserved as well.

## Before public distribution

On 2026-10-27, `pip-audit` checked the 31-package Intel runtime dependency closure, including optional AI runtimes, and returned no known Python-package vulnerabilities. The requirements snapshot and JSON report remain in the ignored private `audit/` directory. This result does not cover every statically bundled native-library vulnerability and is not a security guarantee.

Review the exact finished artifact's native libraries, notices, corresponding-source/relinking requirements and patent-sensitive codec distribution. Assemble corresponding source for the application's AGPL and applicable GPL/LGPL components alongside an approved release. Run vulnerability scanning on the pinned environment and inspect the native-library versions separately; Python advisory scanning does not establish that every bundled codec is vulnerability-free. Resolve any private paths/metadata reported by `tools/check_bundle.py`. Obtain the owner's explicit approval before publishing source, model packages or binaries.

No report here claims that these pending release steps have already been completed. Local development builds remain private.
