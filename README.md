# AWS Network Load Balancer Sidecar Container

This repository contains a Dockerfile and some Python Code that will create a small Python based daemon that will help ensure that your application properly handles an AWS Network Load Balancer in ECS.

Currently ECS will keep the task open for the entire deregistration delay, however there is not a way to "prematurely" stop the task allowing for a gracefully handing over any active connections.

This sidecar monitors the NLB Target Group Target Health of the primary process in order to determine if the target is in the "draining" state. It will then wait the recommended 2 minutes before exiting (by default). If the sidecar container is marked as "essential" in the ECS task definition this will result in a `SIGTERM` signal being sent to all other containers in the task.

If you find that default is insufficient please configure the wait time with the `DEREGISTRATION_WAIT` environment variable.

Depending on how the primary application is configured, this allows the primary application to gracefully exit by using the `SIGTERM` signal and `stopTimeout` property. A graceful exit _should_ consist of:

- Completing the active transaction
- Sending a TCP RST or TCP FIN signal to close out the connection.

This sidecar requires the following minimal permissions added to the Task Role to function (further permission reduction may be possible but has not been tested):

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

Here is an example Task Definition with this in use:

```json
{
  "containerDefinitions": [
    {
      "name": "nginx",
      "image": "nginx",
      "portMappings": [
        {
          "containerPort": 80,
          "protocol": "tcp"
        }
      ],
      "essential": true,
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/web-application-example",
          "awslogs-region": "ap-southeast-2",
          "awslogs-stream-prefix": "ecs"
        }
      }
    },
    {
      "name": "sidecar",
      "image": "sidecar-image",
      "essential": true,
      "environment" : [
        {
          "name": "DEREGISTRATION_WAIT",
          "value": "120"
        }     
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/web-application-example",
          "awslogs-region": "ap-southeast-2",
          "awslogs-stream-prefix": "ecs"
        }
      }
    }
  ],
  "family": "web-application-example",
  "taskRoleArn": "arn:aws:iam::123456789012:role/ECSNLBSidecar",
  "executionRoleArn": "arn:aws:iam::123456789012:role/ecsTaskExecutionRole",
  "networkMode": "awsvpc",
  "requiresCompatibilities": [
    "FARGATE"
  ],
  "cpu": "512",
  "memory": "1024"
}

```

Note also that after initialisation (one `DescribeServices` and one `DescribeTasks`) startup each instance of this sidecar will perform a `DescribeTargetHealth` API call every 30 seconds. It is not recommended to run too many of these in parallel as the code has limited exponential backoff and retry logic.

This sidecar currently also relies on the following assumptions:

- Tasks are running in `awsvpc` network mode.
- Tasks have access to the ECS Metadata V4 API.

Please feel free to raise an issue or pull request.