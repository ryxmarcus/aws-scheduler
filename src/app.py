import json
import boto3
import logging
import datetime
from typing import Dict, List, Any

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Clients
tagging_client = boto3.client('resourcegroupstaggingapi')
ec2_client = boto3.client('ec2')
rds_client = boto3.client('rds')
asg_client = boto3.client('autoscaling')

def get_tag_filters(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convert JSON body tags into Resource Groups Tagging API filters."""
    tags = body.get('tags', {})
    filters = []
    for key, value in tags.items():
        filters.append({
            'Key': key,
            'Values': [value] if isinstance(value, str) else value
        })
    return filters

def handle_ec2(instance_id: str, action: str):
    """Start or Stop EC2 instances."""
    try:
        if action == 'start':
            ec2_client.start_instances(InstanceIds=[instance_id])
            status = 'Started'
        else:
            ec2_client.stop_instances(InstanceIds=[instance_id])
            status = 'Stopped'
        
        # Update Tags
        ec2_client.create_tags(
            Resources=[instance_id],
            Tags=[
                {'Key': 'LastAction', 'Value': status},
                {'Key': 'LastActionTime', 'Value': datetime.datetime.utcnow().isoformat()}
            ]
        )
        return True, status
    except Exception as e:
        logger.error(f"Error handling EC2 {instance_id}: {str(e)}")
        return False, str(e)

def handle_rds(db_id: str, arn: str, action: str):
    """Start or Stop RDS instances."""
    try:
        if action == 'start':
            rds_client.start_db_instance(DBInstanceIdentifier=db_id)
            status = 'Started'
        else:
            rds_client.stop_db_instance(DBInstanceIdentifier=db_id)
            status = 'Stopped'
        
        # Update Tags
        rds_client.add_tags_to_resource(
            ResourceName=arn,
            Tags=[
                {'Key': 'LastAction', 'Value': status},
                {'Key': 'LastActionTime', 'Value': datetime.datetime.utcnow().isoformat()}
            ]
        )
        return True, status
    except Exception as e:
        logger.error(f"Error handling RDS {db_id}: {str(e)}")
        return False, str(e)

def handle_asg(asg_name: str, action: str):
    """Update Auto Scaling Group capacity (0 for stop, 1 for start)."""
    try:
        if action == 'start':
            asg_client.update_auto_scaling_group(
                AutoScalingGroupName=asg_name,
                MinSize=1,
                DesiredCapacity=1
            )
            status = 'Started (Capacity 1)'
        else:
            asg_client.update_auto_scaling_group(
                AutoScalingGroupName=asg_name,
                MinSize=0,
                DesiredCapacity=0
            )
            status = 'Stopped (Capacity 0)'
        
        # Update Tags
        asg_client.create_or_update_tags(
            Tags=[
                {
                    'ResourceId': asg_name,
                    'ResourceType': 'auto-scaling-group',
                    'Key': 'LastAction',
                    'Value': status,
                    'PropagateAtLaunch': True
                },
                {
                    'ResourceId': asg_name,
                    'ResourceType': 'auto-scaling-group',
                    'Key': 'LastActionTime',
                    'Value': datetime.datetime.utcnow().isoformat(),
                    'PropagateAtLaunch': True
                }
            ]
        )
        return True, status
    except Exception as e:
        logger.error(f"Error handling ASG {asg_name}: {str(e)}")
        return False, str(e)

def handle_tag(id: str, arn: str, key: str, value: str):
    """Apply a tag to a resource."""
    try:
        if id.startswith('i-'):
            ec2_client.create_tags(Resources=[id], Tags=[{'Key': key, 'Value': value}])
        elif ':rds:' in arn:
            rds_client.add_tags_to_resource(ResourceName=arn, Tags=[{'Key': key, 'Value': value}])
        elif ':autoscaling:' in arn:
            asg_client.create_or_update_tags(
                Tags=[{
                    'ResourceId': id,
                    'ResourceType': 'auto-scaling-group',
                    'Key': key,
                    'Value': value,
                    'PropagateAtLaunch': True
                }]
            )
        else:
            return False, "Unknown resource type for tagging"
        return True, f"Tagged {key}={value}"
    except Exception as e:
        logger.error(f"Error tagging {id}: {str(e)}")
        return False, str(e)

def lambda_handler(event, context):
    logger.info(f"Event: {json.dumps(event)}")
    
    path_params = event.get('pathParameters', {})
    direct_id = path_params.get('id')
    direct_action = path_params.get('action')
    tag_key = path_params.get('key')
    tag_value = path_params.get('value')

    if direct_id:
        # Determine ARN and handle based on ID
        # EC2 prefix check
        if direct_id.startswith('i-'):
            arn = f"arn:aws:ec2:*:*:instance/{direct_id}"
            resource_type = 'ec2'
        else:
            # Check if it is RDS
            try:
                rds_resp = rds_client.describe_db_instances(DBInstanceIdentifier=direct_id)
                arn = rds_resp['DBInstances'][0]['DBInstanceArn']
                resource_type = 'rds'
            except:
                # Fallback to ASG
                try:
                    asg_resp = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[direct_id])
                    if asg_resp['AutoScalingGroups']:
                        arn = asg_resp['AutoScalingGroups'][0]['AutoScalingGroupARN']
                        resource_type = 'asg'
                    else:
                        raise Exception("ASG not found")
                except:
                    return {'statusCode': 404, 'body': json.dumps({'error': f"Resource {direct_id} not found as EC2, RDS, or ASG"})}

        if tag_key and tag_value:
            success, msg = handle_tag(direct_id, arn, tag_key, tag_value)
            results = {
                'action': 'tag',
                'processed': [{'arn': arn, 'status': msg}] if success else [],
                'failed': [{'arn': arn, 'error': msg}] if not success else []
            }
            return {'statusCode': 200, 'body': json.dumps(results)}

        if direct_action:
            action = direct_action.lower()
            if action not in ['start', 'stop']:
                return {'statusCode': 400, 'body': json.dumps({'error': 'Invalid action. Use "start" or "stop".'})}

            if resource_type == 'ec2':
                success, msg = handle_ec2(direct_id, action)
            elif resource_type == 'rds':
                success, msg = handle_rds(direct_id, arn, action)
            elif resource_type == 'asg':
                success, msg = handle_asg(direct_id, action)

            results = {
                'action': action,
                'processed': [{'arn': arn, 'status': msg}] if success else [],
                'failed': [{'arn': arn, 'error': msg}] if not success else []
            }
            return {'statusCode': 200, 'body': json.dumps(results)}

    # Tag-based filtering (existing logic)
    path = event.get('rawPath', '')
    action = 'start' if '/start' in path else 'stop'
    
    body = {}
    if event.get('body'):
        try:
            body = json.loads(event['body'])
        except json.JSONDecodeError:
            pass

    tag_filters = get_tag_filters(body)
    
    # Safety: Require tags for bulk operations to avoid accidental mass actions
    if not tag_filters:
        return {
            'statusCode': 400,
            'body': json.dumps({'error': 'Tag filters are required for bulk operations. Pass "tags": {"key": "value"} in the request body.'})
        }
    
    results = {
        'action': action,
        'processed': [],
        'failed': []
    }

    try:
        paginator = tagging_client.get_paginator('get_resources')
        page_iterator = paginator.paginate(
            TagFilters=tag_filters,
            ResourceTypeFilters=['ec2:instance', 'rds:db', 'autoscaling:autoScalingGroup']
        )

        for page in page_iterator:
            for resource in page['ResourceTagMappingList']:
                arn = resource['ResourceARN']
                
                if ':ec2:' in arn:
                    instance_id = arn.split('/')[-1]
                    success, msg = handle_ec2(instance_id, action)
                elif ':rds:' in arn:
                    db_id = arn.split(':')[-1]
                    success, msg = handle_rds(db_id, arn, action)
                elif ':autoscaling:' in arn:
                    asg_name = arn.split('/')[-1]
                    success, msg = handle_asg(asg_name, action)
                else:
                    logger.warning(f"Unsupported resource type: {arn}")
                    continue

                if success:
                    results['processed'].append({'arn': arn, 'status': msg})
                else:
                    results['failed'].append({'arn': arn, 'error': msg})

        return {
            'statusCode': 200,
            'body': json.dumps(results)
        }

    except Exception as e:
        logger.error(f"Fatal error: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': 'Internal Server Error', 'details': str(e)})
        }
