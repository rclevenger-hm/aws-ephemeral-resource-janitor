# Optional Kubernetes descheduler

The AWS janitor disables AWS schedules and pauses explicitly enrolled capacity.
Kubernetes Descheduler handles a different task: evicting selected pods so the
Kubernetes scheduler can place replacements on suitable nodes. It does not delete
Deployments, suspend CronJobs, remove clusters, or guarantee fewer billable nodes.

The authored `deploy/descheduler-values.yaml` uses the official upstream Helm chart.
It has dry-run enabled, a 15-minute CronJob, no overlapping runs, and limits of one
pod per node, two per namespace, and three total per cycle. Only pods labeled
`janitor-managed=true` qualify. Node-fit and minimum-replica checks apply. PVC pods,
pods without PDBs, system/daemon/local-storage pods, and resource-claim pods remain
protected. System namespaces are excluded. Prefer-no-eviction annotations are mandatory.

It enables only node-affinity and node-taint violation strategies. These can move
misplaced workloads; they are not TTL deletion or a scale-to-zero workflow. Kubernetes
PDB enforcement applies to eviction; it cannot guarantee zero application disruption.

## Install and review

The example is pinned to chart **0.34.0** and its release-1.34 policy vocabulary.
Check the [upstream compatibility guidance](https://github.com/kubernetes-sigs/descheduler/tree/release-1.34#compatibility)
against your cluster version before installation; select a matching supported chart
and review its release-specific schema when upgrading. Do not assume newest master
documentation describes an older installed chart.

```sh
helm repo add descheduler https://kubernetes-sigs.github.io/descheduler/
helm repo update
helm template ephemeral-descheduler descheduler/descheduler \
  --version 0.34.0 --namespace kube-system \
  --values deploy/descheduler-values.yaml > /tmp/descheduler-rendered.yaml
# Review the rendered CronJob, policy and RBAC before installing.
helm upgrade --install ephemeral-descheduler descheduler/descheduler \
  --version 0.34.0 --namespace kube-system \
  --values deploy/descheduler-values.yaml
```

Review job logs and proposed evictions. Establish PDBs for the enrolled workloads.
When ready, deliberately change `cmdOptions.dry-run` to `false` and upgrade the Helm
release. Install this only with a Kubernetes operator identity: the AWS janitor Lambda
has no Kubernetes credentials, cluster-admin RBAC, or private API endpoint access.

Keep the descheduler on persistent/system capacity. A descheduler running exclusively
on a node group that the AWS janitor scales to zero cannot continue operating.
Use a separate Kubernetes-aware drain process before EKS capacity shutdown when
PDB-preserving drain is required. AWS node-group desired-size changes bypass PDBs;
the descheduler does not change that AWS behavior.

To stop descheduling, set the chart's `suspend=true` or uninstall its Helm release.
No cluster changes are made by this repository until an operator installs it.
