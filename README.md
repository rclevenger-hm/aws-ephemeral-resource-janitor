# AWS Ephemeral Resource Janitor

A scheduled Lambda that retires explicitly enrolled temporary workloads. EventBridge
Scheduler runs it every 15 minutes. DynamoDB holds the plan, decisions, recovery
settings, lifecycle history, lock, and shared action budget.

**Dry-run is the deployment default.** The default Lambda role has no workload mutation
permissions. Turning on live execution requires an operator stack update.

## Supported resources

| Resource | Action after expiry | Additional safeguards |
| --- | --- | --- |
| EC2 EBS-backed, On-Demand instance | Graceful stop; optional later termination | Excludes ASG, Spot, Fleet-tagged, EKS/ECS/EMR-tagged and CloudFormation-tagged instances; honors API protection |
| ECS replica service (including Fargate) | Set desired count to zero | Requires Application Auto Scaling to be absent or fully suspended; excludes daemon services and active deployments |
| EKS managed node group | Set minimum and desired capacity to zero | Requires `JanitorAllowDisruption=true`; excludes node groups with Cluster Autoscaler discovery tags |
| Lambda function | Set reserved concurrency to zero | Excludes provisioned concurrency and the janitor itself; retains code/configuration |
| EventBridge scheduled rule, default bus | Disable rule | Excludes AWS-managed rules and rules without a schedule expression |
| EventBridge Scheduler schedule | Disable schedule, preserving its configuration | Only one explicitly configured group; group-level opt-in and expiry; exact allowlist of target roles |

EKS **control planes stay running and billable**. ECS standalone tasks, EKS Fargate
profiles, Karpenter capacity, and Lambda event-source mappings are not managed.
Non-EC2 resources are quarantined, not deleted. Stopped EC2 storage, IPs, load
balancers, and other surrounding resources can still incur charges.

An optional, separately installed [Kubernetes descheduler](docs/DESCHEDULER.md)
evicts eligible pods that violate node affinity or taints. It starts in dry-run mode,
respects PDBs, and only considers labeled pods. It is not a mechanism for pausing
an EKS control plane or scaling a cluster to zero.

## Enroll a resource

| Tag | Meaning |
| --- | --- |
| `JanitorManaged=true` | Required, exact lowercase opt-in |
| `DoNotCleanup` | Any value protects the resource; remove the tag to opt back in |
| `ExpiresAt=2026-12-01T18:00:00Z` | Absolute expiry with timezone; takes precedence over TTL |
| `TTLHours=24` | Positive, finite lifetime up to 87,600 hours; defaults to 24 |
| `Owner=team@example.com` | Appears in resource decisions; does not send owner email |
| `JanitorAllowTerminate=true` | Additional EC2 termination opt-in, required before the janitor stop |
| `JanitorAllowDisruption=true` | Additional EKS node-group scale-down acknowledgement |

TTL starts when this service first observes the enrolled resource, including in
dry runs. It does not guess creation time from EC2 `LaunchTime`. Use `ExpiresAt`
when provisioning for a precise lifetime. Invalid expiry/TTL excludes that resource.
Tags go on the ECS service, EKS **node group**, Lambda function, or EventBridge rule.
Scheduler only supports tags on **groups**: enrolling its configured group enrolls
all schedules inside it. Use a dedicated group for temporary schedules.

**EKS scale-down bypasses PodDisruptionBudgets.** Drain or otherwise prepare the
workloads and disable autoscaling before setting `JanitorAllowDisruption=true`.
Lambda concurrency zero blocks new executions; existing invocations can finish,
queues can accumulate, and asynchronous events can expire. Disable producers or
enroll their schedules as appropriate. Trigger disabling runs before capacity
quarantine, but the janitor does not infer producer/consumer dependencies.

## Deploy

Requirements: Python 3.12+, AWS credentials for the intended account, AWS CLI, and
[AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html).

```sh
git clone https://github.com/rclevenger-hm/aws-ephemeral-resource-janitor.git
cd aws-ephemeral-resource-janitor
sam build
sam deploy --guided
```

