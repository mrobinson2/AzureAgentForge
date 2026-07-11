// Flex Consumption Function App for the foundry-chat-proxy sample.
//
// Deploys: a storage account (+ a deployment-package container), Log Analytics +
// Application Insights, a Flex Consumption plan, and a Node 24 function app wired
// to an Azure AI Foundry chat deployment via app settings.
//
// WHY FLEX CONSUMPTION (not classic Y1): classic Linux Consumption (Y1) has no
// Node 24 image — the host silently never starts (SCM 503s, keys API 400s). Flex
// Consumption sets the runtime in `functionAppConfig` and supports Node 24. See
// the README for the full symptom write-up.
//
// NO SECRETS IN THIS FILE. `foundryApiKey` is a @secure() parameter that defaults
// to '' — an OPERATOR GATE (see the note on that param, and the README section
// "Secrets are operator gates"). Storage and telemetry authenticate with the
// function app's managed identity (role assignment included), so there is no
// storage key or connection string in code.
//
// Compile locally with no Azure subscription:
//   az bicep build --file infra/main.bicep

targetScope = 'resourceGroup'

@description('Azure region for all resources. Defaults to the resource group location.')
param location string = resourceGroup().location

@description('Base name (3-16 lowercase alphanumerics). Resource names derive from this.')
@minLength(3)
@maxLength(16)
param appName string = 'foundrychat'

@description('Azure AI Foundry / Azure OpenAI resource endpoint, e.g. https://<resource>.cognitiveservices.azure.com/')
param foundryEndpoint string

@description('The Foundry chat *deployment* name to call (not the base model id).')
param foundryDeployment string = 'gpt-4o-mini'

@description('Azure OpenAI data-plane API version.')
param foundryApiVersion string = '2024-10-21'

// ── OPERATOR GATE ────────────────────────────────────────────────────────────
// Do NOT hardcode a key here and do NOT commit one. Leave this empty at deploy
// time and set the key AFTER deploy (README > "Secrets are operator gates"), or —
// better for production — replace this with a Key Vault reference.
@description('Foundry API key. Leave empty and set it post-deploy as an operator gate, or use a Key Vault reference.')
@secure()
param foundryApiKey string = ''

@description('Flex Consumption maximum instance count.')
@minValue(40)
@maxValue(1000)
param maximumInstanceCount int = 40

@description('Per-instance memory in MB.')
@allowed([
  512
  2048
  4096
])
param instanceMemoryMB int = 2048

var storageAccountName = take('st${toLower(appName)}${uniqueString(resourceGroup().id)}', 24)
var functionAppName = take('${toLower(appName)}-${uniqueString(resourceGroup().id)}', 40)
var planName = '${appName}-flex-plan'
var appInsightsName = '${appName}-ai'
var logAnalyticsName = '${appName}-logs'
var deploymentContainerName = 'app-package'

// Built-in role: Storage Blob Data Owner (for AzureWebJobsStorage + deployment container, identity-based).
var storageBlobDataOwnerRoleId = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
}

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: deploymentContainerName
  properties: { publicAccess: 'None' }
}

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

resource flexPlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: planName
  location: location
  kind: 'functionapp'
  sku: {
    tier: 'FlexConsumption'
    name: 'FC1'
  }
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: flexPlan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storageAccount.properties.primaryEndpoints.blob}${deploymentContainerName}'
          authentication: {
            type: 'SystemAssignedIdentity'
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: maximumInstanceCount
        instanceMemoryMB: instanceMemoryMB
      }
      runtime: {
        name: 'node'
        version: '24'
      }
    }
    siteConfig: {
      appSettings: [
        // Identity-based host storage — no account key in config.
        { name: 'AzureWebJobsStorage__accountName', value: storageAccount.name }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
        // Foundry wiring (see src/index.js).
        { name: 'FOUNDRY_ENDPOINT', value: foundryEndpoint }
        { name: 'FOUNDRY_DEPLOYMENT', value: foundryDeployment }
        { name: 'FOUNDRY_API_VERSION', value: foundryApiVersion }
        // OPERATOR GATE: blank by default. The function fails closed until this
        // is set (post-deploy CLI, or swap for a Key Vault reference).
        { name: 'FOUNDRY_API_KEY', value: foundryApiKey }
      ]
    }
  }
}

// Grant the function app's identity blob access on the storage account so the
// Flex host can read the deployment package and use identity-based AzureWebJobs
// storage (no shared key).
resource storageRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, functionApp.id, storageBlobDataOwnerRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataOwnerRoleId)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

@description('Function app name — pass to `az functionapp deployment source config-zip -n <this>`.')
output functionAppName string = functionApp.name

@description('Default host URL. The invoke URL is <host>/api/chat?code=<function key>.')
output functionAppHostName string = functionApp.properties.defaultHostName
