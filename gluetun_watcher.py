#!/usr/bin/env python3
"""
gluetun-watcher — Recreates arr containers when gluetun restarts.

Why recreate:
  Docker bakes the resolved gluetun container ID into each dependent
  container's HostConfig.NetworkMode at creation time. When gluetun
  restarts it gets a new ID. restart, stop+start all fail with
  "No such container" because they try to rejoin the old dead namespace.
  The container must be removed and recreated so Docker re-resolves
  'container:gluetun' to the current live ID.

How:
  For each dependent container we:
    1. Capture full config via 'docker inspect'
    2. Stop the container
    3. Remove the container  
    4. Run 'docker run' with the original config reconstructed from inspect
  
  This is done entirely via Docker CLI subprocess calls as root.
"""

import docker
import json
import logging
import subprocess
import time
import sys

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GLUETUN_CONTAINER = "gluetun"

DEPENDENT_CONTAINERS = [
    "radarr",
    "sonarr",
    "prowlarr",
    "qbittorrent",
    "flaresolverr",
]

HEALTH_WAIT_TIMEOUT  = 60
HEALTH_POLL_INTERVAL = 3
RECREATE_DELAY       = 3

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [gluetun-watcher] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd, timeout=60):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def wait_for_gluetun_healthy(client):
    deadline = time.time() + HEALTH_WAIT_TIMEOUT
    log.info("Waiting for gluetun to become healthy (timeout: %ds)...", HEALTH_WAIT_TIMEOUT)
    while time.time() < deadline:
        try:
            container = client.containers.get(GLUETUN_CONTAINER)
            health = container.attrs.get("State", {}).get("Health", {}).get("Status", "none")
            if health == "healthy":
                log.info("gluetun is healthy.")
                return True
            log.info("gluetun health: %s — waiting...", health)
        except docker.errors.NotFound:
            log.warning("gluetun not found yet — waiting...")
        time.sleep(HEALTH_POLL_INTERVAL)
    log.error("Timed out waiting for gluetun to become healthy.")
    return False


def recreate_container(name):
    """
    Inspect, stop, remove, then docker run with reconstructed args.
    """
    log.info("Inspecting '%s'...", name)
    rc, out, err = run(["docker", "inspect", name])
    if rc != 0:
        log.error("Failed to inspect '%s': %s", name, err)
        return False

    try:
        data = json.loads(out)[0]
    except (json.JSONDecodeError, IndexError) as e:
        log.error("Failed to parse inspect output for '%s': %s", name, e)
        return False

    config     = data.get("Config", {})
    host_cfg   = data.get("HostConfig", {})
    image      = config.get("Image", "")

    # Build docker run args from inspect data
    run_args = ["docker", "run", "-d", "--name", name]

    # Restart policy
    restart = host_cfg.get("RestartPolicy", {})
    restart_name = restart.get("Name", "")
    if restart_name:
        run_args += ["--restart", restart_name]

    # Network mode — still contains "container:gluetun" by name,
    # Docker will re-resolve to the current live gluetun ID on creation
    network_mode = f"container:{GLUETUN_CONTAINER}"
    if network_mode:
        run_args += ["--network", network_mode]

    # Environment variables
    for env in config.get("Env") or []:
        run_args += ["-e", env]

    # Volume mounts
    for bind in host_cfg.get("Binds") or []:
        run_args += ["-v", bind]

    # Port bindings (only relevant for gluetun itself, but included for safety)
    port_bindings = host_cfg.get("PortBindings") or {}
    for container_port, host_bindings in port_bindings.items():
        if host_bindings:
            for hb in host_bindings:
                host_ip   = hb.get("HostIp", "")
                host_port = hb.get("HostPort", "")
                if host_ip:
                    run_args += ["-p", f"{host_ip}:{host_port}:{container_port}"]
                else:
                    run_args += ["-p", f"{host_port}:{container_port}"]

    # Capabilities
    for cap in host_cfg.get("CapAdd") or []:
        run_args += ["--cap-add", cap]

    # Devices
    for dev in host_cfg.get("Devices") or []:
        run_args += ["--device", dev.get("PathOnHost", "") + ":" + dev.get("PathInContainer", "")]

    # Labels
    for k, v in (config.get("Labels") or {}).items():
        run_args += ["--label", f"{k}={v}"]

    run_args.append(image)

    # Add original command if present
    cmd = config.get("Cmd")
    if cmd:
        run_args += cmd

    # Stop and remove
    log.info("Stopping '%s'...", name)
    run(["docker", "stop", "-t", "10", name])

    log.info("Removing '%s'...", name)
    rc, _, err = run(["docker", "rm", name])
    if rc != 0:
        log.error("Failed to remove '%s': %s", name, err)
        return False

    # Recreate
    log.info("Recreating '%s'...", name)
    rc, out, err = run(run_args)
    if rc != 0:
        log.error("Failed to recreate '%s': %s", name, err)
        log.error("Command was: %s", " ".join(run_args))
        return False

    log.info("'%s' recreated successfully.", name)
    return True


def recreate_dependents():
    log.info("Starting cascade recreate of %d containers.", len(DEPENDENT_CONTAINERS))
    for name in DEPENDENT_CONTAINERS:
        recreate_container(name)
        time.sleep(RECREATE_DELAY)
    log.info("Cascade recreate complete.")


# ---------------------------------------------------------------------------
# Main event loop
# ---------------------------------------------------------------------------

def main():
    log.info("gluetun-watcher starting up.")
    log.info("Watching for '%s' start events.", GLUETUN_CONTAINER)
    log.info("Dependents: %s", ", ".join(DEPENDENT_CONTAINERS))

    client = docker.from_env()

    try:
        client.ping()
        log.info("Docker socket connected successfully.")
    except Exception as e:
        log.error("Cannot connect to Docker socket: %s", e)
        sys.exit(1)

    event_filters = {
        "type": "container",
        "event": "start",
        "container": GLUETUN_CONTAINER,
    }

    log.info("Listening for Docker events...")

    while True:
        try:
            for event in client.events(filters=event_filters, decode=True):
                container_name = event.get("Actor", {}).get("Attributes", {}).get("name", "unknown")
                log.info("Detected start event for '%s' — gluetun has restarted.", container_name)
                if wait_for_gluetun_healthy(client):
                    recreate_dependents()
                else:
                    log.error("gluetun did not become healthy — skipping cascade recreate.")
        except docker.errors.APIError as e:
            log.error("Docker API error: %s — reconnecting in 5s.", e)
            time.sleep(5)
        except Exception as e:
            log.error("Unexpected error: %s — reconnecting in 5s.", e)
            time.sleep(5)


if __name__ == "__main__":
    main()