#!/usr/bin/env python3
"""Per-user Mickvvw guardian for Linux desktop and local gateway processes.

The guardian stores only executable paths, non-secret runtime paths, PIDs and
health timestamps. Credentials remain in the Hermes profile's existing secret
store/.env and are never copied into this supervisor's config or status file.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import site
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GUARDIAN_VERSION = 2
DEFAULT_INTERVAL_SECONDS = 15
MAX_RESTART_BACKOFF_SECONDS = 60
_STOP_REQUESTED = False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _proc_identity(pid: int) -> tuple[str, str] | None:
    """Return (state, starttime ticks) from /proc without being fooled by ')' in comm."""
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="ascii")
        end = raw.rfind(")")
        if end < 0:
            return None
        fields = raw[end + 2 :].split()
        # fields[0] is field 3 (state); field 22 (starttime) is fields[19].
        if len(fields) <= 19:
            return None
        return fields[0], fields[19]
    except (OSError, ValueError, IndexError):
        return None


def process_identity_alive(pid: int | None, expected_start_time: str | int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0 or expected_start_time is None:
        return False
    identity = _proc_identity(pid)
    return bool(identity and identity[0] not in {"Z", "X"} and identity[1] == str(expected_start_time))


def should_start_gateway(runtime: Any) -> bool:
    """Do not override the operator's durable `hermes gateway stop` intent."""
    return not (isinstance(runtime, dict) and runtime.get("desired_state") == "stopped")


def classify_gateway_state(pid: int | None, runtime: Any, probe_error: str | None = None) -> str:
    if probe_error:
        return "unavailable"
    if pid:
        return "running"
    if not should_start_gateway(runtime):
        return "stopped_by_user"
    if isinstance(runtime, dict) and runtime.get("gateway_state") in {"startup_failed", "degraded"}:
        return "recovering"
    return "starting"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _read_gateway_runtime(home: Path) -> dict[str, Any] | None:
    return _read_json(home / "gateway_state.json")


def _prepare_gateway_probe(config: dict[str, Any]):
    runtime = config.get("runtime") if isinstance(config.get("runtime"), dict) else {}
    repo_dir = runtime.get("repoDir")
    site_packages = runtime.get("sitePackages")
    if not isinstance(repo_dir, str) or not isinstance(site_packages, str):
        raise RuntimeError("runtime_paths_unavailable")
    if repo_dir not in sys.path:
        sys.path.insert(0, repo_dir)
    if Path(site_packages).is_dir():
        site.addsitedir(site_packages)
    from gateway.status import get_running_pid  # import only after payload paths are ready
    return get_running_pid


def _gateway_status(config: dict[str, Any], get_running_pid) -> tuple[int | None, dict[str, Any] | None, str | None]:
    gateway = config.get("gateway")
    if not isinstance(gateway, dict):
        return None, None, None
    home_value = gateway.get("home")
    if not isinstance(home_value, str) or not Path(home_value).is_absolute():
        return None, None, "gateway_home_unavailable"
    home = Path(home_value)
    runtime = _read_gateway_runtime(home)
    try:
        pid = get_running_pid(home / "gateway.pid", cleanup_stale=False)
        return (int(pid) if pid else None), runtime, None
    except Exception:
        # Ambiguous identity is fail-closed: never spawn a second gateway based on
        # a status probe that could not establish whether another process owns it.
        return None, runtime, "gateway_identity_unavailable"


def _safe_spec(spec: Any) -> tuple[str, list[str], str | None] | None:
    if not isinstance(spec, dict):
        return None
    command = spec.get("command")
    args = spec.get("args", [])
    cwd = spec.get("cwd")
    if not isinstance(command, str) or not Path(command).is_absolute():
        return None
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        return None
    if cwd is not None and (not isinstance(cwd, str) or not Path(cwd).is_absolute()):
        return None
    return command, args, cwd


