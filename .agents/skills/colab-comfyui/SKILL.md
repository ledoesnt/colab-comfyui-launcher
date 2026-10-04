---
name: colab-comfyui
description: Start, test, reconnect to, and clean up this repository's ComfyUI launcher on Google Colab with Google Drive persistence and an explicitly selected public or email-protected Cloudflare URL. Download and render MiniMax H3 through the repository's bounded local commands when requested and its prerequisites are met.
---

# Colab ComfyUI

Use the repository scripts to run the workflow and report separately what was verified: runtime allocation, Drive access, ComfyUI execution, the selected external access mode, and optional H3 rendering. Work from the repository root, three directories above this skill folder. Read [README.md](../../../README.md) and inspect `python3 scripts/colabctl.py --help` before running commands.

## Choose the runtime and cleanup scope

- Inspect the official CLI version, authentication, current sessions, and usage. The tested CLI is `google-colab-cli==0.7.4`; use explicit `--auth=oauth2`. Follow the CLI's current interactive authentication flow when needed. Do not reuse an old OAuth URL or ask the user to send tokens, authorization codes, or key passphrases in chat.
- Follow the user's requested hardware and existing authorization. Allocate a paid G4 GPU only when GPU use is authorized. Do not repeatedly request permission that the user already granted. A model-free connection test can run on CPU when no GPU is authorized.
- Record whether this run creates a new runtime or reuses one. Choose a unique session name for a new runtime; do not create a second runtime just because a command timed out. Keep the ownership record and any spending or time limit outside tracked files. Inspect actual allocated hardware; a requested GPU type alone is not evidence of allocation.
- Follow the user's selected access mode. `--public` exposes ComfyUI to anyone who knows the URL, including task submission and access to inputs and outputs; `--allowed-email` requests an email login gate. Both the local bridge and runtime require an explicit choice and reject selecting both. Do not ask again when the user already authorized a mode, and do not silently switch an email-gated run to public after an authentication failure.
- Clarify only missing details that affect execution, such as access mode when none was selected, a mailbox when email mode was chosen, unresolved hardware choice, or whether an existing runtime may be stopped. Continue independent installation work while waiting. A spending limit is a stopping condition, not permission to exceed it.
- For a test-and-cleanup request, release an agent-created runtime in cleanup even if installation or validation fails. When the user wants an interactive running UI, leave it running as requested and give the exact stop command. Never terminate a reused runtime without authorization to do so.

Example allocation after the user authorizes G4:

```bash
colab --auth=oauth2 new -s "$SESSION" --gpu G4
```

Inspect `new --help` if the installed CLI rejects that GPU name. Do not silently substitute a more expensive GPU. A session name identifies the current runtime; it does not restore files after Colab recycles that runtime.

## Mount Drive and install

For persistent assets, run the official mount command in a terminal that supports its interaction:

```bash
colab --auth=oauth2 drivemount -s "$SESSION" /content/drive
```

When the provider requests consent, including for a new runtime, let the user complete it through the displayed provider UI; do not simulate consent or replace it with an unrelated Drive connector. Do not request another human action when the provider already accepts existing consent. Do not scan the user's Drive. The launcher uses only `/content/drive/MyDrive/colab-comfyui` or the dedicated directory the user specifies. If mounting is blocked, report it and use `--ephemeral` only with the user's acceptance of temporary assets.

Deploy and start the background installation from the local repository:

```bash
python3 scripts/colabctl.py -s "$SESSION" deploy
python3 scripts/colabctl.py -s "$SESSION" install --install-timeout 900
python3 scripts/colabctl.py -s "$SESSION" status
```

Installation being started is not installation being ready. Poll `status` with short bounded waits until installation reports `ready` or fails. Check sanitized installation diagnostics on failure; do not launch another installation blindly after a timeout. Colab CPU/GPU execution, mounting, and browser access do not require an SSH private key or a manual `colab console` shell for this bridge.

The bootstrap fixes the ComfyUI source commit and cloudflared version/checksum, and reuses the Colab torch environment. Its remaining dependencies depend on the Colab image and package resolution. Record the actual installed versions; do not describe this as a fully locked environment.

## Start and verify

When the user has authorized public access, start explicitly in public mode with the dedicated Drive storage directory. Add `--cpu` when the selected runtime should use CPU:

```bash
STORAGE_ROOT=/content/drive/MyDrive/colab-comfyui
python3 scripts/colabctl.py -s "$SESSION" start --public --storage-root "$STORAGE_ROOT"
python3 scripts/colabctl.py -s "$SESSION" status
python3 scripts/colabctl.py -s "$SESSION" smoke
```

For email mode, obtain one explicit permitted mailbox, keep it in an untracked local variable, and use `start --allowed-email "$ALLOWED_EMAIL" --storage-root "$STORAGE_ROOT"` instead. Do not add `--public` to that command. Stop existing services before changing their mode.

