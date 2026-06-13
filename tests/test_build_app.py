"""Verify scripts/build-app.sh produces a valid, signed .app bundle. macOS only."""
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only packaging")

REPO = Path(__file__).resolve().parent.parent
BUILD = REPO / "scripts" / "build-app.sh"


@pytest.fixture(scope="module")
def built_app(tmp_path_factory):
    out = tmp_path_factory.mktemp("appout")
    env = {**os.environ, "OUT_DIR": str(out)}
    subprocess.run(["bash", str(BUILD)], cwd=REPO, env=env, check=True,
                   capture_output=True, text=True)
    return out / "Claude Fleet.app"


def test_bundle_structure(built_app):
    launcher = built_app / "Contents" / "MacOS" / "claude-fleet"
    assert launcher.exists()
    assert os.access(launcher, os.X_OK)


def test_info_plist(built_app):
    plist = built_app / "Contents" / "Info.plist"
    subprocess.run(["plutil", "-lint", str(plist)], check=True, capture_output=True)
    data = plistlib.loads(plist.read_bytes())
    assert data["CFBundleIdentifier"] == "com.tianyilt.claude-fleet"
    assert data["CFBundleExecutable"] == "claude-fleet"
    assert "NSAppleEventsUsageDescription" in data


def test_codesigned(built_app):
    # ad-hoc signature must verify
    subprocess.run(["codesign", "--verify", "--deep", str(built_app)],
                   check=True, capture_output=True)
