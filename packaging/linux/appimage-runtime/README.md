# AppImage runtime

An AppImage is a small executable, the **runtime**, followed by a SquashFS image of the
application folder. When started, the runtime mounts the image read-only with FUSE (or extracts
it with `--appimage-extract-and-run`) and runs `AppRun`. This project assembles the AppImage
itself (`packaging/linux/build_linux.sh`): runtime + `mksquashfs` image, which is what
`appimagetool` does, so no further downloaded tool is involved.

## Pinned runtime

| | |
|---|---|
| Project | AppImage type 2 runtime, https://github.com/AppImage/type2-runtime |
| Release | `20251108` (tag commit `dd6cebedcbddde9c82f89b011e8e1d40b6e43868`) |
| File | `runtime-x86_64` from that release, with its detached signature `runtime-x86_64.sig` |
| Signing key | `type2-runtime-signing-key.asc`: ed25519 `570C 77AC EA40 C0F1 B758  902C BF96 CCA5 6490 F695`, "AppImage type 2 runtime <type2-runtime@appimage.org>", created 2024-05-04, expires 2034-05-03 |
| Key provenance | `signing-pubkey.asc` at the release tag; SHA-256 `db8b615eb5bbf5e8418d52906b4a492960926370e2c5041ad6acaccda56ef0c6` (identical at the `continuous` tag, `fedfad5`) |

`build_linux.sh` downloads the runtime and its signature, verifies the signature with `gpgv` against
this key only, and requires the exact SHA-256 below; either check failing stops the build. The hash
is recorded in the build report. `APPIMAGE_RUNTIME` uses a runtime file you built yourself from the
tagged source instead (then only `APPIMAGE_RUNTIME_SHA256`, if set, is checked).

| | |
|---|---|
| `runtime-x86_64` SHA-256 | `2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d` (944,632 bytes; static-pie, BuildID a87aaf5d…) |
| Signature | "Good signature from AppImage type 2 runtime", EdDSA key 570C77AC…6490F695, made 2025-12-08 17:51:02 UTC (checked 2026-10-29) |

## Licences (ship with the AppImage)

The runtime itself is MIT licensed (`LICENSE.txt`, from the release tag). It is a static binary
that includes:

- libfuse 3.15.0, with the runtime's `patches/libfuse/mount.c.diff` — LGPL-2.1-only (the library part)
- squashfuse 0.5.2 — BSD-2-Clause
- zstd and zlib (Alpine packages) — BSD-3-Clause / zlib licence
- mimalloc (Alpine package) — MIT
- musl libc — MIT

(From `scripts/common/install-dependencies.sh`, `scripts/docker/Dockerfile` and `src/runtime/Makefile`
at the release tag.)

Because libfuse is LGPL and statically linked, a release must point to the complete runtime source
(the tag above, which also builds libfuse and squashfuse from pinned sources) so users can rebuild
and relink it. This is listed in `docs/LICENSING.md` and in the release checklist.
