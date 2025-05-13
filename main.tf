locals {
  is_workload_in_central  = var.central_subscription_id == data.azurerm_client_config.current.subscription_id
  subscription_id         = data.azurerm_client_config.current.subscription_id
  proj_short              = substr(local.subscription_id, 0, 18)
  proj_supershort         = substr(replace(local.subscription_id, "-", ""), 0, 11) # 0.0284% probability of collision in 100,000 subscriptions (birthday-paradox approximation)
  central_proj_short      = substr(var.central_subscription_id, 0, 18)
  central_proj_supershort = substr(replace(var.central_subscription_id, "-", ""), 0, 11) # 0.0284% probability of collision in 100,000 subscriptions (birthday-paradox approximation)
  func_name               = "iw-${local.proj_short}-${var.region}-${var.environment}"
  central_func_name       = "iw-${local.central_proj_short}-${var.region}-${var.environment}"

  region_short = lookup(
    local.region_codes,
    var.region,                   # full code like "westeurope" -> "weu"
    substr(md5(var.region), 0, 3) # fallback to a hash
  )

  runner_image = "quay.io/infraweave/runner:v0.0.85-amd64"
}

data "azurerm_client_config" "current" {}

resource "azurerm_resource_provider_registration" "microsoft_app" {
  name = "Microsoft.App"
}

resource "azurerm_resource_group" "main" {
  name     = "infraweave-workload-${local.proj_short}-${var.region}-${var.environment}"
  location = var.region
}

resource "azurerm_log_analytics_workspace" "container_logs" {
  name                = "law-infraweave-${local.proj_short}-${var.region}-${var.environment}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 365
}

resource "azurerm_storage_account" "storage" {
  name                     = "w${local.proj_supershort}${local.region_short}${var.environment}" # 24 chars limit (1 + 11 for subscription, 4 for region => 8 for env)
  resource_group_name      = azurerm_resource_group.main.name
  location                 = azurerm_resource_group.main.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  min_tls_version          = "TLS1_2"

  blob_properties {
    versioning_enabled = true
  }
}

resource "azurerm_service_plan" "function_plan" {
  name                = "sp-infraweave-${local.proj_short}-${var.region}-${var.environment}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  os_type             = "Linux"
  sku_name            = "Y1"

}

data "archive_file" "function_zip" {
  type        = "zip"
  source_dir  = "${path.module}/api"
  output_path = "${path.module}/function2.zip"
}

resource "azurerm_storage_container" "function_deploy" {
  name                  = "function-deploy"
  storage_account_id    = azurerm_storage_account.storage.id
  container_access_type = "private"
}

resource "azurerm_storage_blob" "function_blob" {
  name                   = "function2.zip"
  storage_account_name   = azurerm_storage_account.storage.name
  storage_container_name = azurerm_storage_container.function_deploy.name
  type                   = "Block"
  source                 = data.archive_file.function_zip.output_path

  depends_on = [data.archive_file.function_zip]
}

data "azurerm_storage_account_sas" "function_sas" {
  connection_string = azurerm_storage_account.storage.primary_connection_string
  https_only        = true

  start  = formatdate("2025-01-02", timestamp())
  expiry = formatdate("2026-01-02", timeadd(timestamp(), "8760h"))

  permissions {
    read    = true
    write   = false
    delete  = false
    list    = true
    add     = false
    create  = false
    update  = false
    process = false
    tag     = false
    filter  = false
  }

  resource_types {
    service   = false
    container = false
    object    = true
  }

  services {
    blob  = true
    queue = false
    table = false
    file  = false
  }
}