def _spawn(spec: Any, environment: dict[str, str], *, gui: bool = False) -> subprocess.Popen | None:
    safe = _safe_spec(spec)
    if safe is None:
        return None
    command, args, cwd = safe
    env = dict(environment)
    if gui:
        env.update({key: value for key, value in os.environ.items() if key in {
            "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP",
            "DESKTOP_SESSION", "GDK_BACKEND",
        }})
    try:
        return subprocess.Popen(
            [command, *args],
            cwd=cwd or str(Path(command).parent),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return None


def _backoff(attempt: int) -> int:
    return min(MAX_RESTART_BACKOFF_SECONDS, 2 ** min(max(attempt, 1), 6))


def _on_signal(_signum, _frame) -> None:
    global _STOP_REQUESTED
    _STOP_REQUESTED = True


def run(config_path: Path) -> int:
    config_path = config_path.resolve()
    status_path = config_path.with_name("status.json")
    lock_path = config_path.with_name("supervisor.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_handle = open(lock_path, "a", encoding="ascii")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_handle.close()
        return 0

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    supervisor_start = _proc_identity(os.getpid())
    supervisor_start_time = supervisor_start[1] if supervisor_start else ""
    app_child: subprocess.Popen | None = None
    gateway_child: subprocess.Popen | None = None
    app_pid: int | None = None
    app_start_time: str | None = None
    app_started_at = time.monotonic()
    gateway_attempt = 0
    app_attempt = 0
    next_app_start = 0.0
    next_gateway_start = 0.0
    last_gateway_pid: int | None = None
    last_error: str | None = None
    gateway_state = "unknown"
    gateway_probe = None
    probe_ready_for = None

    def write_status(config: dict[str, Any], *, running: bool, error: str | None = None) -> None:
        _atomic_json(status_path, {
            "version": GUARDIAN_VERSION,
            "supervisorPid": os.getpid(),
            "supervisorStartTime": supervisor_start_time,
            "supervisorRunning": running,
            "heartbeatAt": _utc_now(),
            "appPid": app_pid,
            "appStartTime": app_start_time,
            "appRestartCount": app_attempt,
            "gatewayPid": last_gateway_pid,
            "gatewayState": gateway_state,
            "gatewayRestartCount": gateway_attempt,
            "lastError": error,
        })

    try:
        while not _STOP_REQUESTED:
            if config_path.with_name("user-stop").exists():
                break
            config = _read_json(config_path)
            if config is None:
                last_error = "invalid_config"
                break
            if config.get("version") != GUARDIAN_VERSION:
                last_error = "guardian_version_mismatch"
                break
            if config.get("enabled") is not True:
                gateway_state = "not_supervised"
                write_status(config, running=False)
                return 0

            environment_raw = config.get("environment")
            environment = {
                str(key): str(value)
                for key, value in environment_raw.items()
                if isinstance(environment_raw, dict) and isinstance(key, str) and isinstance(value, (str, int, float))
            } if isinstance(environment_raw, dict) else {}
            environment.setdefault("HOME", str(Path.home()))
            environment.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")

            # Publish the supervisor heartbeat before process checks/imports so
            # the UI can distinguish a live Guardian from one that never started.
            gateway_state = "starting" if isinstance(config.get("gateway"), dict) else "not_managed"
            write_status(config, running=True, error=last_error)

            app_spec = config.get("app")
            configured_pid = app_spec.get("pid") if isinstance(app_spec, dict) else None
            configured_start = app_spec.get("startTime") if isinstance(app_spec, dict) else None
            if isinstance(configured_pid, int) and configured_pid > 0 and process_identity_alive(configured_pid, configured_start):
                if configured_pid != app_pid or str(configured_start) != str(app_start_time):
                    app_pid = configured_pid
                    app_start_time = str(configured_start)
                    app_child = None
                    app_started_at = time.monotonic()
            elif not process_identity_alive(app_pid, app_start_time):
                if app_child is not None:
                    ended = app_child.poll() is not None
                    if ended:
                        if time.monotonic() - app_started_at >= 60:
                            app_attempt = 0
                        app_attempt += 1
                        next_app_start = time.monotonic() + _backoff(app_attempt)
                        app_child = None
                if app_child is None and time.monotonic() >= next_app_start:
                    app_child = _spawn(app_spec, environment, gui=True)
                    if app_child is not None:
                        app_pid = app_child.pid
                        identity = _proc_identity(app_pid)
                        app_start_time = identity[1] if identity else None
                        app_started_at = time.monotonic()
                    else:
                        app_pid = None
                        app_start_time = None
                        app_attempt += 1
                        next_app_start = time.monotonic() + _backoff(app_attempt)
                        last_error = "desktop_launch_failed"

            gateway_spec = config.get("gateway")
            if isinstance(gateway_spec, dict):
                gateway_runtime = config.get("runtime") if isinstance(config.get("runtime"), dict) else {}
                signature = (gateway_spec.get("home"), gateway_spec.get("command"), tuple(gateway_spec.get("args", [])))
                if signature != probe_ready_for:
                    try:
                        gateway_probe = _prepare_gateway_probe(config)
                        probe_ready_for = signature
                    except Exception:
                        gateway_probe = None
                        probe_ready_for = signature
                        last_error = "gateway_probe_unavailable"

                if gateway_probe is None:
                    gateway_state = "unavailable"
                    last_gateway_pid = None
                else:
                    gateway_pid, gateway_runtime, probe_error = _gateway_status(config, gateway_probe)
                    gateway_state = classify_gateway_state(gateway_pid, gateway_runtime, probe_error)
                    if gateway_pid:
                        last_gateway_pid = gateway_pid
                        gateway_child = None if gateway_child is not None and gateway_child.poll() is not None else gateway_child
                        if gateway_state == "running":
                            last_error = None if last_error == "gateway_launch_failed" else last_error
                    elif probe_error:
                        last_gateway_pid = None
                        last_error = probe_error
                    elif not should_start_gateway(gateway_runtime):
                        last_gateway_pid = None
                        gateway_state = "stopped_by_user"
                        gateway_child = None if gateway_child is not None and gateway_child.poll() is not None else gateway_child
                    elif gateway_child is not None and gateway_child.poll() is None:
                        last_gateway_pid = None
                        gateway_state = "starting"
                    elif time.monotonic() >= next_gateway_start:
                        gateway_child = _spawn(gateway_spec, environment, gui=False)
                        if gateway_child is not None:
                            gateway_attempt += 1
                            last_gateway_pid = gateway_child.pid
                            gateway_state = "starting"
                            next_gateway_start = time.monotonic() + _backoff(gateway_attempt)
                        else:
                            last_gateway_pid = None
                            gateway_state = "recovering"
                            gateway_attempt += 1
                            next_gateway_start = time.monotonic() + _backoff(gateway_attempt)
                            last_error = "gateway_launch_failed"
            else:
                gateway_state = "not_managed"
                last_gateway_pid = None

            write_status(config, running=True, error=last_error)
            time.sleep(max(2, int(config.get("intervalSeconds", DEFAULT_INTERVAL_SECONDS))))
    except Exception:
        # Never persist exception text: it may contain paths or sensitive process context.
        last_error = "supervisor_runtime_error"
    finally:
        final_config = _read_json(config_path) or {}
        gateway_state = "not_supervised" if final_config.get("enabled") is not True else gateway_state
        write_status(final_config, running=False, error=last_error)
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            lock_handle.close()
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 2
    return run(Path(argv[1]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
