#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Run on the Colab VM. Drive and SSH are not required for installation.
readonly RUNTIME_ROOT=/content/colab-comfyui-runtime
readonly COMFY_COMMIT=f1072eb0350638a3390ddb6afbcaa8c6b237c6fd
readonly CLOUDFLARED_VERSION=2026.9.3
readonly CLOUDFLARED_SHA=77e26d8d900e0b8469f416239d14b5f296525fdf79fee6f511ef55609e3fbac2
[[ ${1:-} == install ]] || { echo 'Usage: bash scripts/bootstrap.sh install' >&2; exit 2; }
[[ $(uname -m) == x86_64 ]] || { echo 'Only Linux amd64 is tested' >&2; exit 2; }
mkdir -p "$RUNTIME_ROOT"/{bin,logs}
exec 9>"$RUNTIME_ROOT/install.lock"
flock -n 9 || { echo 'An installation is already running' >&2; exit 1; }

write_status() {
  python3 - "$RUNTIME_ROOT/install.json" "$1" "${2:-$1}" <<'PY'
import json, os, sys, time
from pathlib import Path
p=Path(sys.argv[1]); tmp=p.with_suffix('.tmp')
tmp.write_text(json.dumps({'status':sys.argv[2], 'phase':sys.argv[3], 'updated_at':time.time()}))
os.replace(tmp,p)
PY
}
trap 'write_status failed' ERR
trap 'write_status interrupted; exit 143' TERM INT
write_status installing source

if [[ ! -d "$RUNTIME_ROOT/ComfyUI/.git" ]]; then
  git clone --filter=blob:none --no-checkout https://github.com/Comfy-Org/ComfyUI.git "$RUNTIME_ROOT/ComfyUI"
fi
git -C "$RUNTIME_ROOT/ComfyUI" fetch --depth 1 origin "$COMFY_COMMIT"
git -C "$RUNTIME_ROOT/ComfyUI" checkout --detach "$COMFY_COMMIT"
write_status installing environment
python3 -m venv --without-pip --system-site-packages "$RUNTIME_ROOT/.venv"
# Reuse the Colab CUDA build. Do not install another multi-GB torch wheel.
"$RUNTIME_ROOT/.venv/bin/python" -c 'import torch; print("torch",torch.__version__,"CUDA",torch.version.cuda)'
write_status installing packages
"$RUNTIME_ROOT/.venv/bin/python" -m pip install --disable-pip-version-check \
  -r "$RUNTIME_ROOT/ComfyUI/requirements.txt" websocket-client==1.8.0 jedi==0.19.2
write_status installing dependency_check
"$RUNTIME_ROOT/.venv/bin/python" -m pip check

binary="$RUNTIME_ROOT/bin/cloudflared"
write_status installing cloudflared
if ! echo "$CLOUDFLARED_SHA  $binary" | sha256sum --check --status; then
  curl --fail --location --retry 3 --connect-timeout 15 --max-time 180 \
    --proto '=https' --proto-redir '=https' \
    "https://github.com/cloudflare/cloudflared/releases/download/$CLOUDFLARED_VERSION/cloudflared-linux-amd64" \
    --output "$binary.partial"
  echo "$CLOUDFLARED_SHA  $binary.partial" | sha256sum --check
  chmod 700 "$binary.partial"
  mv "$binary.partial" "$binary"
fi
"$binary" --version
"$binary" tunnel --help | grep -F -- '--allowed-mail' >/dev/null
"$RUNTIME_ROOT/.venv/bin/python" -m pip freeze > "$RUNTIME_ROOT/logs/environment.freeze.txt"
write_status ready
echo 'Installation ready'
