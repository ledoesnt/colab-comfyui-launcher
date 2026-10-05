#!/usr/bin/env python3
"""Deploy and control launcher services through the official Colab CLI.

This bridge never allocates or stops Colab runtimes and never grants Drive
permissions. Those actions remain explicit official ``colab`` commands.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REMOTE_ROOT = "/content/colab-comfyui-launcher"
REMOTE_STATE = "/content/colab-comfyui-runtime"
RESULT_PREFIX = "LAUNCHER_RESULT="
REMOTE_EXEC_TIMEOUT = 60
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class BridgeError(Exception):
    """An operation failed before its result was verified."""


def sanitize_failure(value: str) -> str:
    """Keep useful diagnostics while removing URLs and common secret forms."""
    value = ANSI.sub("", value)
    value = re.sub(r"https?://[^\s\"'<>]+", "[redacted URL]", value)
    value = re.sub(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", "[redacted email]", value)
    value = re.sub(
        r"(?i)\b(?:bearer\s+)[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", value
    )
    value = re.sub(
        r"(?i)([\"']?\b(?:[\w-]*(?:token|secret|password|passphrase)|"
        r"api[_-]?key|authorization[_-]?code)\b[\"']?\s*[=:]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1[redacted]",
        value,
    )
    value = re.sub(
        r"\b(?:sk-[A-Za-z0-9_-]{10,}|AIza[A-Za-z0-9_-]{20,})\b", "[redacted key]", value
    )
    return "\n".join(value.strip().splitlines()[-8:])[-2000:]


def _failure_summary(completed: subprocess.CompletedProcess[str]) -> str:
    return sanitize_failure(completed.stderr + "\n" + completed.stdout)


def _run_colab(
    arguments: list[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("colab")
    if not executable:
        raise BridgeError("Official colab CLI was not found on PATH.")
    try:
        return subprocess.run(
            [executable, "--auth=oauth2", *arguments],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(
            "Local Colab command timed out. Check status before repeating an operation."
        ) from exc
    except OSError as exc:
        raise BridgeError(
            f"Could not run the Colab CLI ({type(exc).__name__})."
        ) from exc


def parse_result(output: str) -> dict[str, Any]:
    lines = [
        line[len(RESULT_PREFIX) :]
        for line in ANSI.sub("", output).splitlines()
        if line.startswith(RESULT_PREFIX)
    ]
    if len(lines) != 1:
        raise BridgeError(
            "Expected exactly one LAUNCHER_RESULT response from the remote operation."
        )
    try:
        result = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise BridgeError("Remote launcher returned invalid result JSON.") from exc
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        raise BridgeError("Remote launcher response must contain a boolean ok field.")
    if result["ok"] is not True:
        message = result.get("error", "Remote launcher reported a failure.")
        raise BridgeError(sanitize_failure(str(message)))
    return result


REMOTE_HELPERS = """\
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
import uuid
import zipfile

def redact(text):
    text = re.sub(r"https?://[^\\s\\\"'<>]+", "[redacted URL]", str(text))
    text = re.sub(r"\\b[^\\s@]+@[^\\s@]+\\.[^\\s@]+\\b", "[redacted email]", text)
    text = re.sub(
        r"(?i)([\\\"']?\\b(?:[\\w-]*(?:token|secret|password|passphrase)|api[_-]?key|authorization[_-]?code)"
        r"\\b[\\\"']?\\s*[=:]\\s*)(?:\\\"[^\\\"]*\\\"|'[^']*'|[^\\s,;]+)",
        r"\\1[redacted]", text,
    )
    return "\\n".join(text.strip().splitlines()[-6:])[-1200:]

def atomic_json(path, data):
    temporary = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    temporary.write_text(json.dumps(data), encoding='utf-8')
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)

