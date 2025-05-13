import os
import base64
import re
from datetime import datetime, timedelta, date
import json
import uuid
import azure.functions as func
import logging
import requests
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient
from azure.cosmos import CosmosClient, exceptions

tables = {
    'events': os.environ.get('EVENTS_TABLE_NAME'),
    'modules': os.environ.get('MODULES_TABLE_NAME'),
    'policies': os.environ.get('POLICIES_TABLE_NAME'),
    'deployments': os.environ.get('DEPLOYMENTS_TABLE_NAME'),
    'change_records': os.environ.get('CHANGE_RECORDS_TABLE_NAME'),
    'config': os.environ.get('CONFIG_TABLE_NAME'),
}

buckets = {
    'modules': os.environ.get('MODULE_S3_BUCKET'),
    'policies': os.environ.get('POLICY_S3_BUCKET'),
    'change_records': os.environ.get('CHANGE_RECORD_S3_BUCKET'),
    'providers':     os.environ.get('PROVIDERS_S3_BUCKET'),
}
    
COSMOS_DB_ENDPOINT = os.getenv("COSMOS_DB_ENDPOINT")
COSMOS_DB_DATABASE = os.getenv("COSMOS_DB_DATABASE")

# Function is fronted by Easy Auth authentication and can safely use Anonymous authentication here
app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

from azure.mgmt.containerinstance import ContainerInstanceManagementClient
from azure.mgmt.containerinstance.models import (
    ContainerGroup,
    Container,
    ContainerGroupNetworkProtocol,
    ContainerPort,
    ResourceRequests,
    ResourceRequirements,
    OperatingSystemTypes,
    ContainerGroupIdentity,
    ResourceIdentityType,
    ContainerGroupSubnetId,
    ContainerGroupDiagnostics,
    LogAnalytics
)

@app.function_name(name="generic_api")
@app.route(route="api")
def handler(req: func.HttpRequest) -> func.HttpResponse:
    try:
        req_body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body.", status_code=400)
    logging.info("req_body:")
    logging.info(req_body)

    event = req_body.get('event')
    import traceback
    try:
        if event == 'insert_db':
            return insert_db(req)
        elif event == 'read_db':
            return read_db(req)
        elif event == 'start_runner':
            return start_runner(req)
        elif event == 'upload_file_base64':
            return upload_file_base64(req)
        elif event == 'upload_file_url':
            return upload_file_url(req)
        elif event == 'read_logs':
            return read_logs(req)
        elif event == 'generate_presigned_url':
            return generate_presigned_url(req)
        elif event == 'transact_write':
            return transact_write(req)
        else:
            return func.HttpResponse(json.dumps({"result":f"Invalid event type ({event})"}), status_code=400)
    except Exception as e:
        tb = traceback.format_exc()
        return func.HttpResponse(json.dumps({"result":f"Api error: {e}", "tb": tb}), status_code=500)
    
def transact_write(req: func.HttpRequest) -> func.HttpResponse:
    try:
        req_body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body.", status_code=400)

    principal = os.getenv("AZURE_SUBSCRIPTION_ID")
    if not principal:
        return func.HttpResponse("Missing AZURE_SUBSCRIPTION_ID", status_code=500)

    resp = get_work_token(principal)
    try:
        tokens = resp.json()
    except Exception as e:
        return func.HttpResponse(resp.text, status_code=resp.status_code)

    db_link   = f"dbs/{COSMOS_DB_DATABASE}"

    responses = []

    for item in req_body['items']:
        try:
            if 'Put' in item:
                container_name = item['Put']['TableName']
                container_name = tables[container_name]

                coll_link   = f"{db_link}/colls/{container_name}"
                credential  = { coll_link: tokens[container_name] }
                client      = CosmosClient(COSMOS_DB_ENDPOINT, credential=credential)
                container   = client.get_database_client(COSMOS_DB_DATABASE).get_container_client(container_name)

                put_item = item['Put']['Item']
                put_item.update({'id': get_id(put_item)}) # Reserved field that should not be used in InfraWeave rows, but is required by Cosmos DB
                
                response = container.upsert_item(put_item)
                responses.append({"operation": "Put", "status": "Success", "item_id": put_item["id"]})
                
            elif 'Delete' in item:
                container_name = item['Delete']['TableName']
                container_name = tables[container_name]
                
                coll_link   = f"{db_link}/colls/{container_name}"
                credential  = { coll_link: tokens[container_name] }
                client      = CosmosClient(COSMOS_DB_ENDPOINT, credential=credential)
                container   = client.get_database_client(COSMOS_DB_DATABASE).get_container_client(container_name)
                
                delete_key = item['Delete']['Key']
                
                container.delete_item(item=delete_key['id'], partition_key=principal)
                responses.append({"operation": "Delete", "status": "Success", "item_id": delete_key["id"]})

        except exceptions.CosmosHttpResponseError as e:
            responses.append({
                "error": str(e)
            })
    return func.HttpResponse(
        body=json.dumps(responses),
        status_code=200,
        mimetype="application/json"
    )


