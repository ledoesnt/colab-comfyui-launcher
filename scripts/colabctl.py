#!/usr/bin/env python3
"""Deploy and control launcher services through the official Colab CLI.

Runtime creation with an explicit CU budget and verified release use the
official Colab CLI. Drive authorization remains interactive in the TUI.
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
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REMOTE_ROOT = "/content/colab-comfyui-launcher"
REMOTE_STATE = "/content/colab-comfyui-runtime"
RESULT_PREFIX = "LAUNCHER_RESULT="
REMOTE_EXEC_TIMEOUT = 60
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from client_requests import ClientRequests, request_id


class BridgeError(Exception):
    """An operation failed before its result was verified."""

    def __init__(self, message: str, result: dict | None = None):
        super().__init__(message)
        self.result = result


def sanitize_failure(value: str) -> str:
    """Keep useful diagnostics while removing URLs and common secret forms."""
    value = ANSI.sub("", value)
    if "ReadTimeout" in value:
        return (
            "The Colab provider request timed out; its result is unknown. "
            "Inspect the existing runtime before retrying an operation."
        )
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
        completed = subprocess.run(
            [executable, "--auth=oauth2", *arguments],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if "[colab] A new version of Colab CLI is available:" in completed.stdout:
            print("Colab CLI reported an available upgrade.", file=sys.stderr)
        return completed
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
        raise BridgeError(sanitize_failure(str(message)), result=result)
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
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
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
    allowed = re.compile(r'(?:scripts/(?:bootstrap\\.sh|runtime\\.py|download_models\\.py|prepare_models\\.py|render_workflow\\.py|assets\\.py)|(?:models|workflows)/[^/]+\\.json)')
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
        command = ['timeout', '--signal=TERM', '--kill-after=15', str(payload['install_timeout']),
                   'bash', str(script), 'install']
        if payload.get('request_id'):
            command = [sys.executable, '-c', INSTALL_SUPERVISOR, json.dumps(payload)]
        with (logs / 'install.log').open('a', encoding='utf-8') as log:
            process = subprocess.Popen(
                command,
                cwd='/content/colab-comfyui-launcher',
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, pass_fds=(lock_fd,),
            )
        atomic_json(state / 'install-launch.json', {'pid': process.pid, 'request_id': payload.get('request_id')})
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
            drive_mounted = os.path.ismount('/content/drive')
            mydrive = Path('/content/drive/MyDrive')
            return {
                'ok': True, 'deployed': False,
                'deployment': {'status': 'not_deployed'},
                'installation': {'status': 'unknown' if unknown_installation else 'not_started',
                                 'phase': 'deployment_missing'},
                'comfyui_alive': None if unknown_services else False,
                'tunnel_alive': None if unknown_services else False,
                'http_ready': None if unknown_services else False,
                'models_ready': False, 'url': None,
                'configuration': None,
                'runtime': {'python': sys.version.split()[0]},
                'drive': {'mounted': drive_mounted,
                          'mydrive_ready': bool(drive_mounted and mydrive.is_dir() and not mydrive.is_symlink()),
                          'path': '/content/drive'},
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
    seconds = payload['max_seconds']
    if payload.get('not_after'):
        seconds = min(seconds, payload['not_after'] - time.time())
        if seconds <= 0:
            raise RuntimeError('Work protection deadline passed before background task startup.')
    arguments = [str(executable), str(script), '--max-seconds', str(seconds)]
    if action in ('download', 'prepare'):
        workers = payload.get('workers', 2)
        if type(workers) is not int or not 1 <= workers <= 4:
            raise RuntimeError('Download workers must be an integer from 1 to 4.')
        arguments.extend(['--workers', str(workers)])
    if action == 'download':
        if payload.get('models_root'):
            arguments.extend(['--models-root', payload['models_root']])
        if payload.get('ephemeral'):
            arguments.append('--ephemeral')
        if payload.get('verify_cache'):
            arguments.append('--verify-cache')
    elif action == 'prepare':
        if payload.get('ephemeral') and payload.get('cache_root'):
            raise RuntimeError('--ephemeral cannot be combined with --cache-root.')
        if payload.get('cache_root'):
            arguments.extend(['--cache-root', payload['cache_root']])
        if payload.get('ephemeral'):
            arguments.append('--ephemeral')
        if payload.get('verify_cache'):
            arguments.append('--verify-cache')
        if payload.get('download_missing'):
            arguments.append('--download-missing')
    else:
        workflow = payload.get('workflow', 'h3-i2v-api.json')
        if payload.get('workflow_data') is not None:
            workflow_path = request_path(payload['request_id']).with_suffix('.workflow.json')
            atomic_json(workflow_path, payload['workflow_data'])
        else:
            if workflow not in ('h3-api.json', 'h3-i2v-api.json', 'h3-first-last-api.json'):
                raise RuntimeError('Choose a deployed launcher workflow or supply custom API JSON.')
            workflow_path = Path('/content/colab-comfyui-launcher/workflows') / workflow
        arguments.extend(['--workflow', str(workflow_path),
                          '--result-file', str(state / 'render-progress.json')])
        if payload.get('request_id'):
            arguments.extend(['--request-id', payload['request_id'], '--job-dir', str(state / 'render-jobs')])
        for name in ('session', 'assignment_id'):
            if payload.get(name):
                arguments.extend(['--' + name.replace('_', '-'), payload[name]])
        if payload.get('mapping_data') is not None:
            mapping_path = request_path(payload['request_id']).with_suffix('.mapping.json')
            atomic_json(mapping_path, payload['mapping_data'])
            arguments.extend(['--mapping', str(mapping_path)])
        for parameter in payload.get('parameters', []):
            arguments.extend(['--param', parameter])
        if payload.get('export_frames'):
            arguments.append('--export-frames')
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
        atomic_json(result_path, {'ok': True, 'status': 'starting', 'deadline_seconds': payload['max_seconds'],
                                 'request_id': payload.get('request_id')})
        progress_name = {'download': 'model-progress.json', 'prepare': 'prepare-progress.json', 'render': 'render-progress.json'}[payload['action']]
        atomic_json(state / progress_name, {'status': 'starting', 'phase': 'queued', 'files': []})
        with (logs / (name + '.log')).open('a', encoding='utf-8') as log:
            process = subprocess.Popen(
                [sys.executable, '-c', BACKGROUND_SUPERVISOR, json.dumps(payload)],
                cwd='/content/colab-comfyui-launcher', stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                pass_fds=(lock_fd,),
            )
        process_record = {'pid': process.pid, 'stamp': process_stamp(process.pid)}
        if payload.get('request_id'):
            process_record['request_id'] = payload['request_id']
        atomic_json(process_path, process_record)
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
    stopped = {'ok': False, 'status': 'stopped', 'error': 'Owned task stopped by caller.'}
    atomic_json(result_path, stopped)
    if process.get('request_id'):
        finish_job(process, stopped)

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
        payload = dict(payload, request_id=payload.get('request_id') or uuid.uuid4().hex)
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
    runtime_payload = dict(payload)
    if payload['action'] == 'stop':
        # User stop has its own journal ID; only failed-start cleanup is scoped
        # to the startup request (and calls runtime_result directly).
        runtime_payload.pop('request_id', None)
    if payload.get('not_after') and payload['action'] not in ('status', 'stop'):
        runtime_timeout = min(50, max(0.1, payload['not_after'] - time.time()))
        result = runtime_result(runtime_payload, timeout=runtime_timeout)
    else:
        result = runtime_result(runtime_payload)
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


REMOTE_HELPERS += r"""

