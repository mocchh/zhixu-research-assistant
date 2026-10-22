# Real browser regression tests

Run from `frontend/` after installing the repository's Python 3.12 dependencies into `.venv`:

```sh
npm ci
npx playwright install chromium
npm run test:e2e
```

On Ubuntu CI, install browser system packages with `npx playwright install --with-deps chromium firefox webkit`. The CI job does not need the project's original PDFs, model keys, or a running application. `npm run test:e2e -- --grep "reader"` runs one matching scenario; `--repeat-each=2` repeats the suite.

The default engine is Chromium. To run the complete compatibility matrix:

```sh
npx playwright install chromium firefox webkit
E2E_BROWSERS=chromium,firefox,webkit npm run test:e2e
```

`E2E_BROWSERS=firefox,webkit` selects a subset. Tests run sequentially against the temporary service and reset its workspace before every scenario. A narrow viewport with touch inputs checks responsive layouts in all engines; this is not testing a physical phone. WebKit is the Playwright engine build, not a test of an installed Safari application.

On macOS, the runner also gives Firefox a private temporary app-data root through `CFFIXED_USER_HOME`, scoped to its browser child process. Firefox's `-profile` argument alone does not isolate this directory. This avoids the [upstream macOS app-data access issue](https://github.com/microsoft/playwright/issues/42768) without reading the user's Firefox profile or changing system privacy settings. The runner precreates its private `Library/Application Support`, `Library/Caches`, `Downloads`, and `Documents` directories so macOS does not add delete-denying ACLs to them. Both the temporary profile and app-data root are removed after the run; cleanup allows a short retry for browser background writes.

## Isolation

`scripts/e2e.sh` builds the production UI, creates a marked temporary workspace, starts the real FastAPI application on a prebound ephemeral loopback port, and runs Playwright. Both the import-time application and the explicit test application use that temporary database. Dotenv loading is disabled and the initial model key is empty. Translation and structured-answer scenarios configure a temporary local HTTP protocol stub; model settings are reset between scenarios. Browser requests to external origins fail the test.

The runner stops the server and removes its database, uploaded files, and generated fixtures on success, failure, or a handled interrupt. Ports 8000 and 5173 are forbidden. `E2E_PORT` can select another explicit port when needed; the default is an OS-assigned available port. Production Origin protection is unchanged: only this test application's actual loopback origin is added through `extra_local_origins`.

The fixtures are generated with PyMuPDF: a native three-page paper, a byte-identical duplicate, a malformed file, and a 48-page task-control document. All responses use the real API, parser, retrieval, and SQLite storage. The test server adds 180 ms between actual parsed pages only for the designated control fixture, and a 1.5-second delay before explicitly marked chat requests. These bounded delays expose cancellation checkpoints. The sentence-translation scenario uses the real model HTTP client with an explicit same-origin protocol stub, which returns source IDs in reverse order. The stub and reset endpoint exist only in this isolated test entrypoint and never call an external provider.

## Coverage and artifacts

Twenty scenarios cover upload/deduplication/errors; extractive chat and source positioning; saved sessions and notes; immutable note revisions and Markdown export; reading search, references, text layer and saved position; task pause/resume/cancel/retry; interrupted chat and session isolation; a 390-pixel mobile viewport; sentence/source pairing and durable translation caches; selected-source translation and page-switch cleanup; full reparse with note preservation; mobile sentence pairs without a configured model; structured answers with source navigation and preserved rejected drafts; note navigation and edits during delayed saves; chat save failures and storage-only retries; provider-bound keys and persistent disconnect; and mixed upload results. This suite does not assess model answer quality or OCR accuracy.

The multiwindow and close-protection scenarios exercise a real two-window note revision conflict with draft export and save-as; a real chat revision conflict with automatic message merging; and the before-unload protection for unsaved notes and generated answers. Conflict tests delay a write but do not mock a successful write or the 409 response.

The close checks trigger the browser's native `beforeunload` dialog after user interaction, dismiss it, verify the unsaved content, save, and reload from SQLite. They do not simulate a process kill or prove that an operating system will always show a prompt. Unsaved content remains in page memory until saved.

Reports persist under `frontend/test-results/e2e/`; `E2E_REPORT_DIR` chooses a separate directory (relative paths are resolved from `frontend/`):

- `junit.xml`: machine-readable results.
- `report.json`: detailed machine-readable results, including each engine and any skips.
- `html/`: browsable report (`npx playwright show-report test-results/e2e/html`).
- `artifacts/`: screenshots and traces for failures.
- `server.log`: isolated server diagnostics.
- Screenshots use engine prefixes (for example, `firefox-settings-key-isolation.png` and `webkit-import-partial-result.png`) so matrix runs do not overwrite one another.

`npm test` continues to run the fast unit/component tests under `src/`; browser tests are a separate command. To use a custom browser cache, set the same `PLAYWRIGHT_BROWSERS_PATH` during browser installation and test execution.
