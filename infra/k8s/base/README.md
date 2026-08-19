# Kubernetes manifests

`base/` is a complete, valid deployment on its own — plain YAML, no templating.
Overlays patch it:

    infra/k8s/overlays/kind   local cluster: in-cluster Postgres and Redis,
                              NodePort, images loaded from the host daemon
    infra/k8s/overlays/gcp    GKE: Memorystore for Redis, secrets from GCP
                              Secret Manager, managed certificate, HPA

Render and inspect without a cluster:

    kubectl kustomize infra/k8s/overlays/kind
    make k8s-validate      # kubeconform against the real API schemas

Why Kustomize rather than Helm: see docs/adr/0011-deployment-topology.md.
