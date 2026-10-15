"""Isolation for every test. Runs before any modules.* import, since the modules compute their paths with
expanduser when imported: HOME points at a temp folder (removed at exit), ADMIN_CONFIG is dropped, and
the network and child processes are cut off so a test can't reach a real service, Orca or the desktop.
common.run turns the blocked subprocess.run into "" (and orca() into None), as if the tool were missing."""
import atexit
import os
import shutil
import subprocess
import tempfile
import urllib.request

HOME = tempfile.mkdtemp(prefix="homelab-test-")
os.environ["HOME"] = HOME
os.environ.pop("ADMIN_CONFIG", None)
atexit.register(shutil.rmtree, HOME, ignore_errors=True)


def _no_network(*args, **kwargs):
    raise AssertionError("tests must not reach the network; patch the call instead")


def _no_process(*args, **kwargs):
    raise PermissionError("tests must not run commands; patch the call instead")


urllib.request.urlopen = _no_network
subprocess.run = _no_process
