# AWS Network Load Balancer Sidecar Container for ECS

This repository contains a Dockerfile and some Python Code that will create a small Python based daemon that will help ensure that your application properly handles an AWS Network Load Balancer in ECS.

You can grab this image under the ECR Public Repository at `public.ecr.aws/x3l4a9v5/ecsnlbsidecar:latest`

Currently ECS will keep the task open for the entire deregistration delay, however there is not a way to "prematurely" stop the task allowing for a gracefully handing over any active connections.

This sidecar monitors the NLB Target Group Target Health of the primary process in order to determine if the target is in the "draining" state. It will then wait the recommended 120 seconds before exiting (by default). If the sidecar container is marked as "essential" in the ECS task definition this will result in a `SIGTERM` signal being sent by default to all other containers in the task.

If you find that default is insufficient please configure the wait time with the `DEREGISTRATION_WAIT` environment variable.

Depending on how the primary application is configured, this allows the primary application to gracefully exit by using the `SIGTERM` signal and `stopTimeout` property. A graceful exit _should_ consist of:

- Completing the active transaction
- Sending a TCP RST or TCP FIN signal to close out the connection.

If your application is configured to achieve this graceful exit condition on a signal other than `SIGTERM` it is recommend you build your image with a modified [`STOPSIGNAL`](https://docs.docker.com/engine/reference/builder/#stopsignal).

For example the `library/nginx` image uses `SIGTERM` by default but a `SIGQUIT` signal can be used provided that you are not using UNIX sockets or a version prior to 1.19.1 (as per [defect #753](https://trac.nginx.org/nginx/ticket/753) which was merged into [1.19.1 of ngnix](https://trac.nginx.org/nginx/browser/nginx/src/os/unix/ngx_process_cycle.c?rev=062920e2f3bf871ef7a3d8496edec1b3065faf80)) which will "gracefully exit" existing connections. Therefore, you may want to build your own nginx with a modified `STOPSIGNAL` or wait for [docker-nginx/pull/457](https://github.com/nginxinc/docker-nginx/pull/457) to be merged and actioned as ECS does not support runtime modification of the stop signal:

```
FROM nginx:1.19.4
STOPSIGNAL SIGQUIT
```

The specific signalling behaviour you will need to use depends on the particular application stack you are using. Please spend time familiarising yourself with the signal handling behaviour of your chosen stack and ensure you are correctly signalling.

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

Here is an example Task Definition with this in use compatible with Fargate:

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
      "image": "public.ecr.aws/x3l4a9v5/ecsnlbsidecar:latest",
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

Note also that after initialisation (one `DescribeServices` and one `DescribeTasks`) startup each instance of this sidecar will perform a `DescribeTargetHealth` API call every 30 seconds. It is not recommended to run too many of these in parallel as the code has limited exponential backoff and retry logic. Failure for the API call to be called currently will result in premature termination of the sidecar.

This sidecar currently also relies on the following assumptions:

- Tasks are running in `awsvpc` network mode.
- Tasks have access to the ECS Metadata V4 API.

Please feel free to raise an issue or pull request.