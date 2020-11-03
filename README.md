# AWS Network Load Balancer Sidecar Container

This repository contains a Dockerfile and some Python Code that will create a small Python based daemon that will help ensure that your application properly handles an AWS Network Load Balancer in ECS.

Currently ECS will keep the task open for the entire deregistration delay, however there is not a way to "prematurely" stop the task in order to force a TCP RST to happen, gracefully handing over any active connections.

This sidecar monitors the NLB Target Group Target Health of the primary process in order to determine if the target is in the "draining" state. It will then wait the recommended 2 minutes before exiting. If the sidecar container is marked as "essential" in the ECS task definition this will result in a `SIGTERM` signal being sent to all other containers in the task.

Depending on how the primary application is configured, this allows the primary application to gracefully exit by using the `SIGTERM` signal and `stopTimeout` property. 

This Sidecar does require the following permissions added to the Task Role to function:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "ECSNLBSideCar",
            "Effect": "Allow",
            "Action": [
                "elasticloadbalancing:DescribeTargetHealth",
                "ecs:DescribeServices",
                "ecs:DescribeTasks"
            ],
            "Resource": "*"
        }
    ]
}
```

Note also that after initialisation (one `DescribeServices` and one `DescribeTasks`) startup each instance of this sidecar will perform a `DescribeTargetHealth` API call every 30 seconds. It is not recommended to run too many of these in parallel as the code has limited exponential backoff and retry logic.