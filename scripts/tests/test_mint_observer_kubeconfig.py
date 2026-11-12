"""Offline security tests: all kubectl responses/tokens are synthetic fixtures.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -v
Only openssl runs for real, to generate a temporary private test CA. No kubectl,
network request, credential issuance, or cluster operation is ever performed.
"""

import base64
from contextlib import redirect_stderr, redirect_stdout
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "mint-observer-kubeconfig.py"
SPEC = importlib.util.spec_from_file_location("mint_observer_kubeconfig", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)

SYNTHETIC_TOKEN = "SYNTHETIC-OBSERVER-TOKEN-NOT-A-REAL-CREDENTIAL"
ADMIN_SECRETS = [
    "SYNTHETIC-ADMIN-TOKEN", "SYNTHETIC-ADMIN-CERT", "SYNTHETIC-ADMIN-KEY",
    "SYNTHETIC-EXEC-SECRET", "SYNTHETIC-AUTH-PROVIDER-SECRET",
]
CONTEXT = "synthetic-explicit-admin-context"


def fixture_request(minutes=60):
    return {
        "apiVersion": "authentication.k8s.io/v1", "kind": "TokenRequest",
        "status": {
            "token": SYNTHETIC_TOKEN,
            "expirationTimestamp": (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"),
        },
    }


class ObserverKubeconfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Honor runtime TMPDIR, resolving macOS's /var alias to a physical path.
        scratch = Path(os.environ.get("TMPDIR", Path.home() / ".hermes/cache/scratch")).resolve()
        scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        cls.ca_workspace = tempfile.TemporaryDirectory(prefix="observer-ca-test-", dir=scratch)
        cls.addClassCleanup(cls.ca_workspace.cleanup)
        cls.scratch = scratch
        root = Path(cls.ca_workspace.name)
        if not shutil.which("openssl"):
            raise RuntimeError("openssl is required to generate the offline test CA")
        result = subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-subj", "/CN=Synthetic Offline Observer Test CA", "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-keyout", str(root / "ca.key"), "-out", str(root / "ca.pem")],
            capture_output=True, text=True, check=False, timeout=30, umask=0o077,
        )
        if result.returncode:
            raise RuntimeError("Could not generate the temporary offline test CA with openssl")
        cls.ca_pem = (root / "ca.pem").read_text()
        cls.ca_data = base64.b64encode(cls.ca_pem.encode("ascii")).decode("ascii")
        cls.ca_key = (root / "ca.key").read_text()

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="observer-helper-test-", dir=self.scratch)
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.output = self.root / "private" / "new" / "observer.json"
        self.config = {
            "apiVersion": "v1", "kind": "Config", "current-context": CONTEXT,
            "clusters": [{"name": "synthetic-admin-cluster", "cluster": {
                "server": "https://synthetic-api.invalid:6443",
                "certificate-authority-data": self.ca_data,
                "tls-server-name": "synthetic-api.invalid",
                "extensions": [{"name": "never-copy", "extension": {"secret": ADMIN_SECRETS[0]}}],
            }}],
            "contexts": [{"name": CONTEXT, "context": {
                "cluster": "synthetic-admin-cluster", "user": "synthetic-admin", "namespace": "admin-only",
            }}],
            "users": [{"name": "synthetic-admin", "user": {
                "token": ADMIN_SECRETS[0], "client-certificate-data": ADMIN_SECRETS[1],
                "client-key-data": ADMIN_SECRETS[2],
                "exec": {"command": "synthetic-plugin-never-run", "args": [ADMIN_SECRETS[3]]},
                "auth-provider": {"name": "synthetic", "config": {"secret": ADMIN_SECRETS[4]}},
            }}],
            "extensions": [{"name": "never-copy", "extension": {"secret": ADMIN_SECRETS[1]}}],
        }
        self.env = patch.dict(os.environ, {"HOME": str(self.home), "KUBECONFIG": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def invoke(self, *, config=None, response=None, output=None, admin=None, failure=None, race=None):
        config = self.config if config is None else config
        response = fixture_request() if response is None else response
        stdout, stderr = io.StringIO(), io.StringIO()
        calls = []

        def mocked_kubectl(argv, **kwargs):
            calls.append((argv, kwargs))
            self.assertEqual(argv[0], "kubectl")
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["text"])
            self.assertFalse(kwargs["check"])
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["timeout"], 60)
            self.assertNotIn(SYNTHETIC_TOKEN, json.dumps(argv))
            for secret in ADMIN_SECRETS:
                self.assertNotIn(secret, json.dumps(argv))
            is_request = "create" in argv
            if failure and failure[0] == ("token" if is_request else "config"):
                value = failure[1]
                if isinstance(value, BaseException):
                    raise value
                return subprocess.CompletedProcess(argv, 1, SYNTHETIC_TOKEN, " ".join(ADMIN_SECRETS))
            if is_request and race:
                race()
            data = response if is_request else config
            payload = data if isinstance(data, str) else json.dumps(data)
            # Even successful stderr is secret and must not be echoed.
            return subprocess.CompletedProcess(argv, 0, payload, " ".join(ADMIN_SECRETS) + SYNTHETIC_TOKEN)

        arguments = ["--context", CONTEXT, "--output", str(self.output if output is None else output)]
        if admin is not None:
            arguments.extend(["--admin-kubeconfig", str(admin)])
        with patch.object(helper.subprocess, "run", side_effect=mocked_kubectl), redirect_stdout(stdout), redirect_stderr(stderr):
            code = helper.main(arguments)
        text = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(SYNTHETIC_TOKEN, text)
        for secret in ADMIN_SECRETS:
            self.assertNotIn(secret, text)
        return code, stdout.getvalue(), stderr.getvalue(), calls

    def assert_no_file(self):
        self.assertFalse(os.path.lexists(self.output))
        if self.output.parent.exists():
            self.assertEqual(list(self.output.parent.iterdir()), [])

    def test_success_is_minimal_private_and_uses_explicit_context(self):
        response = fixture_request()
        code, stdout, stderr, calls = self.invoke(response=response)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn(response["status"]["expirationTimestamp"], stdout)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], ["kubectl", "config", "view", "--context", CONTEXT, "--minify", "--flatten", "--raw", "-o", "json"])
        self.assertEqual(calls[1][0], ["kubectl", "--context", CONTEXT, "-n", "navi-observer", "create", "token", "navi-observer", "--duration=1h", "-o", "json"])
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.output.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.output.parent.parent.stat().st_mode), 0o700)
        content = self.output.read_text()
        actual = json.loads(content)
        self.assertEqual(set(actual), {"apiVersion", "kind", "clusters", "users", "contexts", "current-context"})
        self.assertEqual(actual["users"], [{"name": "navi-observer", "user": {"token": SYNTHETIC_TOKEN}}])
        self.assertEqual(actual["contexts"], [{"name": "navi-observer", "context": {
            "cluster": "navi-observer", "user": "navi-observer", "namespace": "obojima-tools"}}])
        self.assertEqual(actual["current-context"], "navi-observer")
        self.assertEqual(actual["clusters"], [{"name": "navi-observer", "cluster": {
            "server": "https://synthetic-api.invalid:6443", "certificate-authority-data": self.ca_data,
            "tls-server-name": "synthetic-api.invalid"}}])
        for secret in ADMIN_SECRETS:
            self.assertNotIn(secret, content)
        for forbidden in ("exec", "client-key", "client-certificate", "auth-provider", "extensions", "admin-only"):
            self.assertNotIn(forbidden, content)
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])

    def test_optional_admin_kubeconfig_on_both_commands_never_modified(self):
        admin = self.root / "admin-config.json"
        original = json.dumps(self.config)
        admin.write_text(original)
        code, _, _, calls = self.invoke(admin=admin)
        self.assertEqual(code, 0)
        self.assertEqual(admin.read_text(), original)
        for argv, _ in calls:
            self.assertEqual(argv[:3], ["kubectl", "--kubeconfig", str(admin)])
            self.assertEqual(argv[argv.index("--context") + 1], CONTEXT)
        self.assertFalse((self.home / ".kube" / "config").exists())

    def test_optional_tls_name_can_be_omitted_and_false_insecure_flag_is_not_copied(self):
        cluster = self.config["clusters"][0]["cluster"]
        del cluster["tls-server-name"]
        cluster["insecure-skip-tls-verify"] = False
        code, _, _, _ = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(set(json.loads(self.output.read_text())["clusters"][0]["cluster"]), {"server", "certificate-authority-data"})

    def test_missing_invalid_or_non_ca_certificate_rejected_before_token(self):
        cases = [None, "", "not-base64!", base64.b64encode(b"not PEM").decode(),
                 base64.b64encode(b"-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n").decode(),
                 base64.b64encode((self.ca_pem + self.ca_key).encode()).decode()]
        for ca in cases:
            with self.subTest(ca_type=cases.index(ca)):
                config = copy.deepcopy(self.config)
                if ca is None:
                    del config["clusters"][0]["cluster"]["certificate-authority-data"]
                else:
                    config["clusters"][0]["cluster"]["certificate-authority-data"] = ca
                code, _, _, calls = self.invoke(config=config)
                self.assertEqual(code, 1)
                self.assertEqual(len(calls), 1)
                self.assert_no_file()

    def test_insecure_tls_and_proxy_configuration_rejected_before_token(self):
        for field, value in [("insecure-skip-tls-verify", True), ("insecure-skip-tls-verify", "false"),
                             ("proxy-url", "http://SYNTHETIC-ADMIN-TOKEN:password@proxy.invalid"), ("proxy-url", "")]:
            with self.subTest(field=field, value_type=type(value).__name__):
                config = copy.deepcopy(self.config)
                config["clusters"][0]["cluster"][field] = value
                code, _, _, calls = self.invoke(config=config)
                self.assertEqual(code, 1)
                self.assertEqual(len(calls), 1)
                self.assert_no_file()

    def test_invalid_or_credential_bearing_server_rejected_before_token(self):
        for server in ["http://synthetic.invalid", "https://user:password@synthetic.invalid", "https://synthetic.invalid?token=secret",
                       "https://synthetic.invalid/#secret", "https://synthetic.invalid:bad", "https:///missing", None]:
            with self.subTest(server=server):
                config = copy.deepcopy(self.config)
                config["clusters"][0]["cluster"]["server"] = server
                code, _, _, calls = self.invoke(config=config)
                self.assertEqual(code, 1)
                self.assertEqual(len(calls), 1)
                self.assert_no_file()

    def test_malformed_or_wrong_context_config_rejected_before_token(self):
        wrong = copy.deepcopy(self.config)
        wrong["contexts"][0]["name"] = "different-context"
        for config in ["not JSON " + ADMIN_SECRETS[0], [], {}, wrong]:
            with self.subTest(kind=type(config).__name__):
                code, _, _, calls = self.invoke(config=config)
                self.assertEqual(code, 1)
                self.assertEqual(len(calls), 1)
                self.assert_no_file()

    def test_overwrite_and_symlink_output_refused_without_kubectl(self):
        self.output.parent.mkdir(mode=0o700, parents=True)
        self.output.write_text("existing output must survive")
        code, _, _, calls = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertEqual(self.output.read_text(), "existing output must survive")
        self.output.unlink()
        victim = self.root / "nonexistent-target"
        self.output.symlink_to(victim)
        code, _, _, calls = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertFalse(victim.exists())
        self.assertTrue(self.output.is_symlink())

    def test_symlink_parent_refused(self):
        real = self.root / "real"
        real.mkdir(mode=0o700)
        alias = self.root / "alias"
        alias.symlink_to(real, target_is_directory=True)
        code, _, _, calls = self.invoke(output=alias / "observer.json")
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertEqual(list(real.iterdir()), [])

    def test_default_admin_and_all_environment_input_paths_refused(self):
        admin = self.root / "explicit-admin"
        env1, env2 = self.root / "env-admin-one", self.root / "env-admin-two"
        with patch.dict(os.environ, {"KUBECONFIG": str(env1) + os.pathsep + str(env2)}):
            for output in [self.home / ".kube" / "config", admin, env1, env2]:
                with self.subTest(output_name=output.name):
                    code, _, _, calls = self.invoke(output=output, admin=admin)
                    self.assertEqual(code, 1)
                    self.assertEqual(calls, [])
                    self.assertFalse(output.exists())

    def test_git_directory_and_git_file_ancestor_refused(self):
        for marker in ["directory", "file"]:
            with self.subTest(marker=marker):
                repo = self.root / ("git-" + marker)
                repo.mkdir(mode=0o700)
                if marker == "directory":
                    (repo / ".git").mkdir()
                else:
                    (repo / ".git").write_text("gitdir: /synthetic/not/a/real/gitdir\n")
                output = repo / "nested" / "private" / "observer.json"
                code, _, _, calls = self.invoke(output=output)
                self.assertEqual(code, 1)
                self.assertEqual(calls, [])
                self.assertFalse((repo / "nested").exists())

    def test_unsafe_existing_directory_refused_before_token(self):
        self.output.parent.mkdir(mode=0o700, parents=True)
        self.output.parent.chmod(0o777)
        code, _, _, calls = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assert_no_file()

    def test_failed_kubectl_results_and_exceptions_are_sanitized(self):
        for stage in ["config", "token"]:
            for exception in [None,
                              subprocess.CalledProcessError(1, ["kubectl"], output=SYNTHETIC_TOKEN, stderr=ADMIN_SECRETS[0]),
                              subprocess.TimeoutExpired(["kubectl"], 60, output=SYNTHETIC_TOKEN, stderr=ADMIN_SECRETS[0]),
                              OSError(ADMIN_SECRETS[0])]:
                with self.subTest(stage=stage, exception=type(exception).__name__):
                    code, _, stderr, calls = self.invoke(failure=(stage, exception))
                    self.assertEqual(code, 1)
                    self.assertEqual(len(calls), 1 if stage == "config" else 2)
                    if stage == "token":
                        self.assertIn("Wait for Argo", stderr)
                    self.assert_no_file()

    def test_malformed_missing_or_expired_token_response_never_creates_file(self):
        invalid = ["malformed " + SYNTHETIC_TOKEN, [], {}, {"kind": "TokenRequest"}]
        for key, value in [("token", None), ("token", ""), ("token", "contains whitespace"),
                           ("expirationTimestamp", None), ("expirationTimestamp", "not-a-date"),
                           ("expirationTimestamp", "2099-01-01T00:00:00"),
                           ("expirationTimestamp", "2000-01-01T00:00:00Z"),
                           ("expirationTimestamp", "2099-99-99T00:00:00Z")]:
            response = fixture_request()
            response["status"][key] = value
            invalid.append(response)
        for index, response in enumerate(invalid):
            with self.subTest(case=index):
                code, stdout, _, calls = self.invoke(response=response)
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertEqual(len(calls), 2)
                self.assert_no_file()

    def test_server_shorter_and_longer_lifetime_report_actual_expiry_and_warning(self):
        for minutes in [10, 120]:
            with self.subTest(minutes=minutes):
                response = fixture_request(minutes)
                output = self.root / ("expiry-" + str(minutes) + ".json")
                code, stdout, stderr, _ = self.invoke(response=response, output=output)
                self.assertEqual(code, 0)
                self.assertIn(response["status"]["expirationTimestamp"], stdout)
                self.assertIn("Warning", stderr)
                self.assertIn("actual expiry", stderr)
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_exclusive_publication_cannot_overwrite_racing_file_or_symlink(self):
        for symlink in [False, True]:
            with self.subTest(symlink=symlink):
                target = self.root / ("race-" + str(symlink) + ".json")
                victim = self.root / "victim"
                victim.write_text("untouched")
                def race():
                    if symlink:
                        target.symlink_to(victim)
                    else:
                        target.write_text("racing file survives")
                code, stdout, _, _ = self.invoke(output=target, race=race)
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertEqual(victim.read_text(), "untouched")
                if symlink:
                    self.assertTrue(target.is_symlink())
                else:
                    self.assertEqual(target.read_text(), "racing file survives")
                self.assertFalse(list(self.root.glob(".navi-observer-*")))

    def test_write_failure_cleans_temporary_file_without_partial_output(self):
        with patch.object(helper.json, "dump", side_effect=OSError(SYNTHETIC_TOKEN)):
            code, stdout, _, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assert_no_file()

    def test_required_arguments_and_help_do_not_run_kubectl(self):
        with patch.object(helper.subprocess, "run") as run:
            for argv, exit_code in [([], 2), (["--context", CONTEXT], 2), (["--output", str(self.output)], 2), (["--help"], 0)]:
                with self.subTest(argv=argv), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        helper.main(argv)
                    self.assertEqual(raised.exception.code, exit_code)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