def read_logs(req: func.HttpRequest) -> func.HttpResponse:
    from azure.monitor.query import LogsQueryClient
    try:
        req_body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body.", status_code=400)
    
    payload = req_body.get('data', {})
    job_id = payload.get('job_id')
    if not job_id:
        return func.HttpResponse("Missing job_id.", status_code=400)

    log_analytics_workspace_id = os.getenv("LOG_ANALYTICS_WORKSPACE_ID")

    try:
        credential = DefaultAzureCredential()
        client = LogsQueryClient(credential)
        
        query = f"""
        ContainerInstanceLog_CL
        | where ContainerGroup_s == "{job_id}"
        | order by TimeGenerated asc
        """

        timespan = timedelta(days=365)
        response = client.query_workspace(log_analytics_workspace_id, query, timespan=timespan)

        def json_serial(obj):
            if isinstance(obj, (datetime, date)):
                return obj.isoformat()
            raise TypeError(f"Type {obj.__class__.__name__} not serializable")

    
        events = []
        if response.tables:
            for table in response.tables:
                events.extend([{"message": row["Message"]} for row in table.rows])

    except Exception as e:
        return func.HttpResponse(f"Error querying logs: {e}", status_code=500)
    
    return func.HttpResponse(
        body=json.dumps({"events": events}, default=json_serial),
        status_code=200,
        mimetype="application/json"
    )

def generate_presigned_url(req: func.HttpRequest) -> func.HttpResponse:
    try:
        req_body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body.", status_code=400)
    
    from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions


    req_body = req.get_json()
    payload = req_body.get('data')
    bucket_name = payload.get("bucket_name")
    container_name = buckets[bucket_name]
    blob_name = payload.get("key")
    if container_name.startswith("workload-"):
        account_name = os.getenv("STORAGE_ACCOUNT_NAME")
        principal = os.getenv("AZURE_SUBSCRIPTION_ID")
        resp = get_work_token(principal)
        try:
            tokens = resp.json()
        except Exception as e:
            return func.HttpResponse(resp.text, status_code=resp.status_code)

        container_sas = tokens[container_name]
        blob_url = (
            f"https://{account_name}.blob.core.windows.net/"
            f"{container_name}/{blob_name}?{container_sas}"
        )
        return func.HttpResponse(
            json.dumps({"url": blob_url}),
            status_code=200,
            mimetype="application/json"
        )

    account_name = os.getenv("PUBLIC_STORAGE_ACCOUNT_NAME")

    expires_in = payload.get("expires_in", 3600)


    sas_expiry = datetime.utcnow() + timedelta(seconds=expires_in)


    blob_service_client = BlobServiceClient(
        account_url=f"https://{account_name}.blob.core.windows.net",
        credential=DefaultAzureCredential()
    )

    user_delegation_key = blob_service_client.get_user_delegation_key(
        key_start_time=datetime.utcnow() - timedelta(minutes=1),
        key_expiry_time=sas_expiry
    )

    sas_token = generate_blob_sas(
        account_name=account_name,
        container_name=container_name,
        blob_name=blob_name,
        permission=BlobSasPermissions(read=True),
        expiry=sas_expiry,
        user_delegation_key=user_delegation_key,
    )

    blob_url = f"https://{account_name}.blob.core.windows.net/{container_name}/{blob_name}?{sas_token}"

    return func.HttpResponse(
        json.dumps({"url": blob_url}),
        status_code=200,
        mimetype="application/json"
    )

