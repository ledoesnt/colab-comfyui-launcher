---
name: colab-comfyui
description: Start, test, reconnect to, and clean up this repository's ComfyUI launcher on Google Colab. Use persistent Drive caches with verified VM copies, or explicitly selected temporary VM models and outputs. Use the terminal dashboard, SSH localhost forwarding, or explicitly selected public or email-protected Cloudflare access, with bounded MiniMax H3 rendering when requested.
---

# Colab ComfyUI

Use the repository scripts to run the workflow and report separately what was verified: runtime allocation, Drive access, ComfyUI execution, the selected external access mode, and optional H3 rendering. Work from the repository root, three directories above this skill folder. Read [README.md](../../../README.md) and inspect `python3 scripts/colabctl.py --help` before running commands.

## Choose an interface

- For guided startup or a user request to operate/test the TUI, use the terminal wizard below. An agent needs an interactive TTY / PTY with key input and screen output; keep it running while sending arrow keys and Enter, and verify the resulting screen and backend state. A CLI result alone does not prove that a TUI action worked.
- For command automation or an environment without an interactive terminal, use the CLI sections below. The CLI and TUI both run in a terminal and use the same launcher backend; choosing the CLI does not mean entering `colab console` manually. Preserve the user's selected interface when the environment supports it.

## Choose the runtime and cleanup scope

- Inspect the official CLI version, authentication, current sessions, and usage. The tested CLI is `google-colab-cli==0.7.4`; use explicit `--auth=oauth2`. Follow the CLI's current interactive authentication flow when needed. Do not reuse an old OAuth URL or ask the user to send tokens, authorization codes, or key passphrases in chat.
- Follow the user's requested hardware and existing authorization. Allocate a paid G4 GPU only when GPU use is authorized. Do not repeatedly request permission that the user already granted. A model-free connection test can run on CPU when no GPU is authorized.
- Record whether this run creates a new runtime or reuses one. Choose a unique session name for a new runtime; do not create a second runtime just because a command timed out. Keep the ownership record and any spending or time limit outside tracked files. Inspect actual allocated hardware; a requested GPU type alone is not evidence of allocation.
- Follow the user's selected access mode. `--local-only` starts ComfyUI for SSH localhost forwarding without Cloudflare; `--public` exposes ComfyUI to anyone who knows the URL, including task submission and access to inputs and outputs; `--allowed-email` requests an email login gate. Both bridge and runtime require exactly one choice. Prefer the dashboard's local-only default when no external public access is needed. Do not ask again when the user already authorized a mode, and do not silently switch an email-gated run to public after an authentication failure.
- Clarify only missing details that affect execution, such as access mode when none was selected, a mailbox when email mode was chosen, unresolved hardware choice, or whether an existing runtime may be stopped. Continue independent installation work while waiting. A spending limit is a stopping condition, not permission to exceed it.
- For a test-and-cleanup request, release an agent-created runtime in cleanup even if installation or validation fails. When the user wants an interactive running UI, leave it running as requested and give the exact stop command. Never terminate a reused runtime without authorization to do so.

## Terminal wizard (TUI)

Launch from the repository root in an interactive terminal / PTY:

```bash
python3 scripts/dashboard.py
```

The default is an arrow/Enter wizard: new or existing runtime → compute / GPU model → Drive or temporary VM storage → configuration summary → confirmed creation / continuation → background preparation → browser-ready overview. Arrow keys never invoke commands. Esc returns/cancels; PgUp/PgDn scroll the right-hand details. Inputs use a default placeholder only while empty. A missing dedicated SSH key requires explicit creation or an existing path; never overwrite a key.

Account login and Drive consent run in an actual controlling child PTY while the UI stays on screen. Authorization URLs and instructions appear transiently on the right. The user selects Open authorization in browser and completes the current provider flow; only their explicit confirmation/code is sent. Code input is masked and cleared after submission. Exit cancels the owned CLI coordination, retaining the VM. A successful CLI exit still requires actual mounted Drive and MyDrive validation. Do not save authorization frames or raw transcripts in screenshots, logs or Git.