def request_path(identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identifier):
        raise RuntimeError('Invalid request_id.')
    directory = Path('/content/colab-comfyui-runtime/jobs')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory / (identifier + '.json')

def bind_instance(payload):
    if not payload.get('assignment_id'):
        return
    path = Path('/content/colab-comfyui-runtime/instance.json')
    for component in (*reversed(path.parents), path, path.with_suffix('.lock')):
        if component.is_symlink():
            raise RuntimeError('Runtime identity paths must not contain symlinks.')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    expected = {'session': payload['session'], 'assignment_id': payload['assignment_id']}
    descriptor = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as source:
                if json.load(source) != expected:
                    raise RuntimeError('Runtime assignment identity differs from the saved instance marker.')
        else:
            atomic_json(path, expected)

def job_status(identifier):
    path = request_path(identifier)
    if not path.exists():
        return {'ok': False, 'status': 'not_found', 'request_id': identifier, 'job_id': identifier,
                'error': 'No remote request record; an earlier uncertain submission must not be repeated automatically.'}
    record = json.loads(path.read_text())
    result = record.get('result')
    response = dict(result) if isinstance(result, dict) else {
        'ok': False, 'status': 'submission_unknown',
        'error': 'Dispatch was recorded without a verified result. Inspect before any new submission.'}
    response.update(request_id=identifier, job_id=identifier, action=record['action'],
                    session=record.get('session'), assignment_id=record.get('assignment_id'))
    if record['action'] == 'render':
        render_path = Path('/content/colab-comfyui-runtime/render-jobs') / (identifier + '.json')
        if render_path.exists():
            render_record = safe_result(json.loads(render_path.read_text()))
            executable = Path('/content/colab-comfyui-runtime/.venv/bin/python')
            script = Path('/content/colab-comfyui-launcher/scripts/render_workflow.py')
            if executable.is_file() and script.is_file():
                try:
                    completed = subprocess.run(
                        [str(executable), str(script), '--query-only', '--request-id', identifier,
                         '--job-dir', str(render_path.parent), '--max-seconds', '20'],
                        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=25, check=False)
                    queried = json.loads(completed.stdout)
                    if not isinstance(queried.get('result'), dict) or queried.get('request_id') != identifier:
                        raise ValueError('Invalid render query response')
                    render_record = safe_result(queried['result'])
                    response['query_state'] = queried.get('query_state', 'verified')
                    if queried.get('ok') is not True:
                        response['query_error'] = safe_result(queried.get('error', 'Render query unavailable.'))
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    response['query_state'] = 'unavailable'
            if render_record.get('request_id') != identifier or render_record.get('assignment_id') != record.get('assignment_id'):
                raise RuntimeError('Render request identity differs from its launcher job.')
            response.update(render_record=render_record, status=render_record.get('status', response['status']))
            if render_record.get('terminal'):
                response.update(render_record)
    elif record['action'] in ('prepare', 'download') and response.get('status') in ('started', 'starting'):
        state, process_path, result_path, name = background_paths(record['action'])
        process = json.loads(process_path.read_text()) if process_path.exists() else {}
        if process.get('request_id') == identifier and process_stamp(process.get('pid')) != process.get('stamp'):
            response.update(ok=False, status='interrupted', error='Task controller exited without a final receipt; inspect status before a new request.')
    return response

