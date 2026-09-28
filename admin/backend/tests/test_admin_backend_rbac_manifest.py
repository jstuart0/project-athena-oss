"""ATHENA-118 Phase 2: T10 -- RBAC manifest drift guard.

Plan: .mozart/plans/active/2026-09-27-deliver-athena-service-control-k8s.md
Test contract: same directory,
2026-09-27-deliver-athena-service-control-k8s.test-contract.md, T10.

Placement note: the plan text suggested `admin/backend/tests/unit/`, but
every other Phase 1/2 test file in this campaign lives directly under
`admin/backend/tests/` (no `unit/` subdirectory exists in this repo) --
placed here for consistency (jackson, confirming per the plan's own
caveat).

This is a drift guard: the expected resourceNames set is computed HERE, at
test time, by parsing `manifests/athena-prod/*.yaml` -- never transcribed
as a frozen list -- so it self-corrects as RAG services are added and
fails loudly if the tracked manifest falls out of sync.
"""
import glob
import os

import yaml

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
_MANIFESTS_DIR = os.path.join(_REPO_ROOT, "manifests", "athena-prod")
_RBAC_PATH = os.path.join(_MANIFESTS_DIR, "optional", "admin-backend-rbac.yaml")
_PATCH_PATH = os.path.join(_MANIFESTS_DIR, "optional", "admin-backend-k8s-control.patch.yaml")
_BASE_ADMIN_BACKEND_PATH = os.path.join(_MANIFESTS_DIR, "admin-backend.yaml")

PROTECTED_DEPLOYMENTS = {"athena-admin-backend", "athena-admin-frontend"}


def _generate_expected_resource_names():
    """D7's generator: non-recursive glob over manifests/athena-prod/*.yaml
    (so optional/cloudflare-tunnels.yaml, namespace `cloudflare`, and any
    other optional/ file is excluded by construction, not by a namespace
    filter alone), kind==Deployment, namespace==athena-prod, minus
    PROTECTED_DEPLOYMENTS."""
    names = set()
    for path in sorted(glob.glob(os.path.join(_MANIFESTS_DIR, "*.yaml"))):
        with open(path) as f:
            for doc in yaml.safe_load_all(f):
                if not doc:
                    continue
                if doc.get("kind") == "Deployment" and doc.get("metadata", {}).get("namespace") == "athena-prod":
                    names.add(doc["metadata"]["name"])
    return names - PROTECTED_DEPLOYMENTS


def _load_rbac_docs():
    with open(_RBAC_PATH) as f:
        return [doc for doc in yaml.safe_load_all(f) if doc]


def test_generator_excludes_cloudflare_and_the_job_by_name():
    names_before_protected_subtraction = set()
    for path in sorted(glob.glob(os.path.join(_MANIFESTS_DIR, "*.yaml"))):
        with open(path) as f:
            for doc in yaml.safe_load_all(f):
                if doc and doc.get("kind") == "Deployment":
                    names_before_protected_subtraction.add(doc["metadata"]["name"])
    assert "athena-tunnel" not in names_before_protected_subtraction
    assert "chat-tunnel" not in names_before_protected_subtraction
    assert "ollama-model-pull" not in names_before_protected_subtraction  # kind: Job, not Deployment


def test_expected_resource_names_floor_and_named_members():
    expected = _generate_expected_resource_names()
    assert len(expected) >= 30
    assert "athena-rag-tesla" in expected
    assert "ollama" in expected
    assert "athena-admin-backend" not in expected
    assert "athena-admin-frontend" not in expected


def test_role_has_exactly_two_rules_correctly_shaped():
    docs = _load_rbac_docs()
    role = next(d for d in docs if d.get("kind") == "Role")
    rules = role["rules"]
    assert len(rules) == 2

    deployments_rule = next(r for r in rules if r["resources"] == ["deployments"])
    scale_rule = next(r for r in rules if r["resources"] == ["deployments/scale"])

    # codex diff review r1 Medium #6: `get` on the base `deployments`
    # resource is unused -- inventory only ever calls list_deployments()
    # (the `list` verb), and per-Deployment reads/patches go through the
    # `deployments/scale` subresource, which carries its own get/patch.
    assert set(deployments_rule["verbs"]) == {"list"}
    assert "resourceNames" not in deployments_rule  # can't restrict list; not attempted

    assert set(scale_rule["verbs"]) == {"get", "patch"}

    for rule in rules:
        assert rule["apiGroups"] == ["apps"]
        assert "*" not in rule.get("verbs", [])
        assert "*" not in rule.get("resources", [])
        for forbidden_verb in ("update", "delete", "create", "escalate", "bind"):
            assert forbidden_verb not in rule.get("verbs", [])


def test_resource_names_set_equal_to_generator_output():
    docs = _load_rbac_docs()
    role = next(d for d in docs if d.get("kind") == "Role")
    scale_rule = next(r for r in role["rules"] if r["resources"] == ["deployments/scale"])
    manifest_names = set(scale_rule["resourceNames"])

    expected = _generate_expected_resource_names()
    assert manifest_names == expected


def test_role_binding_subject_is_exactly_the_admin_backend_sa():
    docs = _load_rbac_docs()
    binding = next(d for d in docs if d.get("kind") == "RoleBinding")
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "athena-admin-backend", "namespace": "athena-prod"}
    ]


def test_base_admin_backend_manifest_still_has_automount_false():
    with open(_BASE_ADMIN_BACKEND_PATH) as f:
        docs = [d for d in yaml.safe_load_all(f) if d]
    deployment = next(d for d in docs if d.get("kind") == "Deployment")
    pod_spec = deployment["spec"]["template"]["spec"]
    assert pod_spec["serviceAccountName"] == "athena-admin-backend"
    assert pod_spec["automountServiceAccountToken"] is False


def test_patch_file_sets_sa_and_automount_true():
    with open(_PATCH_PATH) as f:
        patch = yaml.safe_load(f)
    pod_spec = patch["spec"]["template"]["spec"]
    assert pod_spec["serviceAccountName"] == "athena-admin-backend"
    assert pod_spec["automountServiceAccountToken"] is True