`start` can launch background work. Verify subsequent status, including the expected `access_mode`, HTTP readiness, live services and successful startup result, instead of assuming the first response means that services are ready. The smoke test submits a real model-free `EmptyImage → SaveImage` workflow, receives WebSocket execution events, retrieves a 64×64 PNG, and compares its bytes with the storage output. It proves the ComfyUI pipeline, not H3 inference. Record whether storage was mounted Drive or ephemeral. Do not claim recovery after a new runtime unless the persisted asset was actually retrieved again.

The official CLI may exit 0 even when executed Python fails. Use the bridge's `LAUNCHER_RESULT` validation and require `ok: true`; a missing, duplicate, malformed, or error result is a failure. Keep the launcher safety tests passing with `python3 -m unittest discover -s tests -v` when changing its scripts. Its process controls must preserve start-time ownership, reject an occupied unowned 8188 port, refuse unmounted Drive storage, and clean up only their own processes.

The URL printed by cloudflared only proves URL provisioning. Confirm that ComfyUI and cloudflared remain alive and that the local HTTP and WebSocket smoke test passes. Then verify the selected external mode:

- **Public:** Open the URL without an email login. Press Ctrl+O to import [workflows/smoke-ui.json](../../../workflows/smoke-ui.json), click Run, and verify a live WebSocket, completed task, and output preview. Do not require OTP acceptance for a public run. Until the browser task actually completes, describe public browser access as pending rather than tested. The recorded IAB CPU test passed these UI steps; revalidate each new run.
- **Email:** Verify that unauthenticated GET redirects to login, POST `/prompt` is rejected, and `/ws` does not upgrade. Have the permitted user finish email OTP in the provider's browser UI, then test the page, live WebSocket and Queue task. Do not ask for OTPs in chat or extract cookies. If login fails, retain the user's selected mode unless they authorize a change.

Email testing has encountered a missing authentication-state cookie in the Codex in-app browser and `ERR_BLOCKED_BY_CLIENT` in a separate Chrome attempt. The specific browser cause is unresolved; do not state that cookie policy or an extension was conclusively diagnosed. A fresh login from the original tunnel root URL in one browser profile is a possible troubleshooting step, not a verified fix. Report the observed gate and login failure honestly instead of calling email mode successful. Quick Tunnel email authentication requires an interactive browser and does not provide unattended API authentication. URLs change on restart; Colab and Quick Tunnels do not guarantee continuous availability.

## Optional MiniMax H3

Read [docs/h3.md](../../../docs/h3.md), [models/h3.json](../../../models/h3.json), [workflows/h3-api.json](../../../workflows/h3-api.json), and the current [test report](../../../docs/test-report.md) when the user requests H3. The tested baseline includes all four approximately 40 GB files passing size/SHA256 checks, and a real 864×480, 124-frame, 24 fps, 20-step render with seed 20261004. It generated a 5.17-second stereo MP4 in 99.251 seconds including validation. The MP4 was downloaded again through Drive after stopping G4 and retained the same hash. A fresh G4 also completed the corrected bootstrap in one installation. These results do not replace validation of a new run or prove every future Colab image compatible.

Check the original model's applicable license, actual runtime location, storage capacity, GPU support, and test budget before downloading. The project's own open-source license does not grant model rights. Do not improvise substitute weights, unverified repositories, or unofficial node patches. Verify every downloaded file against the pinned manifest size and SHA256 before using it. If a prerequisite is unresolved, complete the launcher tests and state the exact remaining H3 prerequisite.

Run model download and rendering from the local repository, without entering console. Keep `--models-root` aligned with the `models` child of the same storage root used for `start`, and shorten deadlines when the user's remaining budget requires it:

```bash
python3 scripts/colabctl.py -s "$SESSION" download \
  --models-root "$STORAGE_ROOT/models" --max-seconds 1800
python3 scripts/colabctl.py -s "$SESSION" status
```

Wait until `model_download.running` is false, its status is `succeeded`, and its result has `ok: true`. Already verified files are skipped; partial downloads can resume. Do not start another runtime writing the same model directory. Once the model files and owned G4 ComfyUI are ready:

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
```

This stops owned launcher services while retaining the Colab runtime. To finish an authorized test of an agent-created runtime, also run:

```bash
colab --auth=oauth2 stop -s "$SESSION"
colab --auth=oauth2 sessions
```

Verify that the owned session is gone. Closing a terminal alone is not runtime cleanup. Do not stop unrelated sessions or delete Drive assets, models, or the user's SSH keys during cleanup.

Publish to GitHub only when the user requests it, preserving their requested visibility and acceptance conditions. Existing explicit authorization to push and make the repository public does not need another confirmation. Validate the user's final selected mode: when they have explicitly chosen public access, a browser Queue task through that URL can satisfy browser acceptance without email OTP. A provisioned URL or local PNG alone cannot satisfy browser acceptance or H3 inference. Finish the required checks before conditional publication. Before committing, inspect the complete staged file list and content: exclude credentials, personal email values, OAuth/login URLs, runtime state, logs, model weights, generated assets, caches, and virtual environments. Verify current tests and documented success boundaries, then publish only this project's code, configuration, and documentation. Report the repository URL and distinguish the tested selected mode from any other mode whose login remains unverified.
