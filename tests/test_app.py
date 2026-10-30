"""Public application boundaries for an independent checkout and artifact."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
import zipfile
from unittest.mock import patch

from stripe_double import cli
from test_state import case_path

ROOT = Path(__file__).resolve().parents[1]


class AppTests(unittest.TestCase):
    def test_cli_accepts_owned_state_without_a_machine_specific_volume(self):
        path = case_path(self)
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        stop = threading.Event()
        stop.set()
        args = argparse.Namespace(state=str(path), create=True, scenario="STRIPE-LOCAL-01",
            run=None, instance=None, api_version=None, host="127.0.0.1", port=0, callback=None,
            bootstrap_fd=write_fd)
        # Simulate a host without the developer's optional Scratch mount. The
        # owned absolute state directory remains valid and must be sufficient.
        with patch.object(Path, "is_mount", return_value=False):
            cli.serve_loopback(args, stop)
        bootstrap = json.loads(os.read(read_fd, 4096))
        self.assertEqual(bootstrap["scenario_id"], "STRIPE-LOCAL-01")
        self.assertTrue(path.is_file())

    def test_version_from_an_unrelated_working_directory(self):
        path = case_path(self)
        result = subprocess.run([sys.executable, "-I", "-S", "-B", str(ROOT), "--version"],
            cwd=path.parent, capture_output=True, timeout=4)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout.decode().strip(), "StripeDouble 0.2.0")
        self.assertEqual(result.stderr, b"")

    def test_zipapp_runs_without_checkout_imports_or_site_packages(self):
        path = case_path(self)
        artifact = path.parent / "stripe-double.pyz"
        built = subprocess.run([sys.executable, "-I", "-S", "-B", str(ROOT / "tools/build_app.py"),
            "--output", str(artifact)], cwd=path.parent, capture_output=True, timeout=5)
        self.assertEqual(built.returncode, 0, built.stderr.decode())
        with zipfile.ZipFile(artifact) as archive:
            names = archive.namelist()
        self.assertIn("stripe_double/state.py", names)
        self.assertTrue(all(name == "__main__.py" or name.startswith("stripe_double/") for name in names))
        self.assertFalse(any("__pycache__" in name or name.endswith("README.md") for name in names))
        example = subprocess.run([sys.executable, "-I", "-S", "-B", str(ROOT / "examples/create_account.py"),
            "--app", str(artifact), "--state", str(path)], cwd=path.parent,
            capture_output=True, timeout=10)
        self.assertEqual(example.returncode, 0, example.stderr.decode())
        self.assertEqual(example.stdout, b"Synthetic account created successfully. No money moved.\n")
        self.assertEqual(example.stderr, b"")


if __name__ == "__main__":
    unittest.main()
