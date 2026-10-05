# Project instructions

- This repository is an independent Colab infrastructure launcher. Keep provider details here; do not change a separate application's Domain or public Render API.
- Read README.md, docs/test-report.md and the relevant script before changing behavior. Distinguish executed tests from static configuration or mocked tests.
- Use the existing checkout. Follow the user's existing hardware, access-mode and cleanup authorization; do not repeatedly ask for permissions already given.
- Keep Drive and browser authentication interactive when the provider requires it. Never extract tokens/cookies or ask for OTPs in chat.
- Default persistent mode caches models and outputs on Drive while preparing verified regular models on VM disk. Explicit ephemeral mode downloads verified models directly to VM disk and retains outputs only for that VM. Load ComfyUI models only from VM disk in both modes; never restore direct Drive loading.
- Preserve explicit `--local-only` / `--public` / `--allowed-email` selection, process-start identity checks, bounded background tasks and dedicated storage directories. Dashboard quit retains resources; release is a separate action.
- First preparation hashes while copying; same-VM metadata receipts avoid unnecessary rereads. Clearly distinguish receipt reuse from a new content hash, and retain the explicit full-cache audit option.
- Cleanup only processes and Colab sessions created by this run or explicitly authorized by the user. Closing a terminal does not release a runtime.
- Test locally with `python3 -m unittest discover -s tests -v` and `bash -n scripts/bootstrap.sh`. Tests must not allocate runtimes or use real credentials.
- Never commit model weights, generated media, runtime files, private probes, logs, personal email values, credentials, OAuth URLs or virtual environments.
- Respect any user condition on publishing. A successful H3 API render does not prove that a browser workflow succeeded.
