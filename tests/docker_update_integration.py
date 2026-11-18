"""Run on a fresh Docker CI checkout: PYTHONPATH=. python3 tests/docker_update_integration.py."""
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from unittest.mock import patch

from ticket_pilot import docker_update_manager as updater


def api(method, token, path="/api/update"):
    url = "http://127.0.0.1:3032"
    req = urllib.request.Request(url + path, method=method, data=b"{}" if method == "POST" else None,
                                 headers={"Origin": url, "Cookie": f"tp_session={token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def wait_until_idle(manager):
    deadline = time.monotonic() + 120
    while True:
        try:
            manager.exec(manager.container(), "python3", "-c", updater.PREFLIGHT)
            return
        except RuntimeError:
            if time.monotonic() > deadline:
                raise
            time.sleep(2)


def local_variant(manager, base, body):
    with tempfile.TemporaryDirectory() as context:
        return updater._run(manager.docker, "build", "--quiet", "-f", "-", context,
                            input=f"FROM {base}\n{body}\n", timeout=120)


def main():
    os.environ["COMPOSE_PROJECT_NAME"] = "tp-update-ci"
    os.environ["TP_PORT"] = "3032"
    manager = updater.Manager()
    env_file = manager.docker_dir / ".env"
    override = manager.docker_dir / "compose.override.yaml"
    # This script is meant for a fresh CI checkout. Use the production installer with custom image ownership.
    with env_file.open("x") as f:
        f.write("TP_ADMINS=ci-admin\nTP_ORG=ci-org\n")
    with override.open("x") as f:
        f.write("""services:
  tp:
    build:
      args:
        TP_RELEASE_VERSION: v0.0.1
        TP_UID: "1001"
        TP_GID: "1002"
""")
    registry_container = "tp-update-ci-registry"
    try:
        updater._run(manager.docker, "run", "--rm", "-d", "--name", registry_container,
                     "-p", "127.0.0.1::5000", "registry:2")
        address = updater._run(manager.docker, "port", registry_container, "5000/tcp")
        registry_image = address + "/toothpaste"
        # CI exercises the real Compose startup. The user service is validated separately with systemd-analyze.
        with patch.object(updater, "install_service") as registration:
            updater.install(manager)
            registration.assert_called_once_with(manager)
            original = manager.container()
            assert manager.exec(original, "python3", "-c", updater.PROBE) == "v0.0.1"
            assert manager.exec(original, "printenv", "TP_DOCKER_UPDATE_DIR") == "/run/tp-update"
            assert manager.exec(original, "test", "-w", "/run/tp-update/requests") == ""
            registration.reset_mock()
            updater.install(manager)
            registration.assert_called_once_with(manager)
            assert manager.container() == original
        admin, visitor = manager.exec(original, "python3", "-c", """
from pathlib import Path
from ticket_pilot import core, lan
con = core.db()
core.meta_set(con, 'update_check', {'latest': 'v0.0.2', 'at': core.now()})
core.meta_set(con, 'update_test_marker', 'preserved')
con.commit()
for login in ('ci-admin', 'ci-member'):
    print(lan.new_session(login, 'ci-org', '127.0.0.1'))
(Path.home() / 'tp-work' / 'update-test-marker').write_text('preserved')
""").splitlines()
        base = "ticket-pilot:ci"  # native release-style image built by the previous step
        for version, cmd in (("v0.0.2", ""), ("v0.0.3", 'CMD ["/bin/false"]')):
            image = local_variant(manager, base, f"ENV TP_RELEASE_VERSION={version}\n{cmd}")
            ref = f"{registry_image}:{version}"
            updater._run(manager.docker, "image", "tag", image, ref)
            updater._run(manager.docker, "push", ref)
            updater._run(manager.docker, "image", "rm", ref)

        def request(version):
            manager.publish(state="idle")
            code, denied = api("POST", visitor)
            assert code == 403, denied
            code, reply = api("POST", admin)
            assert code == 200, reply
            job = manager.claim()
            assert job == {"id": reply["job"]["id"], "tag": version}, job
            return job

        wait_until_idle(manager)
        holder = subprocess.Popen(
            [manager.docker, "exec", "-i", original, "python3", "-c",
             "import fcntl, sys; f = open('/opt/tp/data/tick.lock', 'a'); "
             "fcntl.flock(f, fcntl.LOCK_EX); print('locked', flush=True); sys.stdin.read()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        try:
            assert holder.stdout.readline().strip() == "locked"
            job = request("v0.0.2")
            with patch.object(updater, "latest_release", return_value=job["tag"]), \
                 patch.object(manager, "pull", return_value=None):
                manager.apply(job)
            assert manager.snapshot()["state"] == "failed", manager.snapshot()
            assert "자동화가 실행 중" in manager.snapshot()["message"]
            assert manager.container() == original
        finally:
            holder.communicate(timeout=10)

        job = request("v0.0.2")
        with patch.object(updater, "latest_release", return_value=job["tag"]), \
             patch.object(updater.channel, "REGISTRY_IMAGE", registry_image):
            manager.apply(job)
        assert manager.snapshot()["state"] == "done", manager.snapshot()
        updated = manager.container()
        assert original != updated
        assert manager.exec(updated, "id", "-u") == "1001"
        assert manager.exec(updated, "id", "-g") == "1002"
        assert api("GET", admin)[1]["current"] == "v0.0.2"
        assert manager.exec(updated, "cat", "/home/node/tp-work/update-test-marker") == "preserved"
        assert manager.exec(updated, "python3", "-c",
                            "from ticket_pilot import core; print(core.meta_get(core.db(), 'update_test_marker'))") == "preserved"
        manager.exec(updated, "python3", "-c", """
from ticket_pilot import core
con = core.db()
core.meta_set(con, 'update_check', {'latest': 'v0.0.3', 'at': core.now()})
con.commit()
""")
        wait_until_idle(manager)
        job = request("v0.0.3")
        with patch.object(updater, "latest_release", return_value=job["tag"]), \
             patch.object(updater.channel, "REGISTRY_IMAGE", registry_image), patch.object(updater, "PROBE_SECONDS", 25):
            manager.apply(job)
        state = manager.snapshot()
        assert state["state"] == "failed" and "복구" in state["message"], state
        assert api("GET", admin)[1]["current"] == "v0.0.2"
        assert manager.exec(manager.container(), "cat", "/home/node/tp-work/update-test-marker") == "preserved"
    finally:
        try:
            print(manager.compose("logs", "--tail=30", "tp"), flush=True)
            manager.compose("down", "-v")
        finally:
            subprocess.run([manager.docker, "rm", "-f", registry_container], capture_output=True)
            env_file.unlink()
            override.unlink()


if __name__ == "__main__":
    main()