def start_runner(req: func.HttpRequest) -> func.HttpResponse:
    try:
        req_body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body.", status_code=400)

    subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID")
    resource_group_name = os.getenv("RESOURCE_GROUP_NAME")
    image = os.getenv("IMAGE")
    region_short = os.getenv("REGION_SHORT")

    try:
        # needs to be cleaned up, exmaple: (max 100 containers regardless of state (running, stopped, etc.))
        delete_finished_container_groups(subscription_id, resource_group_name)
    except Exception as e:
        print(f"Error deleting finished container groups: {e}. Continuing...")
        pass

    try:
        credential = DefaultAzureCredential()
        client = ContainerInstanceManagementClient(credential, subscription_id)
    except Exception as e:
        return func.HttpResponse(f"Error initializing ACI client: {e}", status_code=500)

    logging.info('Python HTTP trigger function processed a request.')

    container_group_name = f"infraweave-runner-job-{subscription_id[:8]}-{region_short}-{str(uuid.uuid4())[:8]}"

    payload = req_body.get('data')
    cpu = payload.get('cpu')
    memory = payload.get('memory')

    log_analytics_workspace_id = os.getenv("LOG_ANALYTICS_WORKSPACE_ID")
    log_analytics_workspace_key = os.getenv("LOG_ANALYTICS_WORKSPACE_KEY")

    diagnostics = ContainerGroupDiagnostics(
        log_analytics=LogAnalytics(
            workspace_id=log_analytics_workspace_id,
            workspace_key=log_analytics_workspace_key,
            log_type="ContainerInsights"
        )
    )

    try:
        container_resource_requirements = ResourceRequirements(
            requests=ResourceRequests(
                memory_in_gb=memory,
                cpu=cpu,
            )
        )
        container = Container(
            name="runner",
            image=image,
            resources=container_resource_requirements,
            ports=[],
            environment_variables=[
                {
                    "name": "PAYLOAD",
                    "value": json.dumps(payload)
                },
                {
                    "name": "REGION", 
                    "value": os.getenv("REGION")
                },
                {
                    "name": "AZURE_SUBSCRIPTION_ID",
                    "value": os.getenv("AZURE_SUBSCRIPTION_ID")
                },
                {
                    "name": "INFRAWEAVE_ENV",
                    "value": os.getenv("INFRAWEAVE_ENV")
                },
                {
                    "name": "PROVIDER",
                    "value": "azure"
                },
                {
                    "name": "AZURE_CONTAINER_INSTANCE",
                    "value": "true"
                },
                {
                    "name": "ACCOUNT_ID", # to be renamed to PROJECT_ID
                    "value": os.getenv("AZURE_SUBSCRIPTION_ID")
                },
                {
                    "name": "TF_BUCKET",
                    "value": os.getenv("TF_STATE_CONTAINER")
                },
                {
                    "name": "STORAGE_ACCOUNT",
                    "value": os.getenv("STORAGE_ACCOUNT_NAME")
                },
                {
                    "name": "RESOURCE_GROUP_NAME",
                    "value": os.getenv("RESOURCE_GROUP_NAME")
                },
                {
                    "name": "CONTAINER_GROUP_NAME",
                    "value": container_group_name
                },
                { "name": "ARM_USE_MSI",       "value": "true" },
                { "name": "ARM_USE_AZUREAD",   "value": "true" },
                { "name": "ARM_CLIENT_ID",     "value": os.getenv("TF_AZURE_CLIENT_ID") },
                { "name": "ARM_TENANT_ID",     "value": os.getenv("TF_AZURE_TENANT_ID") },
                { "name": "ARM_SUBSCRIPTION_ID", "value": os.getenv("AZURE_SUBSCRIPTION_ID") },
            ]
        )

        container_group = ContainerGroup(
            location=os.getenv("LOCATION"),
            containers=[container],
            os_type=OperatingSystemTypes.Linux,
            restart_policy="Never",
            identity=ContainerGroupIdentity(
                type=ResourceIdentityType.user_assigned,
                user_assigned_identities={
                    os.getenv("USER_ASSIGNED_IDENTITY_RESOURCE_ID"): {}
                }
            ),
            subnet_ids=[
                ContainerGroupSubnetId(
                    id=os.getenv("ACI_SUBNET_ID")
                )
            ],
            diagnostics=diagnostics,
        )

        client.container_groups.begin_create_or_update(
            resource_group_name=resource_group_name,
            container_group_name=container_group_name,
            container_group=container_group
        )
        
        logging.info("ACI task started successfully.")
        return func.HttpResponse(json.dumps({"status": f"ACI task started successfully.", "job_id": container_group_name}), status_code=200)

    except Exception as e:
        logging.error(f"Error starting ACI task: {e}")
        return func.HttpResponse(json.dumps({"status": f"Error starting ACI task {e}"}), status_code=500)