def finish_job(payload, result):
    identifier = payload.get('request_id')
    if not identifier:
        return
    path = request_path(identifier)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not path.exists():
            return
        record = json.loads(path.read_text())
        result = dict(result, request_id=identifier, job_id=identifier,
                      session=record.get('session'), assignment_id=record.get('assignment_id'))
        record.update(result=safe_result(result), updated_at=time.time())
        atomic_json(path, record)

def dispatch(payload):
    bind_instance(payload)
    action = payload['action']
    if payload.get('not_after') and action not in ('status', 'job', 'stop') and time.time() >= payload['not_after']:
        return {'ok': False, 'status': 'budget_expired', 'error': 'Work protection deadline passed before remote dispatch.'}
    if payload.get('not_after') and action not in ('status', 'job', 'stop'):
        payload = dict(payload)
        remaining = payload['not_after'] - time.time()
        for field in ('max_seconds', 'install_timeout', 'work_seconds_remaining'):
            if field in payload:
                payload[field] = min(payload[field], remaining)
    if action == 'job':
        return job_status(payload['request_id'])
    if action.startswith('asset-'):
        import importlib.util
        spec = importlib.util.spec_from_file_location('launcher_asset_transfer', '/content/colab-comfyui-launcher/scripts/assets.py')
        assets = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(assets)
        return assets.remote_action(payload)
    if action == 'deploy':
        return deploy(payload)
    if action == 'install':
        return install(payload)
    if action in ('download', 'prepare', 'render'):
        return run_background(payload)
    return runtime_action(payload)

def dispatch_request(payload):
    identifier = payload.get('request_id')
    if not identifier or payload['action'] in ('status', 'job') or payload['action'].startswith('asset-'):
        return dispatch(payload)
    path = request_path(identifier)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            old = json.loads(path.read_text())
            if old['payload_sha256'] != digest:
                raise RuntimeError('request_id already belongs to different arguments.')
            return job_status(identifier)
        record = {'version': 1, 'request_id': identifier, 'job_id': identifier, 'action': payload['action'],
                  'session': payload.get('session'), 'assignment_id': payload.get('assignment_id'),
                  'payload_sha256': digest, 'created_at': time.time(), 'status': 'dispatching'}
        atomic_json(path, record)
        try:
            result = dispatch(payload)
        except Exception as error:
            result = {'ok': False, 'status': 'submission_unknown', 'error': redact(str(error))}
        result.update(request_id=identifier, job_id=identifier, session=record['session'],
                      assignment_id=record['assignment_id'])
        record.update(result=safe_result(result), updated_at=time.time())
        atomic_json(path, record)
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
    result = runtime_result(payload, timeout=min(210, payload.get('work_seconds_remaining', 210)))