def deploy(payload):
    archive = Path('/content/colab-comfyui-launcher.zip')
    if hashlib.sha256(archive.read_bytes()).hexdigest() != payload['sha256']:
        raise RuntimeError('Uploaded deployment archive checksum did not match.')
    destination = Path('/content/colab-comfyui-launcher')
    if destination.is_symlink():
        raise RuntimeError('Deployment directory must not be a symlink.')
    destination.mkdir(parents=True, exist_ok=True)
    allowed = re.compile(r'(?:scripts/(?:bootstrap\\.sh|runtime\\.py|download_models\\.py|prepare_models\\.py|render_workflow\\.py)|(?:models|workflows)/[^/]+\\.json)')
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        names = [member.filename for member in members]
        if len(names) != len(set(names)) or len(names) > 200:
            raise RuntimeError('Deployment archive has duplicate or excessive entries.')
        if sum(member.file_size for member in members) > 10 * 1024 * 1024:
            raise RuntimeError('Deployment archive exceeds the code-only size limit.')
        for member in members:
            if not allowed.fullmatch(member.filename) or stat.S_ISLNK(member.external_attr >> 16):
                raise RuntimeError('Deployment archive contains an unsupported entry.')
            target = destination / member.filename
            if target.parent.is_symlink() or target.is_symlink():
                raise RuntimeError('Deployment files must not be symlinks.')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.resolve().relative_to(destination.resolve())
        for member in members:
            target = destination / member.filename
            target.write_bytes(bundle.read(member))
            os.chmod(target, 0o700 if member.filename.endswith('.sh') else 0o600)
    return {'ok': True, 'status': 'deployed', 'files': len(members)}

