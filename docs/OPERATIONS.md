# Operations, recovery, and safety boundaries

## Deployment scope

Deploy once per AWS account and region. All invocations and tools managing the same
resources must use **the same DynamoDB table and scope partition**. Reserved Lambda
concurrency is one, but the DynamoDB lock is what coordinates local executions and
other workers. Independent tables, accounts, and regions do not share budgets.
Do not deploy overlapping janitors with different tables.

The template's role has no workload write grants in dry-run mode. `Mode=stop` grants
only stop/scale/quarantine/disable operations. `Mode=lifecycle` adds EC2 termination.
The latter obeys `DeleteOnTermination` on attached EBS volumes; the plan lists those
volume IDs. The service never changes that setting or creates backups automatically.

For Scheduler, configure a dedicated `ManagedScheduleGroup` and an exact comma-separated
`SchedulerTargetRoleArns` allowlist. `UpdateSchedule` requires passing its existing
target role. Both application checks and `iam:PassRole` constrain the role. Schedules
have no individual tags, so group tags gate application decisions while IAM scopes
updates to the configured group's schedule ARNs. IAM cannot atomically recheck those
group tags during a schedule update. The janitor's own schedule group is explicitly
denied, even if configured accidentally.

Read APIs enumerate within the deployment account/region. Services without server-side
tag filtering require listing resources before inspecting tags. Discovery has a
resource bound and a deadline; an incomplete discovery performs **no workload actions**.
`ResourceTypes` reduces enumeration work. Start with smaller fleets. Large accounts
may need a future Step Functions/sharded planner rather than extending a Lambda loop.
When sharding, preserve one coordinator and one shared budget for overlapping scopes.

## Audit records

The partition key is `scope#ACCOUNT_ID#REGION`; the sort keys are:

| Sort key | Contents | Automatic expiration |
| --- | --- | --- |
| `lock` | Owning run ID and acquisition time | Never |
| `budget` | Operator window policy and charged action reservations | Never; old reservations pruned under lock |
| `resource#ID_OR_ARN` | First observation, stop/quarantine history, unresolved intent | Never |
| `run#RUN_ID` | Effective policy, start/end, status, counts, failure reason | 90 days by default |
| `plan#RUN_ID#ID_OR_ARN` | Decision, proposed action, recovery settings, intent and outcome | 90 days by default |

Bodies are JSON strings in the DynamoDB `body` attribute. Strong reads are used.
Every state transaction checks the current lock owner. Reports are individual items,
not a growing single item containing the whole fleet. Resource records and budgets
are retained even after report retention expires. Size checks fail closed.

A normal run goes from `discovering` to `executing` and then `complete`, `partial`,
or `failed`. Each resource goes from `planned` through `pending` to `submitted`,
`failed`, or `unknown`, or becomes `skipped`, `would_act`, `blocked`, or `not_attempted`.
Submitted operations may still be in progress. EKS update IDs and AWS request IDs
are recorded when returned. Query EKS update status or the resource's current state
to confirm completion. A later manual restore does not trigger immediate automatic
re-quarantine; the retained quarantine record requires deliberate renewal/reconciliation.

Dry-runs write audit records, first-observed times, and observations of existing stop
history. They do not call workload mutation APIs or consume the rolling budget.
`reserved` in a dry-run report is a preview count. It is not a claim that the current
IAM role has live-action permissions.

Summary metrics: `EphemeralJanitor/ActionsSubmitted` and `RunFailure`, dimension `Scope`.
Default alarms cover Lambda Errors and failure-queue backlog. Subscribe to SNS and
watch Lambda throttles/duration and DynamoDB throttles as fleet sizes grow. Notification
failure is recorded and raises a Lambda failure; it never replays workload actions.
There is no estimated-dollar-savings claim: pauses do not eliminate all associated costs.

## Stop history and recovery grace

`JanitorAllowTerminate=true` must be present before the janitor stop. An accepted
stop with a previous running state establishes history. A later observation of a
stopped instance starts the grace clock. Tag, launch-time, or attachment changes
invalidate stop authority; a changed stopped-state fingerprint resets the observation
clock. Unrelated already-stopped instances are never adopted for termination.