except Exception as error:
    result = {'ok': False, 'status': 'failed', 'error': redact(str(error))}
    try:
        # Refusing a duplicate start must preserve services from an older request.
        runtime_result({'action': 'stop', 'request_id': payload['request_id']}, timeout=20)
    except Exception:
        pass
atomic_json(Path('/content/colab-comfyui-runtime/start-result.json'), result)
finish_job(payload, result)
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
    result['status'] = 'succeeded' if result['ok'] is True else result.get('status', 'failed')
except Exception as error:
    result = {'ok': False, 'status': 'failed', 'error': redact(str(error))}
    if payload['action'] == 'render':
        result['job_may_still_be_running'] = True
atomic_json(result_path, result)
finish_job(payload, result)
"""
    )
    install_supervisor = (
        REMOTE_HELPERS
        + """\
payload = json.loads(sys.argv[1])
try:
    completed = subprocess.run(
        ['timeout', '--signal=TERM', '--kill-after=15', str(payload['install_timeout']),
         'bash', '/content/colab-comfyui-launcher/scripts/bootstrap.sh', 'install'],
        stdin=subprocess.DEVNULL, check=False)
    path = Path('/content/colab-comfyui-runtime/install.json')
    installation = json.loads(path.read_text()) if path.exists() else {}
    success = completed.returncode == 0 and installation.get('status') == 'ready'
    result = {'ok': success, 'status': 'succeeded' if success else 'failed', 'installation': installation}
except Exception as error:
    result = {'ok': False, 'status': 'failed', 'error': redact(str(error))}
finish_job(payload, result)
"""
    )
    return (
        REMOTE_HELPERS
        + f"""\
