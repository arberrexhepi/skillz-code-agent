# Third-party licenses in desktop releases

[Root README](../readme.md) · [Desktop development](../desktop/README.md#development)

Skillz's Apache 2.0 license and arbër inc copyright notice describe the project. Dependencies retain their original licensing and attribution. Desktop builds generate a concise `THIRD_PARTY_NOTICES.txt` index stating that each dependency retains its own license. Original license and attribution files ship separately; the index points to them rather than embedding their contents.

## What generation covers

- Installed desktop runtime dependencies, including transitive and available optional dependencies.
- Packages whose source is present in the main, preload, or renderer build, including packages listed under `devDependencies` when their code is bundled.
- Electron, which is a shipped runtime even though it is listed under `devDependencies`.
- Original license and copyright text, upstream `NOTICE` and `ThirdPartyNotices` documents, and embedded bundle license sidecars such as Playwright's `.js.LICENSE` files.

DOMPurify's available Apache 2.0 option is recorded as the selected license. Each upstream document is copied unchanged. References inside notices are not followed or recursively expanded. Monaco's own notices already describe components embedded in its workers.

Development tools whose code is absent from the release are excluded. Python provider SDKs, optional browser downloads, and artifact dependencies installed separately keep their supplied licenses; this desktop manifest does not audit separately distributed Python environments, browsers, or Docker images. Artifact templates and prebuilts ship source, excluding cached `node_modules` and `dist` output and Git metadata at every directory depth.

## Build and package

Run from the repository root:

```bash
npm --prefix desktop run build
npm --prefix desktop run notices:check
npm --prefix desktop run package
```

The Vite builds write package inventories and output hashes under `desktop/out/licenses/`. The `postbuild` step generates `THIRD_PARTY_NOTICES.txt`, `manifest.json`, and a `third-party-licenses/` directory containing the original documents there. These files are generated build output and are ignored by Git; rebuild after changing dependencies or source. Do not edit the generated notice text.

Electron-builder's `beforePack` hook regenerates notices and rejects changed build outputs, version mismatches with `package-lock.json`, missing dependency license text, and missing required Monaco/Playwright attribution documents.

The `afterPack` hook copies the actual target Electron distribution's license and Chromium credits into the app's resources, then verifies:

- The project's `LICENSE` and `NOTICE` match their source documents.
- The concise notice index, manifest, and separately shipped original documents match the generated build.
- The packaged Electron license matches the installed Electron version, and its Chromium credits are preserved.
- Packaged build outputs match the recorded hashes.
- Every packaged npm dependency has matching notice coverage, including dependencies inside nested `node_modules` directories.
- Artifact resources contain source without dependency caches, compiled output, or Git metadata.

A failed check stops packaging before the installer is produced. These checks run for both `package` and `dist`, including direct electron-builder invocations using the repository configuration.

## Where notices ship

Inside the application's resources directory:

```text
LICENSE
NOTICE
THIRD_PARTY_NOTICES.txt
third-party-licenses/
  manifest.json
  packages/
    react/LICENSE
    playwright/NOTICE
    ...
  electron/
    LICENSE
    LICENSES.chromium.html
```

On Windows and Linux, resources are under the unpacked application's `resources/` directory. On macOS, they are inside `<product>.app/Contents/Resources/`. Chromium credits are copied inside the macOS app because files beside the app would not accompany an installed app bundle.

## Verify a packaged app again

For the standard Windows unpacked output, run from the repository root:

```bash
npm --prefix desktop run notices:verify -- desktop/release/win-unpacked
npm --prefix desktop run test:notices
```

The verifier also accepts an unpacked Linux app directory, a macOS `.app` path, or the resources directory. It reads both ASAR archives and unpacked app layouts. To check a finished installer, unpack or install it and pass its application directory to the same command.

Regression checks use a real Vite build and ASAR archive. They cover bundled development dependencies, missing licenses and upstream notices, stale build output, changed or removed release notices, uncovered nested dependencies, artifact cache exclusions, concise index generation, preservation of original documents, and macOS notice placement. This is an attribution and packaging check; other obligations attached to newly introduced dependencies still need review.
