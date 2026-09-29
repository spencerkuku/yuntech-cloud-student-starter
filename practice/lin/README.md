# W3 practice notes

## Goal

Deploy a single EC2 instance that runs the inspection prototype, then confirm the request path and the version contract.

## Request path to remember

Source IP -> route table -> default public subnet -> security group -> EC2 instance -> nginx -> inspection /health

## Required checks before launch

- Confirm the default VPC and public subnet.
- Confirm the route table includes 0.0.0.0/0 via IGW.
- Use AL2023 x86_64 AMI and t3.micro.
- Only allow ingress from the Codespace IPv4 on TCP 22 and 80.
- Use IMDSv2 required, EBS gp3, and DeleteOnTermination.

## Deployment flow

1. Generate or confirm the Codespace public IPv4 and convert it to a /32 CIDR.
2. Ship the exact commit via the user-data builder.
3. Create a dedicated security group and import the SSH public key.
4. Launch the instance with tags and the W3 deployment script.
5. Verify the service health endpoint and compare the version with the deployment commit.

## Failure flow to test

- Block inbound 80 using the security group, then restore exactly the same rule.
- Stop the inspection service while leaving nginx running.
- Stop nginx, confirm the HTTP refusal, and compare the symptoms.

## Cleanup flow

- Use the local resource ledger in .local/resources.json.
- Terminate the instance, then remove the security group and key pair.
- Validate that the tracked resources are gone before closing the lab.