START_SUPERVISOR = {supervisor!r}
BACKGROUND_SUPERVISOR = {background_supervisor!r}
INSTALL_SUPERVISOR = {install_supervisor!r}
payload = json.loads({serialized!r})
try:
    result = dispatch_request(payload)
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
        raise BridgeError(
            f"{exc}\n{detail}" if detail else str(exc), result=exc.result
        ) from exc
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
        PROJECT_ROOT / "scripts/assets.py",
    ]
    selection_name = "selected-extra-manifests.json"
    files.extend(
        sorted(
            path
            for path in (PROJECT_ROOT / "models").glob("*.json")
            if path.name != selection_name
        )
    )
    files.extend(sorted((PROJECT_ROOT / "workflows").glob("*.json")))
    selected = [
        path.name
        for path in files
        if path.parent == PROJECT_ROOT / "models"
        and (path.name == "extra.json" or path.name.startswith("extra-"))
    ]
    if any(
        not re.fullmatch(r"extra(?:-[A-Za-z0-9._-]+)?\.json", name) for name in selected
    ):
        raise BridgeError(
            "Extra manifest names must use extra.json or extra-NAME.json."
        )
    selection_data = json.dumps({"version": 1, "files": sorted(selected)})
    root = PROJECT_ROOT.resolve()
    if (
        len(files) + 1 > 200
        or sum(path.stat().st_size for path in files if path.is_file())
        + len(selection_data.encode())
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
        archive.writestr(
            "models/" + selection_name,
            selection_data,
        )
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def deploy(session: str, timeout: float, payload: dict | None = None) -> dict[str, Any]:
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
            session,
            {**(payload or {}), "action": "deploy", "sha256": checksum},
            timeout,
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
        "--request-id",
        type=request_id,
        help="Stable caller ID; reuse queries the original request",
    )
    root.add_argument(
        "--json", action="store_true", help="JSON output (also the default)"
    )
    root.add_argument(
        "--timeout",
        type=positive_seconds,
        default=120.0,
        help=(
            "Local call timeout; upload/collect share one total asset-operation "
            "deadline, further limited by CU protection (default: 120s). "
            "Budget allocation uses separate provider timeouts."
        ),
    )
    actions = root.add_subparsers(dest="action", required=True)
    new = actions.add_parser(
        "new",
        help="Allocate one instance with a persistent CU guard",
        description=(
            "allocation_pending or allocation_unknown with exit 1 is nonterminal; "
            "allocation may still be in progress. Save the returned request_id and "
            "budget job_id. Poll 'budget' for the same session and check both IDs; "
            "do not repeat new based on the exit code."
        ),
    )
    hardware = new.add_mutually_exclusive_group(required=True)
    hardware.add_argument("--gpu", choices=("T4", "L4", "G4", "H100", "A100"))
    hardware.add_argument("--cpu", action="store_true")
    adopt = actions.add_parser(
        "adopt", help="Explicitly take budget responsibility for an existing allocation"
    )
    adopt.add_argument("--assignment-id", required=True)
    adopt.add_argument(
        "--allocated-at",
        type=positive_seconds,
        required=True,
        help="Original allocation Unix timestamp, never the reconnection time",
    )
    adopt.add_argument("--confirm", action="store_true", required=True)
    for command in (new, adopt):
        command.add_argument("--cu-limit", type=positive_seconds, required=True)
        command.add_argument(
            "--rate-ceiling",
            type=positive_seconds,
            required=True,
            help="Conservative CU/hour bound; unknown rates cannot start managed work",
        )
        command.add_argument("--reserve-cu", type=positive_seconds)
    budget = actions.add_parser(
        "budget",
        help="Read the persisted single-allocation budget and guard status",
        description=(
            "Read-only local status polling without --refresh. For allocation_pending "
            "or allocation_unknown (nonterminal, exit 1), check request_id and budget "
            "job_id against the original request; do not repeat new. This reads the "
            "current session budget; 'job ID' queries separate task records."
        ),
    )
    budget.add_argument(
        "--refresh",
        action="store_true",
        help="Resume the recorded guard and reconcile its allocation; an expired budget may release the instance",
    )
    hold = actions.add_parser(
        "hold", help="Retain a managed idle VM for a bounded period"
    )
    hold.add_argument("--seconds", type=positive_seconds, required=True)
    actions.add_parser(
        "release",
        help="Release only this allocation and verify absence on the platform",
    )
    job = actions.add_parser(
        "job", help="Query a durable request after any client restart"
    )
    job.add_argument("identifier", type=request_id)
    actions.add_parser(
        "jobs", help="List local durable request receipts for this session"
    )
    upload = actions.add_parser(
        "upload",
        help="Upload an input with instance identity and SHA verification",
        description=(
            "Set global --timeout before upload: one total asset-operation deadline "
            "covers staging, hashing, transfer and verification, not a fresh timeout "
            "per step. CU work_deadline and any hold_deadline can shorten it."
        ),
    )
    upload.add_argument("source", type=Path)
    collect = actions.add_parser(
        "collect",
        help="Collect and verify outputs into an instance/job directory",
        description=(
            "Set global --timeout before collect: all files and verification share "
            "one total asset-operation deadline, not a fresh timeout per file. "
            "CU release_deadline and any hold_deadline can shorten it."
        ),
    )
    manifest = collect.add_mutually_exclusive_group(required=True)
    manifest.add_argument("--manifest", type=Path)
    manifest.add_argument("--job", type=request_id)
    collect.add_argument("--destination", type=Path, required=True)
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
    download.add_argument(
        "--workers",
        type=int,
        choices=range(1, 5),
        default=2,
        help="Parallel HTTPS model downloads (default: 2)",
    )
    download.add_argument("--models-root", help="Absolute models directory on runtime")
    download.add_argument("--ephemeral", action="store_true")
    download.add_argument(
        "--verify-cache",
        action="store_true",
        help="Rehash existing Drive files instead of using verified metadata receipts",
    )
    prepare = actions.add_parser(
        "prepare",
        help="Prepare verified models on VM disk from Drive or direct download",
    )
    prepare.add_argument("--max-seconds", type=positive_seconds, default=1800.0)
    prepare.add_argument(
        "--workers",
        type=int,
        choices=range(1, 5),
        default=2,
        help="Parallel HTTPS model downloads; Drive copying stays sequential",
    )
    prepare.add_argument("--cache-root", help="Absolute Drive models cache directory")
    prepare.add_argument(
        "--ephemeral",
        action="store_true",
        help="Use only runtime/models, without mounting Drive or a second cache copy",
    )
    prepare.add_argument(
        "--verify-cache",
        action="store_true",
        help="Rehash the Drive source, or VM models when --ephemeral is selected",
    )
    prepare.add_argument(
        "--download-missing",
        action="store_true",
        help="Download missing pinned files into Drive cache, or directly to VM with --ephemeral",
    )
    prepare.add_argument(
        "--mode",
        choices=("verify_only", "download_missing"),
        help="Explicit preparation policy; default verify_only",
    )
    render = actions.add_parser(
        "render", help="Start bounded execution of the deployed H3 API workflow"
    )
    render.add_argument("--max-seconds", type=positive_seconds, default=900.0)
    render.add_argument(
        "--workflow",
        choices=("h3-i2v-api.json", "h3-api.json", "h3-first-last-api.json"),
        default="h3-i2v-api.json",
        help="Official I2V template test (default), or the original T2V test",
    )
    render.add_argument(
        "--workflow-file",
        type=Path,
        help="Complete custom ComfyUI API JSON, without requiring a parameter mapping",
    )
    render.add_argument(
        "--mapping", type=Path, help="Optional named-parameter mapping JSON"
    )
    render.add_argument(
        "--param",
        action="append",
        default=[],
        help="Named parameter NAME=JSON, repeatable",
    )
    render.add_argument(
        "--export-frames",
        action="store_true",
        help="Include optional decoded first/last frame assets",
    )
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


