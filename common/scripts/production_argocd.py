"""Render one service's Argo CD Application and exclusions for the shared runtime."""

import argparse
import json

SERVICES = ("analytics", "assistant", "bank-bff", "bank-statement-data", "user")
REGISTRY = "ghcr.io/fin-assist"


def application(service):
    return {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
            "metadata": {"name": "production-" + service, "namespace": "argocd"},
            "spec": {"project": "production-releases",
                     "destination": {"server": "https://kubernetes.default.svc", "namespace": "plainfin-apps"},
                     "source": {"repoURL": REGISTRY, "chart": "production-" + service, "targetRevision": "1.*"},
                     "syncPolicy": {"automated": {"enabled": False, "prune": False, "selfHeal": False},
                                    "syncOptions": ["ServerSideApply=true", "FailOnSharedResource=true"]}}}


def project():
    return {"apiVersion": "argoproj.io/v1alpha1", "kind": "AppProject",
            "metadata": {"name": "production-releases", "namespace": "argocd"},
            "spec": {"sourceRepos": [REGISTRY], "destinations": [application("user")["spec"]["destination"]],
                     "clusterResourceBlacklist": [{"group": "*", "kind": "*"}],
                     "namespaceResourceWhitelist": [{"group": "batch", "kind": "Job"},
                                                    {"group": "argoproj.io", "kind": "Rollout"}]}}


def exclusions(service):
    # Kustomize removes the matching manifests during rendering. It does not issue Kubernetes DELETEs.
    targets = [("argoproj.io", "v1alpha1", "Rollout", service)]
    if service != "bank-bff":
        targets.append(("batch", "v1", "Job", service + "-migrate-[0-9a-f]{24}"))
    return [{"target": {"group": group, "version": version, "kind": kind, "name": name, "namespace": "plainfin-apps"},
             "patch": json.dumps({"apiVersion": group + "/" + version, "kind": kind,
                                  "metadata": {"name": service, "namespace": "plainfin-apps"}, "$patch": "delete"})}
            for group, version, kind, name in targets]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=SERVICES)
    parser.add_argument("output", choices=("application", "exclusions", "project"))
    args = parser.parse_args()
    print(json.dumps({"application": application(args.service), "exclusions": exclusions(args.service),
                      "project": project()}[args.output], indent=2))