def install(payload):
    state = Path('/content/colab-comfyui-runtime')
    logs = state / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(state / 'install-launch.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'ok': True, 'status': 'installing', 'already_running': True, 'installed': False}
        with (state / 'install.lock').open('a') as bootstrap_lock:
            try:
                fcntl.flock(bootstrap_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'ok': True, 'status': 'installing', 'already_running': True, 'installed': False}
        script = Path('/content/colab-comfyui-launcher/scripts/bootstrap.sh')
        if not script.is_file():
            raise RuntimeError('Deploy the launcher before installing dependencies.')
        with (logs / 'install.log').open('a', encoding='utf-8') as log:
            process = subprocess.Popen(
                ['timeout', '--signal=TERM', '--kill-after=15', str(payload['install_timeout']),
                 'bash', str(script), 'install'],
                cwd='/content/colab-comfyui-launcher',
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, pass_fds=(lock_fd,),
            )
        atomic_json(state / 'install-launch.json', {'pid': process.pid})
        return {'ok': True, 'status': 'started', 'pid': process.pid,
                'installed': False, 'deadline_seconds': payload['install_timeout'],
                'next': 'Use status to check install.json for ready or failed.'}
    finally:
        os.close(lock_fd)

def runtime_result(payload, timeout=50):
    script = '/content/colab-comfyui-launcher/scripts/runtime.py'
    if not Path(script).is_file():
        services_record = Path('/content/colab-comfyui-runtime/services.json')
        if payload['action'] == 'status':
            unknown_services = services_record.exists()
            unknown_installation = unknown_services or Path('/content/colab-comfyui-runtime/install.json').exists()
            return {
                'ok': True, 'deployed': False,
                'deployment': {'status': 'not_deployed'},
                'installation': {'status': 'unknown' if unknown_installation else 'not_started',
                                 'phase': 'deployment_missing'},
                'comfyui_alive': None if unknown_services else False,
                'tunnel_alive': None if unknown_services else False,
                'http_ready': None if unknown_services else False,
                'models_ready': False, 'url': None,
                'runtime': {'python': sys.version.split()[0]},
                'drive': {'mounted': os.path.ismount('/content/drive'), 'path': '/content/drive'},
                'next': 'Session exists; deploy the launcher before installation or service actions.',
            }
        if payload['action'] == 'stop' and not services_record.exists():
            return {'ok': True, 'status': 'not_deployed', 'deployed': False,
                    'runtime_still_running': True, 'cleanup_skipped': True}
        if payload['action'] == 'stop':
            raise RuntimeError('Existing service state cannot be cleaned without its runtime script; deploy the launcher first.')
        raise RuntimeError('Deploy the launcher before this service action.')
    executable = '/content/colab-comfyui-runtime/.venv/bin/python'
    if payload['action'] in ('status', 'stop') and not Path(executable).is_file():
        executable = sys.executable
    arguments = [executable, script, payload['action']]
    if payload.get('request_id'):
        arguments.extend(['--request-id', payload['request_id']])
    if payload['action'] == 'start':
        if payload['ephemeral']:
            arguments.append('--ephemeral')
        else:
            arguments.extend(['--storage-root', payload['storage_root']])
        if payload.get('local_only', False):
            arguments.append('--local-only')
        elif payload.get('public', False):
            arguments.append('--public')
        else:
            arguments.extend(['--allowed-email', payload['allowed_email']])
        if payload['cpu']:
            arguments.append('--cpu')
    try:
        completed = subprocess.run(
            arguments, cwd='/content/colab-comfyui-launcher',
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError('Remote service action timed out; inspect status before retrying.')
    try:
        result = json.loads(completed.stdout.strip())
    except (ValueError, TypeError):
        raise RuntimeError('Runtime returned invalid JSON. ' + redact(completed.stderr))
    if not isinstance(result, dict) or not isinstance(result.get('ok'), bool):
        raise RuntimeError('Runtime JSON must contain a boolean ok field.')
    if completed.returncode != 0 or result.get('ok') is not True:
        raise RuntimeError(redact(str(result.get('error', 'Runtime action failed.'))
                                  + ': ' + str(result.get('message', ''))))
    result['deployed'] = True
    result['deployment'] = {'status': 'deployed'}
    return result

def process_stamp(pid):
    try:
        fields = Path('/proc/' + str(pid) + '/stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] if fields[0] != 'Z' else None
    except (OSError, IndexError):
        return None

def read_startup():
    path = Path('/content/colab-comfyui-runtime/start-result.json')
    result = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
    if isinstance(result, dict):
        result['progress'] = read_progress('startup-progress.json')
    return result

def read_progress(filename):
    path = Path('/content/colab-comfyui-runtime') / filename
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > 65536:
            return {'status': 'unreadable', 'error': 'Progress record exceeds size limit.'}
        value = json.loads(path.read_text(encoding='utf-8'))
        return safe_result(value) if isinstance(value, dict) else {'status': 'unreadable'}
    except (OSError, ValueError):
        return {'status': 'unreadable'}

def safe_result(value):
    if isinstance(value, dict):
        return {key: safe_result(item) for key, item in value.items()}
    if isinstance(value, list):
        return [safe_result(item) for item in value]
    return redact(value) if isinstance(value, str) else value

def background_paths(action):
    name = {'download': 'model-download', 'prepare': 'model-prepare', 'render': 'render'}[action]
    state = Path('/content/colab-comfyui-runtime')
    return state, state / (name + '-process.json'), state / (name + '-result.json'), name

def background_status(action):
    state, process_path, result_path, name = background_paths(action)
    process = json.loads(process_path.read_text()) if process_path.exists() else {}
    pid, stamp = process.get('pid'), process.get('stamp')
    running = isinstance(pid, int) and pid > 1 and stamp is not None and process_stamp(pid) == stamp
    result = json.loads(result_path.read_text()) if result_path.exists() else None
    status = 'running' if running else (result.get('status', 'unknown') if isinstance(result, dict) else 'not_started')
    if not running and status == 'starting':
        status = 'interrupted'
        result = {'ok': False, 'status': status, 'error': 'Controller exited without a verified final result.'}
    progress_name = {'download': 'model-progress.json', 'prepare': 'prepare-progress.json', 'render': 'render-progress.json'}[action]
    return {'running': running, 'status': status, 'result': safe_result(result), 'progress': read_progress(progress_name)}

def background_arguments(payload):
    action = payload['action']
    state, process_path, result_path, name = background_paths(action)
    source = Path('/content/colab-comfyui-launcher/scripts')
    executable = sys.executable if action in ('download', 'prepare') else '/content/colab-comfyui-runtime/.venv/bin/python'
    script = source / {'download': 'download_models.py', 'prepare': 'prepare_models.py', 'render': 'render_workflow.py'}[action]
    if not script.is_file() or not Path(executable).is_file():
        raise RuntimeError('Deploy and install required scripts before starting this task.')
    arguments = [str(executable), str(script), '--max-seconds', str(payload['max_seconds'])]
    if action == 'download':
        if payload.get('models_root'):
            arguments.extend(['--models-root', payload['models_root']])
        if payload.get('ephemeral'):
            arguments.append('--ephemeral')
        if payload.get('verify_cache'):
            arguments.append('--verify-cache')
    elif action == 'prepare':
        if payload.get('cache_root'):
            arguments.extend(['--cache-root', payload['cache_root']])
        if payload.get('verify_cache'):
            arguments.append('--verify-cache')
        if payload.get('download_missing'):
            arguments.append('--download-missing')
    else:
        arguments.extend(['--workflow', '/content/colab-comfyui-launcher/workflows/h3-api.json',
                          '--result-file', str(state / 'render-progress.json')])
    return arguments

def run_background(payload):
    state, process_path, result_path, name = background_paths(payload['action'])
    logs = state / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(state / (name + '-launch.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'ok': True, 'status': 'running', 'already_running': True,
                    'task': payload['action']}
        background_arguments(payload)
        atomic_json(result_path, {'ok': True, 'status': 'starting', 'deadline_seconds': payload['max_seconds']})
        progress_name = {'download': 'model-progress.json', 'prepare': 'prepare-progress.json', 'render': 'render-progress.json'}[payload['action']]
        atomic_json(state / progress_name, {'status': 'starting', 'phase': 'queued', 'files': []})
        with (logs / (name + '.log')).open('a', encoding='utf-8') as log:
            process = subprocess.Popen(
                [sys.executable, '-c', BACKGROUND_SUPERVISOR, json.dumps(payload)],
                cwd='/content/colab-comfyui-launcher', stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                pass_fds=(lock_fd,),
            )
        atomic_json(process_path, {'pid': process.pid, 'stamp': process_stamp(process.pid)})
        return {'ok': True, 'status': 'started', 'task': payload['action'],
                'deadline_seconds': payload['max_seconds'],
                'next': 'Use status to verify this task result.'}
    finally:
        os.close(lock_fd)

def stop_background(action):
    state, process_path, result_path, name = background_paths(action)
    if not process_path.exists():
        return
    process = json.loads(process_path.read_text())
    pid, stamp = process.get('pid'), process.get('stamp')
    if not isinstance(pid, int) or pid <= 1 or stamp is None or process_stamp(pid) != stamp:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while process_stamp(pid) == stamp and time.monotonic() < deadline:
            time.sleep(0.1)
        if process_stamp(pid) == stamp:
            os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    atomic_json(result_path, {'ok': False, 'status': 'stopped', 'error': 'Owned task stopped by caller.'})

def start_service(payload):
    state = Path('/content/colab-comfyui-runtime')
    logs = state / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(state / 'start-launch.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'ok': True, 'status': 'starting', 'already_running': True}
        if not (state / '.venv/bin/python').is_file():
            raise RuntimeError('Installation must be ready before starting services.')
        payload = dict(payload, request_id=uuid.uuid4().hex)
        atomic_json(state / 'start-result.json', {'ok': True, 'status': 'starting', 'request_id': payload['request_id']})
        atomic_json(state / 'startup-progress.json', {'status': 'starting', 'phase': 'queued', 'updated_at': time.time()})
        with (logs / 'start.log').open('a', encoding='utf-8') as log:
            process = subprocess.Popen(
                [sys.executable, '-c', START_SUPERVISOR, json.dumps(payload)],
                cwd='/content/colab-comfyui-launcher',
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, pass_fds=(lock_fd,),
            )
        atomic_json(state / 'start-process.json',
                    {'pid': process.pid, 'stamp': process_stamp(process.pid)})
        return {'ok': True, 'status': 'starting', 'pid': process.pid,
                'next': 'Use status to verify startup completion and HTTP readiness.'}
    finally:
        os.close(lock_fd)

def stop_startup():
    path = Path('/content/colab-comfyui-runtime/start-process.json')
    if not path.exists():
        return
    process = json.loads(path.read_text(encoding='utf-8'))
    pid, stamp = process.get('pid'), process.get('stamp')
    if not isinstance(pid, int) or pid <= 1 or stamp is None or process_stamp(pid) != stamp:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while process_stamp(pid) == stamp and time.monotonic() < deadline:
            time.sleep(0.1)
        if process_stamp(pid) == stamp:
            os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

def runtime_action(payload):
    if payload['action'] == 'start':
        return start_service(payload)
    if payload['action'] == 'stop':
        stop_startup()
        stop_background('download')
        stop_background('prepare')
        stop_background('render')
    result = runtime_result(payload)
    if payload['action'] == 'status':
        result['startup'] = read_startup()
        result['model_download'] = background_status('download')
        result['model_prepare'] = background_status('prepare')
        result['render'] = background_status('render')
    elif payload['action'] == 'stop' and result.get('deployed') is not False:
        atomic_json(Path('/content/colab-comfyui-runtime/start-result.json'),
                    {'ok': True, 'status': 'stopped'})
    return result
"""


def remote_program(payload: dict[str, Any]) -> str:
    """Build Python source; all varying values enter through a JSON literal."""
    serialized = json.dumps(payload, ensure_ascii=True)
    supervisor = (
        REMOTE_HELPERS
        + """\
payload = json.loads(sys.argv[1])
try:
    result = runtime_result(payload, timeout=210)
except Exception as error:
    result = {'ok': False, 'status': 'failed', 'error': redact(str(error))}
    try:
        # Refusing a duplicate start must preserve services from an older request.
        runtime_result({'action': 'stop', 'request_id': payload['request_id']}, timeout=20)
    except Exception:
        pass
atomic_json(Path('/content/colab-comfyui-runtime/start-result.json'), result)
"""
    )
    background_supervisor = (
        REMOTE_HELPERS
        + """\
payload = json.loads(sys.argv[1])
state, process_path, result_path, name = background_paths(payload['action'])
try:
    completed = subprocess.run(
        background_arguments(payload), cwd='/content/colab-comfyui-launcher',
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=payload['max_seconds'] + 15, check=False,
    )
    try:
        result = json.loads(completed.stdout.strip())
    except (ValueError, TypeError):
        raise RuntimeError('Task did not return a single valid JSON result.')
    if not isinstance(result, dict) or not isinstance(result.get('ok'), bool):
        raise RuntimeError('Task result must contain a boolean ok field.')
    if completed.returncode != 0 and result['ok'] is True:
        raise RuntimeError('Task exited unsuccessfully despite its success result.')
    result = safe_result(result)
    result['status'] = 'succeeded' if result['ok'] is True else 'failed'
except Exception as error:
    result = {'ok': False, 'status': 'failed', 'error': redact(str(error))}
    if payload['action'] == 'render':
        result['job_may_still_be_running'] = True
atomic_json(result_path, result)
"""
    )
    return (
        REMOTE_HELPERS
        + f"""\
START_SUPERVISOR = {supervisor!r}
BACKGROUND_SUPERVISOR = {background_supervisor!r}
payload = json.loads({serialized!r})
try:
    if payload['action'] == 'deploy':
        result = deploy(payload)
    elif payload['action'] == 'install':
        result = install(payload)
    elif payload['action'] in ('download', 'prepare', 'render'):
        result = run_background(payload)
    else:
        result = runtime_action(payload)
except Exception as error:
    result = {{'ok': False, 'error': redact(str(error))}}
print({RESULT_PREFIX!r} + json.dumps(result, ensure_ascii=True))
"""
    )


def execute_remote(
    session: str, payload: dict[str, Any], timeout: float
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="colab-launcher-exec-") as temporary:
        path = Path(temporary) / "remote_operation.py"
        path.write_text(remote_program(payload), encoding="utf-8")
        path.chmod(0o600)
        completed = _run_colab(
            [
                "exec",
                "-s",
                session,
                "-f",
                str(path),
                "--timeout",
                str(REMOTE_EXEC_TIMEOUT),
            ],
            timeout,
        )
    try:
        result = parse_result(completed.stdout)
    except BridgeError as exc:
        detail = _failure_summary(completed)
        raise BridgeError(f"{exc}\n{detail}" if detail else str(exc)) from exc
    if completed.returncode != 0:
        raise BridgeError(
            "Colab command exited unsuccessfully. " + _failure_summary(completed)
        )
    return result


def build_archive(destination: Path) -> str:
    files = [
        PROJECT_ROOT / "scripts/bootstrap.sh",
        PROJECT_ROOT / "scripts/runtime.py",
        PROJECT_ROOT / "scripts/download_models.py",
        PROJECT_ROOT / "scripts/prepare_models.py",
        PROJECT_ROOT / "scripts/render_workflow.py",
    ]
    files.extend(sorted((PROJECT_ROOT / "models").glob("*.json")))
    files.extend(sorted((PROJECT_ROOT / "workflows").glob("*.json")))
    root = PROJECT_ROOT.resolve()
    if (
        len(files) > 200
        or sum(path.stat().st_size for path in files if path.is_file())
        > 10 * 1024 * 1024
    ):
        raise BridgeError("Deployment sources exceed the code-only archive limit.")
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            if (
                not path.is_file()
                or path.is_symlink()
                or not path.resolve().is_relative_to(root)
            ):
                raise BridgeError(
                    "Deployment source contains a missing file or unsupported symlink."
                )
            if path.stat().st_size > 10 * 1024 * 1024:
                raise BridgeError(
                    "Deployment sources must contain only small code/configuration files."
                )
            archive.write(path, path.relative_to(PROJECT_ROOT).as_posix())
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def deploy(session: str, timeout: float) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="colab-launcher-deploy-") as temporary:
        archive = Path(temporary) / "colab-comfyui-launcher.zip"
        checksum = build_archive(archive)
        completed = _run_colab(
            [
                "upload",
                "-s",
                session,
                str(archive),
                "content/colab-comfyui-launcher.zip",
            ],
            timeout,
        )
        if completed.returncode != 0:
            raise BridgeError(
                "Deployment upload failed. " + _failure_summary(completed)
            )
        return execute_remote(
            session, {"action": "deploy", "sha256": checksum}, timeout
        )


def positive_seconds(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a positive number") from exc
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("timeout must be a positive finite number")
    return number


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument(
        "-s", "--session", required=True, help="Existing Colab session name"
    )
    root.add_argument(
        "--timeout",
        type=positive_seconds,
        default=120.0,
        help="Local timeout for each short official CLI invocation (default: 120s)",
    )
    actions = root.add_subparsers(dest="action", required=True)
    actions.add_parser(
        "deploy", help="Upload only launcher scripts and JSON configurations"
    )
    installer = actions.add_parser(
        "install", help="Start background installation; inspect status later"
    )
    installer.add_argument(
        "--install-timeout",
        type=positive_seconds,
        default=900.0,
        help="Remote background installation deadline (default: 900s)",
    )
    start = actions.add_parser("start", help="Start services in the existing runtime")
    storage = start.add_mutually_exclusive_group()
    storage.add_argument(
        "--storage-root", default="/content/drive/MyDrive/colab-comfyui"
    )
    storage.add_argument(
        "--ephemeral", action="store_true", help="Explicitly use runtime-local storage"
    )
    access = start.add_mutually_exclusive_group(required=True)
    access.add_argument("--allowed-email", help="Email permitted by the access gate")
    access.add_argument(
        "--public",
        action="store_true",
        help="Explicitly expose this temporary UI publicly",
    )
    access.add_argument(
        "--local-only",
        action="store_true",
        help="Run ComfyUI for SSH forwarding without Cloudflare",
    )
    start.add_argument("--cpu", action="store_true", help="Explicit CPU mode")
    download = actions.add_parser(
        "download", help="Start bounded background downloads of pinned H3 models"
    )
    download.add_argument("--max-seconds", type=positive_seconds, default=1800.0)
    download.add_argument("--models-root", help="Absolute models directory on runtime")
    download.add_argument("--ephemeral", action="store_true")
    download.add_argument(
        "--verify-cache",
        action="store_true",
        help="Rehash existing Drive files instead of using verified metadata receipts",
    )
    prepare = actions.add_parser(
        "prepare", help="Copy Drive models to VM disk and verify them during the copy"
    )
    prepare.add_argument("--max-seconds", type=positive_seconds, default=1800.0)
    prepare.add_argument("--cache-root", help="Absolute Drive models cache directory")
    prepare.add_argument(
        "--verify-cache",
        action="store_true",
        help="Also rehash the Drive source before copying",
    )
    prepare.add_argument(
        "--download-missing",
        action="store_true",
        help="Download missing pinned files into Drive before copying; existing cache is verified during copy",
    )
    render = actions.add_parser(
        "render", help="Start bounded execution of the deployed H3 API workflow"
    )
    render.add_argument("--max-seconds", type=positive_seconds, default=900.0)
    for action in ("status", "smoke", "stop"):
        actions.add_parser(
            action,
            help=(
                "Stop launcher services, retaining the Colab session"
                if action == "stop"
                else f"Run remote {action}"
            ),
        )
    return root


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    payload: dict[str, Any] = {"action": arguments.action}
    if arguments.action == "install":
        payload["install_timeout"] = arguments.install_timeout
    elif arguments.action in ("download", "prepare", "render"):
        payload["max_seconds"] = arguments.max_seconds
        if arguments.action == "download":
            if arguments.models_root and not arguments.models_root.startswith("/"):
                print("Models root must be an absolute runtime path.", file=sys.stderr)
                return 2
            payload.update(
                models_root=arguments.models_root,
                ephemeral=arguments.ephemeral,
                verify_cache=arguments.verify_cache,
            )
        elif arguments.action == "prepare":
            if arguments.cache_root and not arguments.cache_root.startswith("/"):
                print("Cache root must be an absolute runtime path.", file=sys.stderr)
                return 2
            payload.update(
                cache_root=arguments.cache_root,
                verify_cache=arguments.verify_cache,
                download_missing=arguments.download_missing,
            )
    elif arguments.action == "start":
        if arguments.allowed_email and not re.fullmatch(
            r"[^\s@]+@[^\s@]+\.[^\s@]+", arguments.allowed_email
        ):
            print("Allowed email must be a valid email address.", file=sys.stderr)
            return 2
        if not arguments.ephemeral and not arguments.storage_root.startswith("/"):
            print("Storage root must be an absolute runtime path.", file=sys.stderr)
            return 2
        payload.update(
            storage_root=arguments.storage_root,
            ephemeral=arguments.ephemeral,
            allowed_email=arguments.allowed_email,
            public=arguments.public,
            local_only=arguments.local_only,
            cpu=arguments.cpu,
        )
    try:
        result = (
            deploy(arguments.session, arguments.timeout)
            if arguments.action == "deploy"
            else execute_remote(arguments.session, payload, arguments.timeout)
        )
    except (BridgeError, OSError, ValueError, zipfile.BadZipFile) as exc:
        print(sanitize_failure(str(exc)), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