resource "azurerm_linux_function_app" "function_app" {
  name                       = local.func_name
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  service_plan_id            = azurerm_service_plan.function_plan.id
  storage_account_name       = azurerm_storage_account.storage.name
  storage_account_access_key = azurerm_storage_account.storage.primary_access_key

  app_settings = {
    "FUNCTIONS_WORKER_RUNTIME"       = "python"
    "SCM_DO_BUILD_DURING_DEPLOYMENT" = "true"
    "FUNCTIONS_EXTENSION_VERSION"    = "~4"
    "AzureWebJobsFeatureFlags"       = "EnableWorkerIndexing"
    "WEBSITE_CONTENTSHARE"           = "functionappshare"
    "ENABLE_ORYX_BUILD"              = "true"

    # Environment variables
    "AZURE_SUBSCRIPTION_ID" = local.subscription_id
    "TF_AZURE_TENANT_ID"    = data.azurerm_client_config.current.tenant_id
    "TF_AZURE_CLIENT_ID"    = azurerm_user_assigned_identity.aci_identity.client_id

    INFRAWEAVE_ENV = var.environment

    "RESOURCE_GROUP_NAME" = azurerm_resource_group.main.name
    "LOCATION"            = azurerm_resource_group.main.location
    "REGION"              = var.region
    "REGION_SHORT"        = local.region_short
    "IMAGE"               = local.runner_image

    "COSMOS_DB_ENDPOINT" = "https://iw-${local.central_proj_short}-${var.region}-${var.environment}.documents.azure.com:443/"
    "COSMOS_DB_DATABASE" = "db-infraweave"

    "AZURE_SUBSCRIPTION_ID" = local.subscription_id

    "EVENTS_TABLE_NAME"         = "events"
    "MODULES_TABLE_NAME"        = "modules"
    "POLICIES_TABLE_NAME"       = "policies"
    "CHANGE_RECORDS_TABLE_NAME" = "change-records"
    "DEPLOYMENTS_TABLE_NAME"    = "deployments"
    "CONFIG_TABLE_NAME"         = "config"

    "MODULE_S3_BUCKET"        = "modules"
    "POLICY_S3_BUCKET"        = "policies"
    "PROVIDERS_S3_BUCKET"     = "providers"
    "CHANGE_RECORD_S3_BUCKET" = "workload-change-records-${local.subscription_id}"
    "TF_STATE_CONTAINER"      = "workload-tf-state-${local.subscription_id}"

    BROKER_SCOPE = "api://infraweave-broker-${var.central_subscription_id}-${var.environment}-${var.region}/.default"
    BROKER_URL   = "https://${local.central_func_name}.azurewebsites.net/api"

    "USER_ASSIGNED_IDENTITY_RESOURCE_ID" = azurerm_user_assigned_identity.aci_identity.id
    "ACI_SUBNET_ID"                      = azurerm_subnet.aci_subnet.id

    STORAGE_ACCOUNT_NAME        = "c${local.central_proj_supershort}${local.region_short}${var.environment}"
    PUBLIC_STORAGE_ACCOUNT_NAME = "p${local.central_proj_supershort}${local.region_short}${var.environment}"

    "LOG_ANALYTICS_WORKSPACE_ID"  = azurerm_log_analytics_workspace.container_logs.workspace_id
    "LOG_ANALYTICS_WORKSPACE_KEY" = azurerm_log_analytics_workspace.container_logs.primary_shared_key

    "BROKER_SECRET" = azuread_service_principal_password.broker_sp_secret.value
  }

  zip_deploy_file = data.archive_file.function_zip.output_path

  site_config {
    application_stack {
      python_version = "3.9"
    }

    cors {
      allowed_origins = ["https://portal.azure.com"]
    }
  }

  identity {
    type = "SystemAssigned"
  }

  auth_settings_v2 {
    auth_enabled           = true
    require_https          = true
    require_authentication = true

    active_directory_v2 {
      client_id            = azuread_application.broker.client_id
      tenant_auth_endpoint = "https://login.microsoftonline.com/${data.azurerm_client_config.current.tenant_id}/v2.0/"

      allowed_audiences = concat(
        [azuread_application.broker.client_id],
        [for uri in azuread_application.broker.identifier_uris : uri]
      )
    }

    login {}
  }

  depends_on = [azurerm_storage_blob.function_blob]

}

resource "azurerm_virtual_network" "aci_vnet" {
  name                = "vnet-infraweave-${local.proj_short}-${var.region}-${var.environment}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  address_space       = ["10.1.0.0/16"]
}