Choose the account/region deliberately. Keep `DryRun=true` and `Mode=stop` initially.
The template binds the policy and IAM resource ARNs to that deployment account and
region. `ResourceTypes` selects which services to discover. `ManagedScheduleGroup`
and `SchedulerTargetRoleArns` are empty by default, leaving Scheduler cleanup off.
Subscribe your operations endpoint to the output `NotificationTopicArn`; the stack
creates no subscriptions automatically.

Review the DynamoDB decisions, then update the stack with `DryRun=false` to allow
stops/quarantine. `Mode=lifecycle` additionally grants EC2 termination permission.
Termination still requires the per-instance tag, a recorded janitor stop, a later
observed stopped state, and the configured `GraceHours` (24 by default).

Deployment creates Lambda, Scheduler, an encrypted DynamoDB table with point-in-time
recovery, CloudWatch logs/alarms, SNS, and an encrypted SQS failure queue. Scheduler
and Lambda retries are disabled; stable schedule-slot IDs still deduplicate duplicate
deliveries. A five-minute timeout and a 60-second execution reserve bound each run.

## Inspect or run locally

Use the same state table as the scheduled Lambda. Local credentials must have the
same scoped permissions; a separate state table would bypass coordination.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
# Edit examples/policy.json with your account, region, and deployed table name.
export JANITOR_POLICY="$(cat examples/policy.json)"
aws-janitor --dry-run
aws-janitor --run-id RUN_ID
aws-janitor --dry-run --resource-id arn:aws:lambda:us-east-1:123456789012:function:demo
```

Lambda request examples:

```json
{"dry_run": true, "max_actions_per_run": 2}
```

```json
{"resource_ids": ["i-0123456789abcdef0"]}
```

Requests may enable dry-run, reduce the action cap, or select a subset of discovered
resource IDs. They cannot change account, region, service coverage, tags, budget,
grace, mode, state table, IAM roles, or deployment dry-run settings. Unsupported
fields fail validation. An empty ID list means no resources.

## Safety and operations

- Save the complete discovery plan before any workload mutation.
- Refresh eligibility twice around service-specific protection checks.
- Commit intent, action reservation, and resource state in one DynamoDB transaction.
- Share a rolling budget across resource types, invocations, and workers in a scope.
  Failures and uncertain outcomes stay charged. Defaults: 10 actions/run and 10/hour.
- Continue after known resource-specific rejection; abort after authorization,
  discovery, or uncertain mutation failures. Preserve partial results.
- Never replay unresolved actions automatically. A storage failure or abrupt worker
  death retains a non-expiring lock for deliberate recovery.
- Use tag-based IAM where supported, account/region constraints, and explicit
  Scheduler group/role boundaries. The janitor does not grant itself tagging,
  resource creation, force-stop, protection-removal, or broad deletion access.

AWS APIs used here do not offer a common conditional mutation primitive. Fresh reads
and tag-based IAM reduce races but do not create an atomic eligibility-and-action
transaction. See [safety model and limitations](docs/OPERATIONS.md), including
Scheduler's group-tag limitation, eventual consistency, and overlapping deployments.

`submitted` means the AWS API accepted the request, not that shutdown completed.
Reports explain every skipped resource. CloudWatch receives structured run summaries
and `EphemeralJanitor` metrics; SNS receives action/failure summaries and alarms.
Lambda failures raise exceptions so its asynchronous failure destination works.
The failure queue covers both schedule delivery failures and Lambda execution failures.

## Development and design references

```sh
pip install -e '.[test]'
pytest -q
ruff check .
ruff format --check .
cfn-lint template.yaml
```

Tests use Moto for real DynamoDB transaction/condition behavior and Botocore Stubber
for AWS request/response contracts. They make no live AWS mutations.

[Research notes](docs/RESEARCH.md) compare Cloud Custodian, aws-nuke, cloud-nuke,
Instance Scheduler on AWS, and Kubernetes Descheduler, with the patterns adopted
here and the differences in purpose. This is an original implementation; it does
not embed the destructive account-cleaning engines from those projects.