def command_payload(arguments) -> dict[str, Any]:
    payload: dict[str, Any] = {"action": arguments.action}
    if arguments.action == "install":
        payload["install_timeout"] = arguments.install_timeout
    elif arguments.action in ("download", "prepare", "render"):
        payload["max_seconds"] = arguments.max_seconds
        if arguments.action in ("download", "prepare"):
            payload["workers"] = arguments.workers
        if arguments.action == "render":
            payload["workflow"] = arguments.workflow
            for option, key in (
                ("workflow_file", "workflow_data"),
                ("mapping", "mapping_data"),
            ):
                source = getattr(arguments, option)
                if source:
                    if source.stat().st_size > 4 * 1024 * 1024:
                        raise ValueError(
                            "Workflow/mapping JSON exceeds the 4 MiB limit."
                        )
                    data = json.loads(source.read_text())
                    if not isinstance(data, dict):
                        raise ValueError("Workflow/mapping must be a JSON object.")
                    payload[key] = data
            payload.update(
                parameters=arguments.param, export_frames=arguments.export_frames
            )
        if arguments.action == "download":
            if arguments.models_root and not arguments.models_root.startswith("/"):
                raise ValueError("Models root must be an absolute runtime path.")
            payload.update(
                models_root=arguments.models_root,
                ephemeral=arguments.ephemeral,
                verify_cache=arguments.verify_cache,
            )
        elif arguments.action == "prepare":
            if arguments.mode == "verify_only" and arguments.download_missing:
                raise ValueError("verify_only conflicts with --download-missing.")
            arguments.download_missing = (
                arguments.download_missing or arguments.mode == "download_missing"
            )
            if arguments.ephemeral and arguments.cache_root:
                raise ValueError("--ephemeral cannot be combined with --cache-root.")
            if arguments.cache_root and not arguments.cache_root.startswith("/"):
                raise ValueError("Cache root must be an absolute runtime path.")
            payload.update(
                cache_root=arguments.cache_root,
                ephemeral=arguments.ephemeral,
                verify_cache=arguments.verify_cache,
                download_missing=arguments.download_missing,
            )
    elif arguments.action == "start":
        if arguments.allowed_email and not re.fullmatch(
            r"[^\s@]+@[^\s@]+\.[^\s@]+", arguments.allowed_email
        ):
            raise ValueError("Allowed email must be a valid email address.")
        if not arguments.ephemeral and not arguments.storage_root.startswith("/"):
            raise ValueError("Storage root must be an absolute runtime path.")
        payload.update(
            storage_root=arguments.storage_root,
            ephemeral=arguments.ephemeral,
            allowed_email=arguments.allowed_email,
            public=arguments.public,
            local_only=arguments.local_only,
            cpu=arguments.cpu,
        )
    return payload


def budget_manager():
    from budget_guard import BudgetManager

    return BudgetManager()


def provider():
    from colab_provider import ColabProvider

    return ColabProvider()


def assignment(session: str) -> dict:
    api = provider()
    scope = api.account_scope()
    matches = [row for row in api.sessions() if row["name"] == session]
    if scope == "unknown" or api.account_scope() != scope:
        raise BridgeError(
            "Authentication scope changed during allocation verification."
        )
    if len(matches) != 1:
        raise BridgeError(
            "The selected session has no unique verified platform allocation."
        )
    return dict(matches[0], account_scope=scope)


def safe_structure(value):
    if isinstance(value, dict):
        return {key: safe_structure(item) for key, item in value.items()}
    if isinstance(value, list):
        return [safe_structure(item) for item in value]
    return sanitize_failure(value) if isinstance(value, str) else value


