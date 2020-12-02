"""
AWS Network Load Balancer Sidecar Container for ECS

Copyright 2020 Taylor Bertie

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""

import time
import os
import sys
import signal
import logging
import daemon
from enum import Enum
import requests
import boto3
import botocore
from awsretry import AWSRetry

logging.basicConfig(stream=sys.stdout, level=logging.INFO)


class Errors(Enum):
    """
    Enum representing the different error types that may be encountered
    """
    UNKNOWN = 0
    METADATA = 1
    CONTEXT = 2
    AWS_ACCESS = 3


class sideCarApp:
    """
    Class containing the sidecar application

    ...

    Attributes
    ----------
    deregistration_wait : int
        how long to wait after the NLB is in draining state before exiting
    metadata : dict
        metadata extracted from the ECS container metadata V4 URI
    network_type : str
        network type the ECS task is running on (currently only awsvpc is supported)
    network_addr : str
        IP address of the network interface
    network_mac : str
        MAC address of the network interface
    task_arn : str
        ARN of the ECS Task
    cluster : str
        Cluster the ECS Task is a member of
    client_ecs : boto3.client
        ECS Client to interact with ECS API
    client_elb : boto3.client
        ELBv2 Client to interact with the ELBv2 API
    service_name : str
        Service Name the ECS Task is a member of
    service : str
        ARN of the ECS Service the ECS Task is a member of
    load_balancers : dict
        Dictionary containing the attached Load Balancers to the ECS Service
    context : daemon.DaemonContext
        Context to setup the daemon process.
    """

    def __init__(self):
        # Grab Deregistration Wait Time from Environment Variables
        if not (deregistration_wait := os.getenv('DEREGISTRATION_WAIT', '120')).isnumeric():
            logging.warning('DEREGISTRATION_WAIT was not a numeric value: %s' % deregistration_wait)
            deregistration_wait = 120
        self.deregistration_wait = int(deregistration_wait)
        logging.info('Deregistration wait configured to %i seconds' % self.deregistration_wait)

        # Validate Required Environment Variable and get Metadata
        if (ECS_CONTAINER_METADATA_URI_V4 := os.getenv('ECS_CONTAINER_METADATA_URI_V4')) is None:
            self.error(Errors.METADATA, "Environment Variable ECS_CONTAINER_METADATA_URI_V4 not set", fatal=True)
        try:
            r = requests.get(ECS_CONTAINER_METADATA_URI_V4 + '/task')
            self.metadata = r.json()

            # Assert Assumptions from Metadata and gather data
            ## Assumption 1: Running in awsvpc mode
            if 'awsvpc' != (network_type := self.metadata['Containers'][0]['Networks'][0]['NetworkMode']):
                self.error(Errors.CONTEXT, "Task is not running in 'awsvpc' mode", fatal=True)
            self.network_type = network_type

            ## Assumption 2: Only one IPv4 attached to network
            if len(self.metadata['Containers'][0]['Networks'][0]['IPv4Addresses']) != 1:
                self.error(Errors.CONTEXT, "Task has more than one IPv4 address", fatal=True)
            self.network_addr = self.metadata['Containers'][0]['Networks'][0]['IPv4Addresses'][0]
            self.network_mac = self.metadata['Containers'][0]['Networks'][0]['MACAddress']
            self.task_arn = self.metadata['TaskARN']
            self.cluster = self.metadata['Cluster']

        except Exception as e:
            self.error(Errors.METADATA, str(e), fatal=True)

        logging.info('Determined IP address to be %s' % self.network_addr)
        logging.info('Determined TaskARN to be %s' % self.task_arn)

        # Setup Needed Clients
        self.client_ecs = boto3.client('ecs')
        self.client_elb = boto3.client('elbv2')

        # Attempt to find out service details
        try:
            logging.debug('Attempting to Describe Task in order to find out Task Group for Service Information')
            r = self.client_ecs.describe_tasks(cluster=self.cluster, tasks=[self.task_arn])
            task_group = r['tasks'][0]['group']
            if not task_group.startswith('service:'):
                self.error(Errors.CONTEXT, "Task is not in a service, task group: %s" % r['tasks'][0]['group'],
                           fatal=True)
            self.service_name = task_group.split(':', 1)[1]

            logging.debug('Attempting to Describe Service %s in order to get TargetGroupArn information'
                          % self.service_name)
            r = self.client_ecs.describe_services(cluster=self.cluster, services=[self.service_name])
            self.service = r['services'][0]['serviceArn']
            self.load_balancers = r['services'][0]['loadBalancers']

        except Exception as e:
            self.error(Errors.AWS_ACCESS, str(e), fatal=True)

        logging.info('Determined Service to be %s' % self.service)

        # Validate Access
        count = 0
        try:
            for lb in self.load_balancers:
                if 'targetGroupArn' in lb:
                    count += 1
                    logging.info('Found TG to check: %s' % lb['targetGroupArn'])
                    r = self.check_health(lb['targetGroupArn'], self.network_addr, lb['containerPort'])
                    logging.info('Target %s had initial status %s' % (self.network_addr, r['State']))
            if count == 0:
                self.error(Errors.CONTEXT, "No NLB/ALBs attached", fatal=True)
        except Exception as e:
            self.error(Errors.AWS_ACCESS, str(e), fatal=True)

        logging.info('Found %d Load Balancers attached' % count)

        # Important: run with detach_process=False as running inside a container
        self.context = daemon.DaemonContext(
            detach_process=False,
            stdout=sys.stdout,
            stderr=sys.stderr,
            signal_map={
                signal.SIGTERM: self.shutdown,
            })

    @AWSRetry.backoff(tries=10, delay=2, backoff=1.5)
    def check_health(self, target_group_arn: str, network_addr: str, port: int = 80):
        """
         Checks the health of a given target IP and port in a Target Group

         :param target_group_arn: Target Group ARN to query
         :param network_addr: IP address of the target to query
         :param port: Port of the target to query
         :return: Health status of the Target
         """
        logging.debug('Attempting DescribeTargetHealth with %s ; %s ; %s' % (target_group_arn, network_addr, port))
        try:
            r = self.client_elb.describe_target_health(TargetGroupArn=target_group_arn, Targets=[
                {
                    'Id': network_addr,
                    'Port': port
                }
            ])
        except botocore.exceptions.ClientError as e:
            raise e
        return r['TargetHealthDescriptions'][0]['TargetHealth']

    def run(self):
        """
        Runs the daemon process which checks the health of the targets for this task every 30 seconds and calls drain()
        when a target goes into 'draining"
        """
        logging.info('Initialization Complete, starting daemon')
        with self.context:
            logging.info('Daemon started')
            while True:
                # Attempt to check every 30 seconds
                time.sleep(30)
                for lb in self.load_balancers:
                    logging.info('Checking Target Health')
                    if 'targetGroupArn' in lb:
                        r = self.check_health(lb['targetGroupArn'], self.network_addr, lb['containerPort'])
                        if r['State'] == 'draining':
                            logging.info('Determined that %s target %s is in state %s, exiting after %i seconds' %
                                         (lb['targetGroupArn'], self.network_addr, r['State'],
                                          self.deregistration_wait))
                            self.drain()

    def drain(self):
        """
        Waits the deregistration_wait time (in seconds) and then calls shutdown()
        """
        # Wait DEREGISTRATION seconds for NLB workflow timeout
        time.sleep(self.deregistration_wait)
        # If task is marked as essential this should send a SIGTERM to compliment task.
        self.shutdown()

    def error(self, error: Errors, message: str, fatal: bool = False):
        """
        Prints an error message to the log.

        :param error: Error type
        :param message: Message to be printed
        :param fatal: Determines if function should call shutdown after this error
        """
        if error == Errors.METADATA:
            logging.error('Error import ECS Metadata: %s' % message)

        elif error == Errors.CONTEXT:
            logging.error('Task context incorrect: %s' % message)

        elif error == Errors.AWS_ACCESS:
            logging.error('Unable to access AWS API: %s' % message)

        else:
            logging.error('Unknown Error: %s' % message)

        if fatal:
            logging.fatal("Previous error was a fatal error, attempting to exit process cleanly")
            self.shutdown(clean=False)

    def shutdown(self, clean: bool = True):
        """
        Exits the application/daemon

        :param clean: Determines the exit code.
        """
        logging.debug('Closing out task %s' % self.task_arn)
        if not clean:
            logging.error('Detected unclean exit, exit(1)')
            sys.exit(1)
        else:
            logging.info('Detected clean exit, exit(0)')
            sys.exit(0)


app = sideCarApp()
app.run()