Existing runtimes are inspected before any mutation. Restore only complete saved storage/access configuration; unknown configuration requires a storage choice. Already running installation/preparation is waited on, ready steps are skipped, and uncertain state is not restarted automatically. A new runtime reports not_deployed before uploading scripts; this is normal and does not require Drive. The default wizard performs deployment automatically after confirmed allocation. The Ready overview offers browser access, Exit and keep resources, End this VM with a separate default-Cancel confirmation, other runtimes, and Advanced actions. Summary/failure also exposes advanced recovery when an existing service needs explicit stopping.

Exit retains remote resources, owned SSH and running tasks; End this VM releases only the selected authorized runtime. Cleanup queued during startup is bound to the original session/key/port. Do not claim a result from a selected menu item before Enter or before verification. `--classic` retains the legacy action-code interface for advanced usage.

`--demo` is an offline read-only fixture, with prepare/ready/error variants and navigable configuration previews; never use it as cloud evidence. `--no-color`/`NO_COLOR` disable the brand palette; unsupported terminals use numeric choices followed by Enter. Use the full-screen terminal for sensitive provider code input. Follow the user's screenshot requirement by capturing each real operation stage after sensitive authorization data has cleared, recording Demo and live tests separately.

Read the [Advanced actions table](../../../README.md#advanced-actions) when managing an individual step. Normal startup already handles Drive mounting (or skips it for temporary storage), GPU model preparation, service startup and, in the default local-only mode, SSH forwarding.

The home screen performs read-only Colab login verification before new/existing-runtime navigation; use the explicit Authorize / check Colab login entry when required. Network errors are unverified status, not evidence of logout. Creating a VM rechecks login. Do not silently bypass this prerequisite or treat an unverified CLI exit as authenticated.

Use Manage models on home, configuration summary, or Advanced actions for a saved checkbox catalog. Six built-in files start enabled; Enter toggles auto_download and saves immediately without deleting weights. Public Hugging Face file URLs can be added after category selection and confirmation; only public metadata is fetched, then a pinned extra-added manifest is saved. Private/gated or missing-LFS-SHA files require manually verified metadata; never collect tokens in the form. Disabled required weights may make a workflow unusable. Default preparation filters disabled files after validating the entire catalog, including collisions.

HTTPS downloads default to two file workers; --workers 1–4 and the TUI Parallel downloads setting control this. Drive copies stay sequential; only missing downloads run concurrently. Each file retains complete SHA/resume checks, while the parent owns receipts and progress. Local real-HTTP concurrency tests do not prove HF/CDN/Drive FUSE cloud performance. Keep currently running preparation unchanged; deploy new lists only after it ends.

For added models, read [docs/models.md](../../../docs/models.md). Edit `models/extra.json`; use a separate `models/extra-*.json` for each additional pinned Hugging Face repository. Default download, preparation and readiness checks merge these lists. In an existing GPU runtime use Advanced actions → Prepare / refresh models: deploy the new lists, verify missing models, and retain existing services/SSH. An already running preparation is waited on without deploying edits; run the action again after it finishes to apply those edits. A model file does not install custom nodes or establish architecture compatibility.

Busy actions appear at the top and in the first right-hand status block; blue ABOUT THIS CHOICE text is help, not execution evidence. Wait for verified ComfyUI and SSH status before claiming ready. Save / Save As writes workflows into the selected storage's `user/default/workflows`; persistent Drive workflows reopen from Workflows on a later run with the same storage directory. Export downloads a local JSON; unsaved browser drafts are not a persistence test.

## CLI: allocate a runtime

Example allocation after the user authorizes G4:

```bash
colab --auth=oauth2 new -s "$SESSION" --gpu G4
```

Inspect `new --help` if the installed CLI rejects that GPU name. Do not silently substitute a more expensive GPU. A session name identifies the current runtime; it does not restore files after Colab recycles that runtime.

## CLI: mount Drive and install

For persistent assets, run the official mount command in a terminal that supports its interaction:

```bash
colab --auth=oauth2 drivemount -s "$SESSION" /content/drive
```

When the provider requests consent, including for a new runtime, let the user complete it through the displayed provider UI; do not simulate consent or replace it with an unrelated Drive connector. Do not request another human action when the provider already accepts existing consent. Do not scan the user's Drive. The dashboard defaults to the dedicated `/content/drive/MyDrive/colab-comfyui-launcher-test` directory; honor another dedicated directory the user specifies. If mounting is blocked, report it and use `--ephemeral` only with the user's acceptance of temporary assets.

Deploy and start the background installation from the local repository:

```bash
python3 scripts/colabctl.py -s "$SESSION" deploy
python3 scripts/colabctl.py -s "$SESSION" install --install-timeout 900
python3 scripts/colabctl.py -s "$SESSION" status
```

Installation being started is not installation being ready. Poll `status` with short bounded waits until installation reports `ready` or fails. Check sanitized installation diagnostics on failure; do not launch another installation blindly after a timeout. Colab CPU/GPU execution, mounting, and browser access do not require an SSH private key or a manual `colab console` shell for this bridge.

The bootstrap fixes the ComfyUI source commit and cloudflared version/checksum, and reuses the Colab torch environment. Its remaining dependencies depend on the Colab image and package resolution. Record the actual installed versions; do not describe this as a fully locked environment.

## CLI: prepare models on VM disk

Before GPU startup, read the H3 model prerequisites below. Drive is only the persistent cache: never point ComfyUI model configuration at Drive. The preparation operation downloads only missing pinned files into Drive, then copies each model into `/content/colab-comfyui-runtime/models` while computing SHA256 in the same pass:

```bash
STORAGE_ROOT=/content/drive/MyDrive/colab-comfyui-launcher-test
python3 scripts/colabctl.py -s "$SESSION" prepare \
  --download-missing --cache-root "$STORAGE_ROOT/models" --max-seconds 1800
python3 scripts/colabctl.py -s "$SESSION" status
```

Wait for `model_prepare.running: false`, `model_prepare.status: succeeded`, `model_prepare.result.ok: true`, and `models_ready: true`. Per-file download/copy/hash progress appears in `model_prepare.progress`. Existing legacy Drive models are verified during copy without a separate initial Drive hash. Within the same VM, verified receipts and unchanged metadata avoid reading all model bytes again. These metadata receipts are a trusted private-cache optimization, not fresh content hashing or protection against a same-account attacker. A new VM still copies and verifies the complete current manifest (approximately 42.03 GB by default). Add `--verify-cache` only when an explicit extra Drive content audit is needed. No cross-VM distributed writer lock is provided: use one writer per Drive cache.

For explicitly selected temporary GPU storage, skip Drive and use `prepare --ephemeral --download-missing --max-seconds 1800` without `--cache-root`. It streams downloads directly into the VM model directory, checks size/SHA256 before publishing each file, and avoids a second full-manifest copy. A verified metadata receipt is reusable only on the same VM boot; metadata reuse is not a new SHA. Wait for all models_ready gates, then start with `--ephemeral --local-only`. Models and outputs are lost when the VM is released.

CPU `--ephemeral --cpu` network/PNG tests do not require H3 preparation or Drive. Code, dependencies, model loading and progress records remain on VM disk. Persistent runs save output files under the selected Drive storage root; ephemeral output survives only while that VM is retained.

## CLI: start and verify

When the user has authorized public access, start explicitly in public mode with the dedicated Drive storage directory. Add `--cpu` when the selected runtime should use CPU:

```bash
STORAGE_ROOT=/content/drive/MyDrive/colab-comfyui-launcher-test
python3 scripts/colabctl.py -s "$SESSION" start --public --storage-root "$STORAGE_ROOT"
python3 scripts/colabctl.py -s "$SESSION" status
python3 scripts/colabctl.py -s "$SESSION" smoke
```

For email mode, obtain one explicit permitted mailbox, keep it in an untracked local variable, and use `start --allowed-email "$ALLOWED_EMAIL" --storage-root "$STORAGE_ROOT"` instead. Do not add `--public` to that command. Stop existing services before changing their mode.

For SSH access use `start --local-only --storage-root "$STORAGE_ROOT"`, wait for owned ComfyUI and HTTP readiness, then run:

```bash
python3 scripts/ssh_forward.py -s "$SESSION" start \
  --identity "$HOME/.ssh/colab_comfyui_launcher" --create-key
python3 scripts/ssh_forward.py -s "$SESSION" status
```

The explicit `--create-key` creates a dedicated unencrypted Ed25519 key only when absent and never replaces existing key material. Existing encrypted keys are not usable by this background helper; do not alter them or request their passphrase in chat. The helper checks an existing session, uses the official WebSocket proxy and OpenSSH, binds only `127.0.0.1:8188`, keeps per-runtime host keys, verifies its owned listener and actual ComfyUI HTTP readiness, and never allocates/releases a VM. Open its returned localhost URL. Test browser import, Run and output preview here too. SSH still crosses the network and is not a guarantee of faster access or a fix for browser client blocking.

`start` can launch background work. Verify subsequent status, including the expected `access_mode`, HTTP readiness, live services and successful startup result, instead of assuming the first response means that services are ready. The smoke test submits a real model-free `EmptyImage → SaveImage` workflow, receives WebSocket execution events, retrieves a 64×64 PNG, and compares its bytes with the storage output. It proves the ComfyUI pipeline, not H3 inference. Record whether storage was mounted Drive or ephemeral. Do not claim recovery after a new runtime unless the persisted asset was actually retrieved again.

The official CLI may exit 0 even when executed Python fails. Use the bridge's `LAUNCHER_RESULT` validation and require `ok: true`; a missing, duplicate, malformed, or error result is a failure. Keep the launcher safety tests passing with `python3 -m unittest discover -s tests -v` when changing its scripts. Its process controls must preserve start-time ownership, reject an occupied unowned 8188 port, refuse unmounted Drive storage, and clean up only their own processes.

The URL printed by cloudflared only proves URL provisioning. Confirm that ComfyUI and cloudflared remain alive and that the local HTTP and WebSocket smoke test passes. Then verify the selected external mode:

- **Public:** Open the URL without an email login. Press Ctrl+O to import [workflows/smoke-ui.json](../../../workflows/smoke-ui.json), click Run, and verify a live WebSocket, completed task, and output preview. Do not require OTP acceptance for a public run. Until the browser task actually completes, describe public browser access as pending rather than tested. The recorded IAB CPU test passed these UI steps; revalidate each new run.
- **Email:** Verify that unauthenticated GET redirects to login, POST `/prompt` is rejected, and `/ws` does not upgrade. Have the permitted user finish email OTP in the provider's browser UI, then test the page, live WebSocket and Queue task. Do not ask for OTPs in chat or extract cookies. If login fails, retain the user's selected mode unless they authorize a change.

Email testing has encountered a missing authentication-state cookie in the Codex in-app browser and `ERR_BLOCKED_BY_CLIENT` in a controlled Chrome attempt. Later, the user confirmed that their everyday Chrome manually opened the same SSH localhost editor successfully while the controlled tab remained blocked; IAB import/Run/preview also passed. These observations do not establish the blocking component or successful email login. Do not diagnose an extension from the error code alone, or change browser protections to hide a failing test. Record manual user confirmation separately from agent-observed UI evidence, and consult the current [test report](../../../docs/test-report.md). A fresh login from the original tunnel root URL in one browser profile is a possible troubleshooting step, not a verified fix. Quick Tunnel email authentication requires an interactive browser and does not provide unattended API authentication. URLs change on restart; Colab and Quick Tunnels do not guarantee continuous availability.

## Optional MiniMax H3

Read [docs/h3.md](../../../docs/h3.md), [docs/h3-i2v.md](../../../docs/h3-i2v.md), [models/h3.json](../../../models/h3.json), and the current [test report](../../../docs/test-report.md) when the user requests H3. The current default includes six files, approximately 42.03 GB: the original four baseline models, official I2V Turbo LoRA and an optional style embedding. The original T2V test remains `workflows/h3-api.json`; use `render --workflow h3-api.json` explicitly for it. Default render uses `workflows/h3-i2v-api.json`, derived from the official Popular I2V UI template in `workflows/h3-i2v-ui.json`, with a pinned dedicated example image prepared by the runner. Browser acceptance requires actually opening the Popular I2V template, selecting/uploading the image, pressing Run and verifying output. Check the submitted Turbo flag and actual steps rather than inferring them from the LoRA filename or inner subgraph defaults. For workflow persistence, actually Save and reopen it from Workflows; record same-service, service-restart and fresh-VM evidence separately. Historical four-model T2V timings and verification remain in the report; they do not substitute for current I2V validation or a controlled transfer comparison. The 2026-10-06 browser base/Turbo runs completed; a separate API run after service restart exceeded a 120-second test deadline. Do not present that API regression as completed, or extend a runtime budget to retry it without authorization.

Check the original model's applicable license, actual runtime location, storage capacity, GPU support, and test budget before downloading. The project's own open-source license does not grant model rights. Do not improvise substitute weights, unverified repositories, or unofficial node patches. Verify every downloaded file against the pinned manifest size and SHA256 before using it. If a prerequisite is unresolved, complete the launcher tests and state the exact remaining H3 prerequisite.

Use `prepare --download-missing` above for first use and new VMs; do not run a separate full-cache download check before preparing legacy models. For a standalone Drive-cache download task only, keep `--models-root` aligned with the selected storage root and shorten deadlines when the user's remaining budget requires it:

```bash
python3 scripts/colabctl.py -s "$SESSION" download \
  --models-root "$STORAGE_ROOT/models" --max-seconds 1800
python3 scripts/colabctl.py -s "$SESSION" status
```

Wait until `model_download.running` is false, its status is `succeeded`, and its result has `ok: true`. Previously verified unchanged Drive receipts are skipped; partial downloads can resume. Download alone does not prepare local models: run prepare and verify `models_ready` before GPU startup. Do not start another runtime writing the same model directory. Once local models and owned G4 ComfyUI are ready:

```bash
python3 scripts/colabctl.py -s "$SESSION" render --max-seconds 900
python3 scripts/colabctl.py -s "$SESSION" status
```

The bridge invokes `scripts/render_workflow.py` remotely, submits the deployed API workflow, and checks the resulting media and storage bytes. Wait for `render.running: false`, `render.status: succeeded`, and `render.result.ok: true`; `started` or CLI exit 0 does not prove completion. On timeout inspect the recorded task state before resubmitting.

H3 success requires an actually executed workflow without node errors, a decodable MP4 with video/audio tracks, and verified persisted output. A GPU-visible status page or a model-free PNG is insufficient. Record failures and unexecuted steps honestly.

## Stop and publish when requested

For service cleanup:

```bash
python3 scripts/colabctl.py -s "$SESSION" stop
python3 scripts/ssh_forward.py -s "$SESSION" stop
```

This stops owned launcher services while retaining the Colab runtime. To finish an authorized test of an agent-created runtime, also run:

```bash
colab --auth=oauth2 stop -s "$SESSION"
colab --auth=oauth2 sessions
```

Verify that the owned session is gone. Closing a terminal alone is not runtime cleanup. Do not stop unrelated sessions or delete Drive assets, models, or the user's SSH keys during cleanup.

Publish to GitHub only when the user requests it, preserving their requested visibility and acceptance conditions. Existing explicit authorization to push and make the repository public does not need another confirmation. Validate the user's final selected mode: when they have explicitly chosen public access, a browser Queue task through that URL can satisfy browser acceptance without email OTP. A provisioned URL or local PNG alone cannot satisfy browser acceptance or H3 inference. Finish the required checks before conditional publication. Before committing, inspect the complete staged file list and content: exclude credentials, personal email values, OAuth/login URLs, runtime state, logs, model weights, generated assets, caches, and virtual environments. Verify current tests and documented success boundaries, then publish only this project's code, configuration, and documentation. Report the repository URL and distinguish the tested selected mode from any other mode whose login remains unverified.