def query_job(
    session: str, identifier: str, timeout: float, *, locked: bool = False
) -> dict:
    receipts = ClientRequests(session)
    if not locked:
        with receipts.lock(identifier):
            return query_job(session, identifier, timeout, locked=True)
    local = receipts.read(identifier)
    cached = local.get("result") or {}
    if local.get("action") == "release" and cached:
        return dict(cached, source="local_receipt")
    settled = cached.get("terminal") is True or (
        cached.get("status")
        in ("succeeded", "failed", "released", "completed", "not_submitted")
        and cached.get("terminal") is not False
        and not cached.get("job_may_still_be_running")
    )
    if settled:
        return dict(cached, source="local_receipt")
    current = assignment(session)
    if local and local.get("assignment_id") != current["assignment_id"]:
        raise BridgeError(
            "Original allocation is unavailable; refusing to query a replacement instance."
        )
    result = execute_remote(
        session,
        {
            "action": "job",
            "request_id": identifier,
            "assignment_id": current["assignment_id"],
            "session": session,
        },
        timeout,
    )
    if local:
        receipts.finish(identifier, safe_structure(result))
    return result


def submit_command(arguments, payload: dict) -> dict:
    identifier = arguments.request_id or uuid.uuid4().hex
    current = assignment(arguments.session)
    payload = dict(
        payload,
        request_id=identifier,
        session=arguments.session,
        assignment_id=current["assignment_id"],
    )
    receipts = ClientRequests(arguments.session)
    with receipts.lock(identifier):
        _, fresh = receipts.begin(identifier, payload)
        dispatched = not fresh
        try:
            if not fresh:
                result = query_job(
                    arguments.session, identifier, arguments.timeout, locked=True
                )
            else:
                effective = dict(payload)
                action = payload["action"]
                seconds = payload.get("max_seconds", payload.get("install_timeout"))
                allowed = budget_manager().authorize(arguments.session, action, seconds)
                if allowed is not None:
                    effective["work_seconds_remaining"] = allowed
                    effective["not_after"] = time.time() + allowed
                    if "max_seconds" in effective:
                        effective["max_seconds"] = min(
                            effective["max_seconds"], allowed
                        )
                    if "install_timeout" in effective:
                        effective["install_timeout"] = min(
                            effective["install_timeout"], allowed
                        )
                dispatched = True
                result = (
                    deploy(arguments.session, arguments.timeout, effective)
                    if action == "deploy"
                    else execute_remote(arguments.session, effective, arguments.timeout)
                )
            result = dict(
                result,
                request_id=identifier,
                job_id=identifier,
                session=arguments.session,
                assignment_id=current["assignment_id"],
            )
            receipts.finish(identifier, safe_structure(result))
            return result
        except Exception as exc:
            result = dict(
                getattr(exc, "result", None) or {},
                ok=False,
                request_id=identifier,
                job_id=identifier,
                session=arguments.session,
                assignment_id=current["assignment_id"],
            )
            result.setdefault("status", "submission_unknown")
            if not dispatched:
                result.update(
                    status="not_submitted",
                    terminal=True,
                    job_may_still_be_running=False,
                )
            result.setdefault("error", sanitize_failure(str(exc)))
            receipts.finish(identifier, safe_structure(result))
            raise BridgeError(result["error"], result=result) from exc


def release_unmanaged(arguments) -> dict:
    """Persist explicit release identity so a timeout can be retried safely."""
    receipts = ClientRequests(arguments.session)
    identifier = arguments.request_id
    previous = (
        receipts.read(identifier)
        if identifier
        else next(
            (
                row
                for row in receipts.list()
                if row["action"] == "release" and row.get("status") != "released"
            ),
            {},
        )
    )
    if previous:
        if previous["action"] != "release":
            raise ValueError("request_id belongs to a different action.")
        identifier = previous["request_id"]
        if previous.get("status") == "released":
            return dict(previous["result"], source="local_receipt")
        payload = {
            "action": "release",
            "assignment_id": previous["assignment_id"],
            "account_scope": previous["account_scope"],
        }
    else:
        current = assignment(arguments.session)
        identifier = identifier or uuid.uuid4().hex
        payload = {
            "action": "release",
            "assignment_id": current["assignment_id"],
            "account_scope": current["account_scope"],
        }
    with receipts.lock(identifier):
        receipts.begin(identifier, payload)
        result = provider().verified_release(
            arguments.session,
            payload["assignment_id"],
            expected_scope=payload["account_scope"],
        )
        result.update(request_id=identifier, job_id=identifier)
        receipts.finish(identifier, safe_structure(result))
        return result