Pause and inspect an expired workload during its recovery window. To rescue it,
set `DoNotCleanup` or extend `ExpiresAt` **before** restarting. Restarts and extensions
observed by the janitor invalidate prior termination history. A polling service cannot
prove continuous state or detect every change that occurs and reverts between polls.
For stronger ownership requirements, integrate CloudTrail/state-change evidence before
allowing termination; this release does not promise atomic lifecycle ownership.

AWS SDK automatic retries are disabled. EC2 mutations are one instance at a time.
EC2 stop/terminate, ECS scaling, Lambda concurrency, and scheduling operations do not
share an ETag-like conditional write. Protection/eligibility can change after a read;
service IAM tag conditions are an additional check where supported. Do not grant
untrusted callers the ability to retag protected resources or alter operator policy.

## Failure recovery

A normal partial/failed run records all available outcomes and releases its lock.
An unknown action remains `pending` in the resource record, blocking that resource
from automatic replay. Other resources may run on the next timer subject to the
remaining budget. Storage failure or worker death retains the scope lock and the
last durable intent. Never interpret a missing final result as proof nothing happened.

1. Disable the schedule and stop manual invocations. Set Lambda reserved concurrency
   to zero for recovery if needed, but remember that this does not stop active workers.
2. Prove the old worker cannot resume. For Lambda, wait beyond the configured maximum
   execution duration and queued-event age; for local workers, stop them explicitly.
   Inspect the failure queue before re-enabling triggers. A lease timeout alone is
   not proof: the AWS workload APIs cannot fence a paused old worker.
3. Read the lock owner, its run and plan records, and each `resource` pending intent.
   Compare the recorded request/update IDs with CloudTrail, EKS update status, and
   current resource state. Treat transport timeouts as possibly successful.
4. Reconcile the resource record and plan outcome with a conditional DynamoDB
   transaction. Preserve first-seen time, original recovery settings, and every
   reservation still inside the action window. Clear a pending intent only after
   determining its outcome. Leave unresolved resources blocked.
5. Remove the lock **conditionally on its recorded owner** after reconciliation.
   Do not delete all state or the budget to unlock a run.
6. Run a dry-run, inspect its decisions, restore the Lambda concurrency setting,
   then re-enable the schedule. Do not blindly replay DLQ events.

Changing `MaxActionsPerWindow` or `WindowSeconds` causes live execution to refuse the
old budget policy. During a maintenance window, stop/prove all workers stopped, retain
reservations still relevant to the longer old/new window, and conditionally update
the budget policy. Never erase reservations merely to apply a less restrictive cap.
`MaxActionsPerRun` may be lowered independently.

## Restore a quarantined workload

First protect it with `DoNotCleanup` or a future `ExpiresAt`, inspect the recorded
`before` settings and its current configuration, then use the owning service's API:

| Resource | Restore operation |
| --- | --- |
| EC2 stopped instance | `aws ec2 start-instances --instance-ids INSTANCE_ID` |
| ECS service | `aws ecs update-service --cluster CLUSTER --service SERVICE --desired-count ORIGINAL_COUNT` |
| EKS managed node group | `aws eks update-nodegroup-config --cluster-name CLUSTER --nodegroup-name GROUP --scaling-config minSize=ORIGINAL_MIN,desiredSize=ORIGINAL_DESIRED` |
| Lambda with previous reserved limit | `aws lambda put-function-concurrency --function-name FUNCTION --reserved-concurrent-executions ORIGINAL_LIMIT` |
| Lambda previously without a limit (`null`) | `aws lambda delete-function-concurrency --function-name FUNCTION` |
| EventBridge scheduled rule | `aws events enable-rule --name RULE` |
| Scheduler schedule | Fetch its current `GetSchedule`, preserve every supported update field, then call `UpdateSchedule` with `State=ENABLED` |

Restoring capacity does not recover volatile state lost during shutdown. Review
queued Lambda events before unthrottling. Review EKS pending pods and your autoscaling
controller before restarting capacity. Terminated EC2 instances cannot be restarted.
The janitor role deliberately lacks restore permissions; use an operator role.