resource "azurerm_subnet" "aci_subnet" {
  name                 = "aci-subnet-infraweave-${local.proj_short}-${var.region}-${var.environment}"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.aci_vnet.name
  address_prefixes     = ["10.1.1.0/24"]


  delegation {
    name = "aciDelegation"
    service_delegation {
      name = "Microsoft.ContainerInstance/containerGroups"
      actions = [
        "Microsoft.Network/virtualNetworks/subnets/join/action",
        "Microsoft.Network/virtualNetworks/subnets/prepareNetworkPolicies/action",
      ]
    }
  }
}

resource "azurerm_role_assignment" "aci_contributor" {
  scope                = azurerm_resource_group.main.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_linux_function_app.function_app.identity[0].principal_id
}

resource "azurerm_user_assigned_identity" "aci_identity" {
  name                = "runner-id-${local.proj_short}-${var.region}-${var.environment}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
}

resource "azurerm_role_assignment" "aci_storage_role_function" {
  scope                = azurerm_storage_account.storage.id
  role_definition_name = "Storage Account Key Operator Service Role"
  principal_id         = azurerm_user_assigned_identity.aci_identity.principal_id
}

data "azurerm_subscription" "current" {}

resource "azurerm_role_assignment" "aci_admin_role" {
  scope                = data.azurerm_subscription.current.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.aci_identity.principal_id
}

output "function_app_default_hostname" {
  description = "The default URL for the Function App"
  value       = azurerm_linux_function_app.function_app.default_hostname
}

resource "azuread_app_role_assignment" "aci_invoker" {
  principal_object_id = azurerm_user_assigned_identity.aci_identity.principal_id
  app_role_id         = one(azuread_application.broker.app_role).id
  resource_object_id  = azuread_service_principal.broker_sp.object_id
}

resource "azuread_application" "broker" {
  display_name     = "api://infraweave-broker-${local.subscription_id}-${var.environment}-${var.region}"
  sign_in_audience = "AzureADMultipleOrgs"

  identifier_uris = [
    "api://infraweave-broker-${local.subscription_id}-${var.environment}-${var.region}",
  ]

  api {
    oauth2_permission_scope {
      id                         = uuid()
      admin_consent_description  = "Allow workloads to get per-subscription Cosmos tokens"
      admin_consent_display_name = "GetInfraWeaveToken"
      user_consent_description   = "Allow this app to generate resource tokens"
      user_consent_display_name  = "GenerateInfraWeaveToken"
      value                      = "access_as_infraweave"
      type                       = "User"
    }
  }
  app_role {
    id                   = uuid()
    display_name         = "InfraWeave Invoker"
    description          = "Allows principals to invoke the Function API"
    value                = "invoke_infraweave_app"
    allowed_member_types = ["User", "Application"]
  }
}

resource "azuread_service_principal" "broker_sp" {
  client_id                    = azuread_application.broker.client_id
  app_role_assignment_required = true
}

resource "azuread_service_principal_password" "broker_sp_secret" {
  service_principal_id = azuread_service_principal.broker_sp.id
  end_date             = timeadd(timestamp(), "8760h") # 1 year
}

resource "azuread_group" "function_invokers" {
  display_name     = "InfraWeave Function Invokers - ${local.subscription_id} (${var.environment})"
  security_enabled = true
}

resource "azuread_app_role_assignment" "group_app_role" {
  principal_object_id = azuread_group.function_invokers.object_id
  resource_object_id  = azuread_service_principal.broker_sp.object_id
  app_role_id         = one(azuread_application.broker.app_role).id
}

data "azuread_service_principal" "azure_cli" {
  client_id = "04b07795-8ddb-461a-bbee-02f9e1bf7b46" # Azure CLI App ID, see https://github.com/Azure/azure-cli/issues/28628#issuecomment-2302694201
}

resource "azuread_service_principal_delegated_permission_grant" "cli_broker_consent" {
  service_principal_object_id          = data.azuread_service_principal.azure_cli.object_id
  resource_service_principal_object_id = azuread_service_principal.broker_sp.object_id
  claim_values = [
    "access_as_infraweave"
  ]
}
