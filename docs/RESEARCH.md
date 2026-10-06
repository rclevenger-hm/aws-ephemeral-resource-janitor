# Related services and design choices

Reviewed October 2026. These are patterns adopted in original code, not copied engines.
For organizations wanting hundreds of resource-specific policies and broad multi-account
orchestration, evaluating Cloud Custodian directly can be better than growing this service.

| Project | Existing capabilities reviewed | Applied here |
| --- | --- | --- |
| [Cloud Custodian](https://github.com/cloud-custodian/cloud-custodian) (Apache-2.0) | Declarative resource filters/actions, dry-run, serverless execution, reporting, `mark-for-op` delayed actions | Tag policy separate from execution; delayed EC2 termination; audit decisions; scheduled Lambda |
| [aws-nuke](https://github.com/ekristen/aws-nuke) (MIT) | Account blocklists, account/region configuration, explicit resource types, global filters | Mandatory expected account checked through STS; fixed deployment region; service allowlist; exclusions. No account-wide wipe behavior |
| [cloud-nuke](https://github.com/gruntwork-io/cloud-nuke) (MIT) | Inspection, region/type filters, include/exclude tags, expiry protection | Preview mode; explicit lifetime and protection tags; bounded selection. No telemetry or broad deletion engine |
| [Instance Scheduler on AWS](https://github.com/aws-solutions/instance-scheduler-on-aws) (Apache-2.0) | Scheduled Lambda-based EC2/RDS start/stop automation and deployment infrastructure | Serverless timer, durable configuration/history, reversible stop-first behavior. Recurring business-hours starts and RDS support are not implemented |
| [Kubernetes Descheduler](https://github.com/kubernetes-sigs/descheduler) (Apache-2.0) | Eviction plugins, CronJob deployment, label selectors, node-fit checks, PDB-aware eviction, limits | Optional official Helm chart with a conservative authored values file; separate from AWS resource cleanup |

Inspected project READMEs, [aws-nuke configuration](https://github.com/ekristen/aws-nuke/blob/main/docs/config.md),
[cloud-nuke configuration](https://github.com/gruntwork-io/cloud-nuke/blob/master/docs/configuration.md),
and Descheduler's [release-1.34 configuration](https://github.com/kubernetes-sigs/descheduler/blob/release-1.34/README.md)
and [Helm values](https://github.com/kubernetes-sigs/descheduler/blob/release-1.34/charts/descheduler/values.yaml).
The original `rebuy-de/aws-nuke` is archived; the `ekristen` repository was used for comparison.

## AWS-specific implementation choices

- [EC2 StopInstances](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_StopInstances.html)
  and [TerminateInstances](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_TerminateInstances.html)
  do not expose an ETag/If-Match condition. Revalidation, scoped IAM, and explicit race
  limitations replace the OCI service's conditional mutations.
- [EKS scaling configuration](https://docs.aws.amazon.com/eks/latest/userguide/update-managed-node-group.html)
  changes do not respect PodDisruptionBudgets. This requires explicit disruption opt-in;
  it is never described as a PDB-safe Kubernetes drain.
- [Lambda concurrency](https://docs.aws.amazon.com/lambda/latest/api/API_PutFunctionConcurrency.html)
  supports zero to block new execution without deleting functions.
- [UpdateSchedule](https://docs.aws.amazon.com/scheduler/latest/APIReference/API_UpdateSchedule.html)
  resets omitted optional fields. The adapter carries every field supported by its SDK
  from a fresh GetSchedule response, modifies only state/token, and tests preservation.
- [Scheduler delivery](https://docs.aws.amazon.com/lambda/latest/dg/with-eventbridge-scheduler.html)
  and [Lambda asynchronous failures](https://docs.aws.amazon.com/lambda/latest/dg/invocation-async-error-handling.html)
  are separate failure paths; both are configured explicitly.

## Next extensions

Dependency-aware teardown of whole environments; owner-specific advance notifications;
CloudTrail-backed lifecycle ownership; a guarded restore API; approval workflows for
irreversible deletion; Step Functions orchestration for fleets exceeding Lambda runtime;
EBS/EIP/RDS adapters with their own retention and dependency checks. None is silently
approximated by deleting EKS/ECS backing instances directly.