def get_id(body):
    raw = f"{body['PK']}~{body['SK']}".lower()
    safe = re.sub(r'[^0-9a-z]', '_', raw)
    return safe

def insert_db(req: func.HttpRequest) -> func.HttpResponse:
    try:
        req_body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body.", status_code=400)

    table_key = req_body.get('table')
    container_name = tables[table_key]
    item = req_body.get('data', {})
    item.update({'id': get_id(item)})

    principal = os.getenv("AZURE_SUBSCRIPTION_ID")
    if not principal:
        return func.HttpResponse("Missing AZURE_SUBSCRIPTION_ID", status_code=500)

    resp = get_work_token(principal)
    try:
        tokens = resp.json()
    except Exception as e:
        return func.HttpResponse(resp.text, status_code=resp.status_code)

    db_link   = f"dbs/{COSMOS_DB_DATABASE}"
    coll_link = f"{db_link}/colls/{container_name}"
    credential = { coll_link: tokens[container_name] }
    pk_arg = {}

    client = CosmosClient(COSMOS_DB_ENDPOINT, credential=credential)
    database = client.get_database_client(COSMOS_DB_DATABASE)
    container = database.get_container_client(container_name)

    try:
        response = container.upsert_item(body=item, **pk_arg)
        logging.info("Insert/upsert succeeded:")
        logging.info(response)
        return func.HttpResponse(json.dumps(response), status_code=200)
    except exceptions.CosmosHttpResponseError as e:
        logging.error("Error inserting item:", exc_info=e)
        return func.HttpResponse(f'Error inserting item: {e}', status_code=500)


def read_db(req: func.HttpRequest) -> func.HttpResponse:
    try:
        req_body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body.", status_code=400)

    container_name = req_body.get('table')
    container_name = tables[container_name]
    query = req_body.get('data').get('query')
    table_key    = req_body["table"]
    principal = os.getenv("AZURE_SUBSCRIPTION_ID")
    if not principal:
        return func.HttpResponse("Missing AZURE_SUBSCRIPTION_ID", status_code=500)

    if table_key in ["modules", "policies", "config"]:
        credential = DefaultAzureCredential()
        cross_partition = True
        pk_arg = {}
    else:
        resp = get_work_token(principal)
        try:
            tokens = resp.json()
        except Exception as e:
            return func.HttpResponse(resp.text, status_code=resp.status_code)
        db_link   = f"dbs/{COSMOS_DB_DATABASE}"
        coll_link = f"{db_link}/colls/{container_name}"
        credential = {
            coll_link: tokens[container_name]
        }
        cross_partition = False
        pk_arg = {"partition_key": principal}
    client = CosmosClient(COSMOS_DB_ENDPOINT, credential=credential)

    q_kwargs = {
        "query": query,
        **({"enable_cross_partition_query": True} if cross_partition else {}),
        **pk_arg
    }

    database = client.get_database_client(COSMOS_DB_DATABASE)
    container = database.get_container_client(container_name)

    try:
        items = list(container.query_items(**q_kwargs))
        logging.info(f"Read operation succeeded, found {len(items)} items.")
        logging.info("response:")
        logging.info(items)
        return func.HttpResponse(json.dumps(items), status_code=200)
    except exceptions.CosmosHttpResponseError as e:
        print(f'Error querying items: {e}')
        logging.error("response error:")
        logging.error(e)
        return func.HttpResponse(json.dumps({"message": f"error querying: {e}"}), status_code=500)

