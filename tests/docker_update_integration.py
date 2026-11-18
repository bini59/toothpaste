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
    # The PR exercises replacement using local images. Publishing to GHCR is a release workflow operation.
    with tempfile.TemporaryDirectory() as context:
        return updater._run(manager.docker, "build", "--quiet", "-f", "-", context,
                            input=f"FROM {base}\n{body}\n", timeout=120)


def main():
    os.environ["COMPOSE_PROJECT_NAME"] = "tp-update-ci"
    os.environ["TP_PORT"] = "3032"
    manager = updater.Manager()
    manager.prepare()
    env_file = manager.docker_dir / ".env"
    # This script is meant for a fresh CI checkout.
    with env_file.open("x") as f:
        f.write("TP_ADMINS=ci-admin\nTP_ORG=ci-org\n")
    try:
        manager.compose("build", "--build-arg", "TP_RELEASE_VERSION=v0.0.1", "tp", timeout=900)
        manager.compose("up", "-d", "--no-build", "tp")
        manager.wait("v0.0.1")
        original = manager.container()
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
        base = manager.image_name()
        next_image = local_variant(manager, base, "ENV TP_RELEASE_VERSION=v0.0.2")
        broken_image = local_variant(manager, base, 'ENV TP_RELEASE_VERSION=v0.0.3\nCMD ["/bin/false"]')

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
        job = request("v0.0.2")
        with patch.object(updater, "latest_release", return_value=job["tag"]), \
             patch.object(manager, "pull", return_value=next_image):
            manager.apply(job)
        assert manager.snapshot()["state"] == "done", manager.snapshot()
        updated = manager.container()
        assert original != updated
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
             patch.object(manager, "pull", return_value=broken_image), patch.object(updater, "PROBE_SECONDS", 15):
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
            env_file.unlink()


if __name__ == "__main__":
    main()