def run_command(arguments, payload: dict) -> dict:
    action, session = arguments.action, arguments.session
    if (
        not session
        or session.startswith("-")
        or any(c.isspace() or ord(c) < 32 for c in session)
    ):
        raise ValueError("Choose a valid session name.")
    if action == "new":
        result = budget_manager().create(
            session,
            gpu=arguments.gpu,
            limit_cu=arguments.cu_limit,
            rate_ceiling=arguments.rate_ceiling,
            reserve_cu=arguments.reserve_cu,
            request_id=arguments.request_id,
        )
        if result.get("status") in ("active", "holding", "collect_only"):
            current = assignment(session)
            if current["assignment_id"] != result.get("assignment_id"):
                raise BridgeError(
                    "Allocation changed after creation; inspect the recorded budget before continuing."
                )
            result["provider"] = {
                key: value for key, value in current.items() if key != "account_scope"
            }
        return result
    if action == "adopt":
        return budget_manager().adopt(
            session,
            arguments.assignment_id,
            limit_cu=arguments.cu_limit,
            rate_ceiling=arguments.rate_ceiling,
            reserve_cu=arguments.reserve_cu,
            allocated_at=arguments.allocated_at,
            confirm=arguments.confirm,
            request_id=arguments.request_id,
        )
    if action == "budget":
        return budget_manager().status(session, refresh=arguments.refresh)
    if action == "hold":
        return budget_manager().hold(session, arguments.seconds)
    if action == "release":
        managed = budget_manager().status(session)
        if managed.get("managed"):
            return budget_manager().release(session)
        return release_unmanaged(arguments)
    if action == "jobs":
        return {
            "ok": True,
            "session": session,
            "requests": ClientRequests(session).list(),
        }
    if action == "job":
        return query_job(session, arguments.identifier, arguments.timeout)
    if action in ("upload", "collect"):
        from assets import AssetsClient

        current = assignment(session)
        manifest = None
        if action == "collect":
            manifest = (
                json.loads(arguments.manifest.read_text())
                if arguments.manifest
                else query_job(session, arguments.job, arguments.timeout)
            )
            for key in ("result", "render_record"):
                if "assets" not in manifest and isinstance(manifest.get(key), dict):
                    nested = manifest[key]
                    if nested.get("status") == "succeeded":
                        manifest = {**manifest, **nested}
        allowed = budget_manager().authorize(session, action, arguments.timeout)
        seconds = (
            min(arguments.timeout, allowed)
            if allowed is not None
            else arguments.timeout
        )
        cutoff = time.monotonic() + seconds

        def remaining():
            value = cutoff - time.monotonic()
            if value <= 0:
                raise BridgeError("Asset transfer protection deadline reached.")
            return value

        def checked_identity(selected):
            nonlocal cutoff
            allowance = budget_manager().authorize(selected, action, remaining())
            if allowance is not None:
                cutoff = min(cutoff, time.monotonic() + allowance)
            return assignment(selected)["assignment_id"]

        def transfer(argv, timeout):
            return _run_colab(argv, min(timeout, remaining()))

        def remote(selected, operation, timeout):
            operation = dict(operation, not_after=time.time() + remaining())
            return execute_remote(selected, operation, min(timeout, remaining()))

        client = AssetsClient(
            session,
            current["assignment_id"],
            transfer,
            remote,
            checked_identity,
            timeout=seconds,
        )
        if action == "upload":
            return client.upload(arguments.source)
        return client.collect(manifest, arguments.destination, job_id=arguments.job)
    if action == "status":
        result = execute_remote(session, payload, arguments.timeout)
        result["budget"] = budget_manager().status(session)
        result["recent_requests"] = [
            {
                key: row.get(key)
                for key in ("request_id", "job_id", "action", "status", "assignment_id")
            }
            for row in ClientRequests(session).list()[:20]
        ]
        return result
    return submit_command(arguments, payload)


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = parser().parse_args(argv)
    except SystemExit as exc:
        if exc.code:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "status": "invalid_arguments",
                        "error": "Invalid arguments; see stderr.",
                    }
                )
            )
        return int(exc.code or 0)
    try:
        result = run_command(arguments, command_payload(arguments))
    except Exception as exc:  # noqa: BLE001 - always emit structured errors at the CLI boundary.
        result = dict(getattr(exc, "result", None) or {})
        result.update(ok=False)
        result.setdefault(
            "status", "invalid_arguments" if isinstance(exc, ValueError) else "failed"
        )
        result.setdefault("error", sanitize_failure(str(exc)))
        if arguments.request_id:
            result.setdefault("request_id", arguments.request_id)
            result.setdefault("job_id", arguments.request_id)
        print(sanitize_failure(str(exc)), file=sys.stderr)
        print(json.dumps(safe_structure(result), ensure_ascii=False, indent=2))
        return 2 if isinstance(exc, ValueError) else 1
    print(json.dumps(safe_structure(result), ensure_ascii=False, indent=2))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
