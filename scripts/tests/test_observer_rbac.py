"""Offline regression tests for the rendered observer's declared RBAC policy.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -v
Requires local kubectl (with kustomize) and Ruby's stdlib YAML/JSON; no PyYAML.
Only `kubectl kustomize` runs: no API server, live kubeconfig, auth checks, or
network are used. The small rule matcher is NOT Kubernetes authorization or an
assessment of effective privileges (especially credentials readable in Secrets).
Any request names and matcher fixtures below are explicitly synthetic.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = ROOT / "k8s/deployments/configs/navi-observer"
APPLICATION = ROOT / "k8s/deployments/apps/navi-observer.yaml"
IDENTITY = "navi-observer"
CREDENTIAL = "navi-observer-credential"
READ_VERBS = {"get", "list", "watch"}
RUBY_YAML_JSON = "puts JSON.generate(YAML.load_stream(STDIN.read))"


def run_local(argv, *, input_text=None):
    """Run only a caller-selected local renderer/parser, never cluster commands."""
    env = os.environ.copy()
    # Do not expose the user's kubeconfig to even this purely local command.
    env["KUBECONFIG"] = os.devnull
    try:
        result = subprocess.run(
            argv, input=input_text, capture_output=True, text=True, check=False,
            cwd=ROOT, env=env, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Offline command failed: {argv!r}: {exc}") from exc
    if result.returncode:
        raise RuntimeError(
            f"Offline command failed ({result.returncode}): {argv!r}\n{result.stderr}"
        )
    return result.stdout


def parse_yaml(text):
    return json.loads(run_local(
        ["ruby", "-ryaml", "-rjson", "-e", RUBY_YAML_JSON], input_text=text,
    ))


def declared_allows(rules, verb, group, resource, name=None):
    """Match this role's resource rules, not live/effective Kubernetes RBAC.

    The binding's cluster scope is asserted separately. `resource` includes any
    subresource suffix. Match wildcard grants too so forbidden-action checks do
    not accidentally overlook a broadened rule. No discovery/server is involved.
    """
    for rule in rules:
        resources = rule.get("resources", [])
        subresource_wildcard = (
            "*/" + resource.split("/", 1)[1] if "/" in resource else None
        )
        if not (verb in rule.get("verbs", []) or "*" in rule.get("verbs", [])):
            continue
        if not (group in rule.get("apiGroups", []) or "*" in rule.get("apiGroups", [])):
            continue
        if not (resource in resources or "*" in resources or
                (subresource_wildcard is not None and subresource_wildcard in resources)):
            continue
        if "resourceNames" in rule and name not in rule["resourceNames"]:
            continue
        return True
    return False


class ObserverRenderedRBACTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        missing = [tool for tool in ("kubectl", "ruby") if not shutil.which(tool)]
        if missing:
            raise RuntimeError(
                "Offline observer RBAC tests require local tools: " + ", ".join(missing)
                + "; install kubectl (with kustomize) and Ruby with stdlib YAML/JSON."
            )
        cls.documents = parse_yaml(run_local(["kubectl", "kustomize", str(MANIFESTS)]))
        cls.app_documents = parse_yaml(APPLICATION.read_text())

    def role(self):
        roles = [doc for doc in self.documents if doc.get("kind") == "ClusterRole"]
        self.assertEqual(len(roles), 1, "Expected one rendered diagnostic ClusterRole")
        self.assertTrue(roles[0].get("rules"), "ClusterRole must declare diagnostic rules")
        return roles[0]

    def test_exact_rendered_objects_scopes_and_binding(self):
        expected = {
            ("v1", "ServiceAccount", IDENTITY, IDENTITY),
            ("v1", "Secret", IDENTITY, CREDENTIAL),
            ("rbac.authorization.k8s.io/v1", "ClusterRole", None, IDENTITY),
            ("rbac.authorization.k8s.io/v1", "ClusterRoleBinding", None, IDENTITY),
        }
        self.assertEqual(len(self.documents), 4)
        actual = {
            (doc["apiVersion"], doc["kind"], doc["metadata"].get("namespace"),
             doc["metadata"]["name"]) for doc in self.documents
        }
        self.assertEqual(actual, expected)
        binding = next(doc for doc in self.documents if doc["kind"] == "ClusterRoleBinding")
        self.assertEqual(binding["roleRef"], {
            "apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": IDENTITY,
        })
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount", "name": IDENTITY, "namespace": IDENTITY,
        }])
        for doc in self.documents:
            if doc["kind"] in {"ClusterRole", "ClusterRoleBinding"}:
                self.assertNotIn("namespace", doc["metadata"])

    def test_identity_and_metadata_only_durable_credential(self):
        account = next(doc for doc in self.documents if doc["kind"] == "ServiceAccount")
        self.assertIs(account.get("automountServiceAccountToken"), False)
        self.assertFalse(account.get("secrets"), "Credential must not be referenced by the SA")
        secret = next(doc for doc in self.documents if doc["kind"] == "Secret")
        self.assertEqual(set(secret), {"apiVersion", "kind", "metadata", "type"})
        self.assertEqual(secret["type"], "kubernetes.io/service-account-token")
        annotations = secret["metadata"]["annotations"]
        self.assertEqual(annotations["kubernetes.io/service-account.name"], IDENTITY)
        self.assertEqual(annotations["argocd.argoproj.io/sync-wave"], "1")
        self.assertNotIn("kubernetes.io/service-account.uid", annotations)
        self.assertNotIn("data", secret)
        self.assertNotIn("stringData", secret)
        self.assertNotIn("uid", secret["metadata"])

    def test_rule_structure_has_only_reads_and_explicit_resource_names(self):
        log_rules = []
        # Optional health/status non-resource grants must stay exact and get-only.
        health_paths = {
            "/healthz", "/livez", "/readyz", "/version", "/metrics",
            "/healthz/ping", "/livez/ping", "/readyz/ping",
        }
        for rule in self.role()["rules"]:
            with self.subTest(rule=rule):
                self.assertTrue(rule.get("verbs"))
                self.assertLessEqual(set(rule["verbs"]), READ_VERBS)
                if "nonResourceURLs" in rule:
                    self.assertEqual(set(rule), {"nonResourceURLs", "verbs"})
                    self.assertEqual(rule["verbs"], ["get"])
                    self.assertTrue(rule["nonResourceURLs"])
                    self.assertLessEqual(set(rule["nonResourceURLs"]), health_paths)
                    continue
                self.assertLessEqual(set(rule), {"apiGroups", "resources", "verbs"})
                self.assertTrue(rule.get("apiGroups"))
                self.assertTrue(rule.get("resources"))
                for resource in rule["resources"]:
                    self.assertIsInstance(resource, str)
                    self.assertNotIn("*", resource)
                    if "/" in resource:
                        self.assertEqual(resource, "pods/log")
                        self.assertEqual(rule["apiGroups"], [""])
                        self.assertEqual(rule["resources"], ["pods/log"])
                        self.assertEqual(rule["verbs"], ["get"])
                        log_rules.append(rule)
                    else:
                        self.assertRegex(resource, r"^[a-z][a-z0-9]*$")
        self.assertTrue(log_rules, "Log reads must use a separate core pods/log get rule")

    def test_representative_diagnostic_reads_are_not_name_or_namespace_limited(self):
        rules = self.role()["rules"]
        # All names here are synthetic; namespace independence comes from the
        # asserted ClusterRoleBinding, not a mocked API authorization response.
        resources = [
            ("", "pods"), ("", "secrets"), ("", "configmaps"), ("", "nodes"),
            ("", "namespaces"), ("", "events"), ("", "serviceaccounts"),
            ("rbac.authorization.k8s.io", "roles"),
            ("rbac.authorization.k8s.io", "rolebindings"),
            ("rbac.authorization.k8s.io", "clusterroles"),
            ("rbac.authorization.k8s.io", "clusterrolebindings"),
            # These top-level reads include controller status fields; a separate
            # /status subresource grant would violate the declared boundary.
            ("volsync.backube", "replicationsources"),
            ("volsync.backube", "replicationdestinations"),
            ("snapshot.storage.k8s.io", "volumesnapshots"),
            ("snapshot.storage.k8s.io", "volumesnapshotcontents"),
            ("snapshot.storage.k8s.io", "volumesnapshotclasses"),
            ("", "persistentvolumes"), ("", "persistentvolumeclaims"),
            ("storage.k8s.io", "storageclasses"), ("storage.k8s.io", "volumeattachments"),
            ("batch", "jobs"), ("batch", "cronjobs"),
            ("argoproj.io", "applications"), ("argoproj.io", "applicationsets"),
            ("apiextensions.k8s.io", "customresourcedefinitions"),
            ("velero.io", "backups"), ("velero.io", "restores"),
            ("velero.io", "schedules"), ("velero.io", "backupstoragelocations"),
            ("velero.io", "volumesnapshotlocations"),
            ("metrics.k8s.io", "pods"), ("metrics.k8s.io", "nodes"),
        ]
        for group, resource in resources:
            for verb in sorted(READ_VERBS):
                for name in (None, "synthetic-diagnostic-object", "synthetic-other-app"):
                    with self.subTest(verb=verb, group=group, resource=resource, name=name):
                        self.assertTrue(declared_allows(rules, verb, group, resource, name))
        self.assertTrue(declared_allows(rules, "get", "", "pods/log", "synthetic-pod"))
        # Reading this real manifest identity's own credential is explicitly
        # approved; no live Secret or token is fetched by this test.
        self.assertTrue(declared_allows(rules, "get", "", "secrets", CREDENTIAL))

    def test_mutations_and_privilege_operations_are_not_declared(self):
        rules = self.role()["rules"]
        forbidden_verbs = {
            "create", "update", "patch", "delete", "deletecollection", "bind", "escalate",
            "impersonate", "approve", "sign", "use", "*",
        }
        # Exercise every actual resource/group grant, rather than only examples.
        for rule in rules:
            for resource in rule.get("resources", []):
                for group in rule["apiGroups"]:
                    for verb in sorted(forbidden_verbs):
                        with self.subTest(verb=verb, group=group, resource=resource):
                            self.assertFalse(declared_allows(rules, verb, group, resource))
        for verb, group, resource in [
            ("create", "", "serviceaccounts/token"),
            ("bind", "rbac.authorization.k8s.io", "clusterroles"),
            ("escalate", "rbac.authorization.k8s.io", "roles"),
            ("impersonate", "", "users"), ("impersonate", "", "groups"),
            ("impersonate", "", "serviceaccounts"),
        ]:
            with self.subTest(verb=verb, group=group, resource=resource):
                self.assertFalse(declared_allows(rules, verb, group, resource, "synthetic-target"))

    def test_interactive_proxy_and_other_subresources_are_not_declared(self):
        rules = self.role()["rules"]
        dangerous = [
            "pods/exec", "pods/attach", "pods/portforward", "pods/proxy",
            "services/proxy", "nodes/proxy", "serviceaccounts/token",
            "proxy", "*/exec", "*/attach", "*/portforward", "*/proxy",
            "pods/*", "services/*", "nodes/*", "*/*",
            "pods/status", "nodes/status", "pods/ephemeralcontainers",
            "pods/eviction", "pods/syntheticfuture",
        ]
        for resource in dangerous:
            for verb in ("get", "create", "list", "watch"):
                with self.subTest(verb=verb, resource=resource):
                    self.assertFalse(declared_allows(rules, verb, "", resource, "synthetic-target"))
        for verb in ("list", "watch", "create"):
            self.assertFalse(declared_allows(rules, verb, "", "pods/log"))
        for resource in ("replicationsources/status", "replicationdestinations/status"):
            self.assertFalse(declared_allows(rules, "get", "volsync.backube", resource))
        for verb in sorted(READ_VERBS):
            self.assertFalse(declared_allows(
                rules, verb, "synthetic-unlisted.example.invalid", "syntheticfuturewidgets",
            ))

    def test_argo_ignores_only_this_credentials_generated_data_and_uid(self):
        self.assertEqual(len(self.app_documents), 1)
        app = self.app_documents[0]
        self.assertEqual((app["apiVersion"], app["kind"]), ("argoproj.io/v1alpha1", "Application"))
        self.assertEqual(app["metadata"]["name"], IDENTITY)
        self.assertEqual(app["metadata"]["namespace"], "argocd")
        spec = app["spec"]
        self.assertEqual(spec["destination"]["namespace"], IDENTITY)
        self.assertEqual([source["path"] for source in spec["sources"]], [
            "k8s/deployments/configs/navi-observer",
        ])
        ignores = spec["ignoreDifferences"]
        self.assertEqual(len(ignores), 1)
        self.assertEqual(set(ignores[0]), {"group", "kind", "name", "namespace", "jsonPointers"})
        self.assertEqual({key: value for key, value in ignores[0].items() if key != "jsonPointers"}, {
            "group": "", "kind": "Secret", "name": CREDENTIAL, "namespace": IDENTITY,
        })
        self.assertCountEqual(ignores[0]["jsonPointers"], [
            "/data", "/metadata/annotations/kubernetes.io~1service-account.uid",
        ])
        options = spec["syncPolicy"]["syncOptions"]
        self.assertIn("RespectIgnoreDifferences=true", options)
        self.assertNotIn("RespectIgnoreDifferences=false", options)

    def test_static_matcher_catches_synthetic_wildcard_and_name_restrictions(self):
        # Synthetic policies only: these are neither real manifests nor API results.
        wildcard = [{"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}]
        self.assertTrue(declared_allows(wildcard, "create", "", "pods/exec"))
        subresource = [{"apiGroups": [""], "resources": ["*/proxy"], "verbs": ["get"]}]
        self.assertTrue(declared_allows(subresource, "get", "", "nodes/proxy"))
        self.assertFalse(declared_allows(subresource, "get", "", "nodes"))
        named = [{"apiGroups": ["argoproj.io"], "resources": ["applications"],
                  "verbs": ["get"], "resourceNames": ["synthetic-one-app"]}]
        self.assertTrue(declared_allows(named, "get", "argoproj.io", "applications", "synthetic-one-app"))
        self.assertFalse(declared_allows(named, "get", "argoproj.io", "applications", "synthetic-other-app"))
        self.assertFalse(declared_allows(named, "get", "argoproj.io", "applications"))


if __name__ == "__main__":
    unittest.main()