def upload_file_base64(req: func.HttpRequest) -> func.HttpResponse:
    try:
        req_body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body.", status_code=400)
    
    account_name = os.getenv("STORAGE_ACCOUNT_NAME")
    blob_service_client = BlobServiceClient(
        account_url=f"https://{account_name}.blob.core.windows.net",
        credential=DefaultAzureCredential()
    )

    payload = req_body.get('data')
    bucket_name = payload.get('bucket_name')
    container_name = buckets[bucket_name]
    blob_name = payload.get('key')
    base64_body = payload.get('base64_content')
    binary_body = base64.b64decode(base64_body)
    blob_client = blob_service_client.get_blob_client(container=container_name, blob=blob_name)
    blob_client.upload_blob(binary_body, overwrite=True)
    print(f"Blob {blob_name} uploaded to container {container_name} successfully.")
    response_body = {
        "status": f"Blob {blob_name} uploaded to container {container_name} successfully."
    }
    return func.HttpResponse(
        json.dumps(response_body),
        status_code=200,
        mimetype="application/json"
    )

def upload_file_url(req: func.HttpRequest) -> func.HttpResponse:
    try:
        req_body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body.", status_code=400)

    payload = req_body.get('data', {})
    bucket_name = payload.get('bucket_name')
    container_name = buckets.get(bucket_name)
    if not container_name:
        return func.HttpResponse(f"Unknown container_name '{container_name}'", status_code=400)

    blob_name = payload.get('key')

    download_url = payload.get('url')
    account_name = os.getenv("STORAGE_ACCOUNT_NAME")
    blob_service = BlobServiceClient(
        account_url=f"https://{account_name}.blob.core.windows.net",
        credential=DefaultAzureCredential()
    )
    blob_client = blob_service.get_blob_client(container=container_name, blob=blob_name)

    # check if blob already exists
    if blob_client.exists():
        return func.HttpResponse(
            json.dumps({"object_already_exists": True}),
            status_code=200,
            mimetype="application/json"
        )

    # download from URL and upload
    try:
        with urllib.request.urlopen(download_url) as resp:
            blob_client.upload_blob(resp, overwrite=False)
    except Exception as e:
        return func.HttpResponse(f"Error uploading blob: {e}", status_code=500)

    return func.HttpResponse(
        json.dumps({"object_already_exists": False}),
        status_code=200,
        mimetype="application/json"
    )

### EXTRA FUNCTIONS ###

def delete_finished_container_groups(subscription_id, resource_group_name):
    credential = DefaultAzureCredential()
    client = ContainerInstanceManagementClient(credential, subscription_id)

    finished_states = {"Succeeded", "Failed"}

    container_groups = client.container_groups.list_by_resource_group(resource_group_name)
    
    for cg in container_groups:
        state = cg.provisioning_state
        if state in finished_states:
            print(f"Deleting container group: {cg.name} (state: {state})")
            delete_op = client.container_groups.begin_delete(resource_group_name, cg.name)
            delete_op.wait()
        else:
            print(f"Skipping container group: {cg.name} (state: {state})")

work_token_cache = {}

def get_work_token(pk: str) -> str:
    cred  = ManagedIdentityCredential()
    jwt   = cred.get_token(os.environ["BROKER_SCOPE"]).token
    subId = pk

    resp = requests.post(
        f"{os.environ['BROKER_URL']}/token",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"data": {"partitionKey": subId}},
        timeout=30,
    )

    return resp
