"""Offline phase-one Foundry backup isolation regression tests.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests \
    -p 'test_foundry_backup_isolation.py' -v
Requires kubectl kustomize and Ruby stdlib YAML/JSON; Python uses only stdlib.
Copies the actual non-secret resources and Kustomization into a temporary directory,
removing ONLY generators before rendering. No plugins, API server, network, live
kubeconfig, credentials, or private keys are used. Encrypted files are inspected
separately, NOT decrypted: this is not a full KSOPS application render, MAC check,
proof of distinct plaintext repositories/passwords, or live backup verification.
All six movers must remain paused in this first stage. Unpausing new movers needs
separate approval after checking actual legacy mover pod/job inactivity.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = ROOT / "k8s/deployments/configs/foundry"
SCHEDULES = {"worlds": "0 * * * *", "modules": "30 * * * *", "base": "15 * * * *"}
RESOURCES = {"deployment.yaml", "backup.yaml", "backup-isolated.yaml"}
SECRET_FILES = ["secret.enc.yaml", "restic-secret.enc.yaml"] + [
    f"restic-{part}-secret.enc.yaml" for part in SCHEDULES
]
RECIPIENT = "age146ew5u7ufdeqf80ehh79hmcg93mpyttpzq52erdu769f6wm7xass0amenq"
PUBLIC_FIELDS = "^(apiVersion|metadata|kind|type)$"
RESTIC_POLICY = {
    "copyMethod": "Direct", "cacheStorageClassName": "nfs-client",
    "pruneIntervalDays": 7,
    "retain": {"hourly": 24, "daily": 30, "weekly": 26, "monthly": 12},
}
# Original bytes at origin/main dba2e9525e8b440a56d977189c7310805d57311f.
LEGACY_SECRET_SHA256 = "66f73c0d27d7724b4214d6480095b8a6ebd2ad018fd9577a14a8945e0c9d95ba"
SEALED = re.compile(
    r"ENC\[AES256_GCM,data:([A-Za-z0-9+/=]+),iv:([A-Za-z0-9+/=]+),"
    r"tag:([A-Za-z0-9+/=]+),type:str\]"
)


def run_local(argv, input_text=None):
    env = os.environ.copy()
    env["KUBECONFIG"] = os.devnull
    result = subprocess.run(
        argv, input=input_text, capture_output=True, text=True, check=False,
        cwd=ROOT, env=env, timeout=30,
    )
    if result.returncode:
        # Do not echo parser input or stderr that might quote malformed Secret data.
        raise RuntimeError(f"Offline {argv[0]} command failed ({result.returncode})")
    return result.stdout


def parse_yaml(text):
    return json.loads(run_local(
        ["ruby", "-ryaml", "-rjson", "-e",
         "puts JSON.generate(YAML.load_stream(STDIN.read))"], text,
    ))


class FoundryBackupIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        missing = [tool for tool in ("kubectl", "ruby") if not shutil.which(tool)]
        if missing:
            raise RuntimeError("Required local tools missing: " + ", ".join(missing))
        cls.kustomization, = parse_yaml((MANIFESTS / "kustomization.yaml").read_text())
        # Reject remote resources, plugins, transformers and other execution paths
        # before invoking kustomize. Render all actual declared non-secret resources.
        if set(cls.kustomization) != {"apiVersion", "kind", "resources", "generators"}:
            raise RuntimeError("Unexpected Foundry Kustomization fields; review offline rendering")
        resources = cls.kustomization["resources"]
        if len(resources) != len(RESOURCES) or set(resources) != RESOURCES:
            raise RuntimeError("Expected only local Foundry deployment and both backup resources")
        render_config = {k: v for k, v in cls.kustomization.items() if k != "generators"}
        with tempfile.TemporaryDirectory(
            prefix="foundry-backup-tests-", dir=os.environ.get("TMPDIR"),
        ) as directory:
            target = Path(directory)
            for name in resources:
                source = MANIFESTS / name
                if source.is_symlink():
                    raise RuntimeError("Offline resource must not be a symlink: " + name)
                shutil.copyfile(source, target / name)
            (target / "kustomization.yaml").write_text(json.dumps(render_config))
            cls.rendered = parse_yaml(run_local(["kubectl", "kustomize", directory]))
        cls.sources = [d for d in cls.rendered if d.get("kind") == "ReplicationSource"]

    def test_exact_rendered_inventory_and_no_alert_resources(self):
        expected = {
            ("v1", "PersistentVolumeClaim", f"foundry-{part}") for part in SCHEDULES
        } | {("v1", "Service", "foundry"), ("apps/v1", "Deployment", "foundry")} | {
            ("volsync.backube/v1alpha1", "ReplicationSource", f"foundry-{part}{suffix}")
            for part in SCHEDULES for suffix in ("-backup", "-isolated-backup")
        }
        actual = [(d["apiVersion"], d["kind"], d["metadata"]["name"]) for d in self.rendered]
        self.assertCountEqual(actual, expected)
        self.assertEqual(len(self.sources), 6)

    def test_legacy_sources_stay_paused_on_original_repository(self):
        for part, schedule in SCHEDULES.items():
            with self.subTest(part=part):
                source, = [d for d in self.sources if d["metadata"]["name"] == f"foundry-{part}-backup"]
                self.assertIs(source["spec"].get("paused"), True)
                self.assertEqual(source["spec"], {
                    "paused": True, "sourcePVC": f"foundry-{part}",
                    "trigger": {"schedule": schedule},
                    "restic": {"repository": "foundry-restic-secret", **RESTIC_POLICY},
                })

    def test_new_sources_are_paused_with_isolated_refs_and_original_policy(self):
        references = []
        for part, schedule in SCHEDULES.items():
            with self.subTest(part=part):
                source, = [d for d in self.sources if d["metadata"]["name"] == f"foundry-{part}-isolated-backup"]
                self.assertIs(source["spec"].get("paused"), True)
                self.assertEqual(source["spec"], {
                    "paused": True, "sourcePVC": f"foundry-{part}",
                    "trigger": {"schedule": schedule},
                    "restic": {"repository": f"foundry-{part}-restic-secret", **RESTIC_POLICY},
                })
                references.append(source["spec"]["restic"]["repository"])
        self.assertEqual(len(set(references)), 3)
        self.assertNotIn("foundry-restic-secret", references)

    def test_backup_files_contain_only_expected_sources_without_hooks(self):
        for name, suffix in (("backup.yaml", "-backup"), ("backup-isolated.yaml", "-isolated-backup")):
            with self.subTest(file=name):
                documents = parse_yaml((MANIFESTS / name).read_text())
                self.assertCountEqual(
                    [d["metadata"]["name"] for d in documents],
                    [f"foundry-{part}{suffix}" for part in SCHEDULES],
                )
                for doc in documents:
                    self.assertEqual(set(doc), {"apiVersion", "kind", "metadata", "spec"})
                    self.assertEqual(doc["kind"], "ReplicationSource")
                    self.assertEqual(set(doc["metadata"]), {"name"})
                    # Exact spec assertions above also forbid manual triggers,
                    # custom mover jobs/hooks, unlocks and extra prune commands.
                    self.assertIn(doc, self.sources)

    def test_kustomization_preserves_old_and_adds_exact_secret_generators(self):
        self.assertEqual(self.kustomization["apiVersion"], "kustomize.config.k8s.io/v1beta1")
        self.assertEqual(self.kustomization["kind"], "Kustomization")
        generator_text, = self.kustomization["generators"]
        generator, = parse_yaml(generator_text)
        self.assertEqual(set(generator), {"apiVersion", "kind", "metadata", "files"})
        self.assertEqual(generator["apiVersion"], "viaduct.ai/v1")
        self.assertEqual(generator["kind"], "ksops")
        self.assertEqual(generator["metadata"]["name"], "sops-generator")
        self.assertEqual(set(generator["metadata"]), {"name", "annotations"})
        annotations = generator["metadata"]["annotations"]
        self.assertEqual(set(annotations), {"config.kubernetes.io/function"})
        self.assertEqual(parse_yaml(annotations["config.kubernetes.io/function"]), [{"exec": {"path": "ksops"}}])
        self.assertCountEqual(generator["files"], SECRET_FILES)
        self.assertTrue(all((MANIFESTS / name).is_file() for name in generator["files"]))

    def test_original_encrypted_repository_is_byte_for_byte_preserved(self):
        self.assertEqual(
            hashlib.sha256((MANIFESTS / "restic-secret.enc.yaml").read_bytes()).hexdigest(),
            LEGACY_SECRET_SHA256,
        )

    def assert_sealed(self, value, label):
        # Boolean assertions deliberately avoid printing potentially plaintext values.
        match = SEALED.fullmatch(value) if isinstance(value, str) else None
        if match is None:
            self.fail(label + " must be SOPS ciphertext")
        try:
            data, iv, tag = [base64.b64decode(v, validate=True) for v in match.groups()]
        except ValueError:
            self.fail(label + " has invalid ciphertext encoding")
        self.assertTrue(bool(data), label + " ciphertext must be nonempty")
        self.assertEqual(len(iv), 32, label + " IV length")
        self.assertEqual(len(tag), 16, label + " tag length")

    def test_new_secrets_have_only_encrypted_data_and_correct_public_metadata(self):
        keys = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "RESTIC_PASSWORD", "RESTIC_REPOSITORY"}
        for part in SCHEDULES:
            with self.subTest(part=part):
                secret, = parse_yaml((MANIFESTS / f"restic-{part}-secret.enc.yaml").read_text())
                self.assertEqual(set(secret), {"apiVersion", "kind", "metadata", "type", "data", "sops"})
                self.assertEqual(secret["apiVersion"], "v1")
                self.assertEqual(secret["kind"], "Secret")
                self.assertEqual(secret["type"], "Opaque")
                self.assertEqual(secret["metadata"], {
                    "name": f"foundry-{part}-restic-secret", "namespace": "foundry",
                })
                self.assertEqual(set(secret["data"]), keys)
                self.assertNotIn("stringData", secret)
                for key, value in secret["data"].items():
                    self.assert_sealed(value, part + ":" + key)

    def test_sops_metadata_uses_real_project_recipient_without_alternate_keys(self):
        config, = parse_yaml((ROOT / ".sops.yaml").read_text())
        rule, = config["creation_rules"]
        self.assertEqual(rule["age"], RECIPIENT)
        self.assertEqual(rule["unencrypted_regex"], PUBLIC_FIELDS)
        for part in SCHEDULES:
            with self.subTest(part=part):
                secret, = parse_yaml((MANIFESTS / f"restic-{part}-secret.enc.yaml").read_text())
                sops = secret["sops"]
                self.assertLessEqual(set(sops), {
                    "age", "mac", "lastmodified", "unencrypted_regex", "version",
                    "kms", "gcp_kms", "azure_kv", "hc_vault", "pgp",
                })
                self.assertEqual(sops["unencrypted_regex"], PUBLIC_FIELDS)
                self.assert_sealed(sops["mac"], part + ":MAC")
                self.assertRegex(str(sops["version"]), r"^\d+\.\d+\.\d+$")
                self.assertRegex(sops["lastmodified"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
                for provider in ("kms", "gcp_kms", "azure_kv", "hc_vault", "pgp"):
                    self.assertFalse(sops.get(provider), "Unexpected alternative key provider")
                age, = sops["age"]
                self.assertEqual(set(age), {"recipient", "enc"})
                self.assertEqual(age["recipient"], RECIPIENT)
                armor = age["enc"].strip().splitlines()
                self.assertEqual(armor[0], "-----BEGIN AGE ENCRYPTED FILE-----")
                self.assertEqual(armor[-1], "-----END AGE ENCRYPTED FILE-----")
                payload = base64.b64decode("".join(armor[1:-1]), validate=True)
                self.assertTrue(payload.startswith(b"age-encryption.org/v1\n"))

    def test_existing_deployment_and_volume_mount_mapping_are_preserved(self):
        deployment, = [d for d in self.rendered if d["kind"] == "Deployment"]
        pod = deployment["spec"]["template"]["spec"]
        container, = pod["containers"]
        self.assertEqual(container["volumeMounts"], [
            {"name": "foundry-base", "mountPath": "/data"},
            {"name": "foundry-worlds", "mountPath": "/data/Data/worlds"},
            {"name": "foundry-modules", "mountPath": "/data/Data/modules", "subPath": "modules"},
            {"name": "foundry-modules", "mountPath": "/data/Data/systems", "subPath": "systems"},
        ])
        self.assertCountEqual(pod["volumes"], [
            {"name": f"foundry-{part}", "persistentVolumeClaim": {"claimName": f"foundry-{part}"}}
            for part in SCHEDULES
        ])


if __name__ == "__main__":
    unittest.main()
