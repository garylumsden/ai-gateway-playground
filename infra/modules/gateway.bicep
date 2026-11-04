@description('Selected Azure location.')
param location string

@export()
type ModelDeployment = {
  name: string
  model: string
  version: string
  sku: string
  capacity: int
}

@description('Model deployment definitions.')
param modelDeployments ModelDeployment[]

@description('Unique token for this azd environment.')
param resourceToken string = uniqueString(resourceGroup().id)

@description('Developer object ID for local telemetry ingestion.')
param principalId string = ''

@description('Environment ownership tags.')
param tags object = {}

var suffix = resourceToken

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: 'foundry-playground-${suffix}'
  location: location
  kind: 'AIServices'
  tags: tags
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: 'foundry-playground-${suffix}'
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

@batchSize(1)
resource models 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = [
  for model in modelDeployments: {
    parent: foundry
    name: model.name
    sku: {
      name: model.sku
      capacity: model.capacity
    }
    properties: {
      model: {
        format: 'OpenAI'
        name: model.model
        version: model.version
      }
      versionUpgradeOption: 'NoAutoUpgrade'
    }
  }
]

// The bundled Bicep catalog lacks this type; the public 2026-09-01-preview schema and ARM validate it.
#disable-next-line BCP081
resource gateway 'Microsoft.ApiManagement/aigateways@2026-09-01-preview' = {
  name: 'aigw-play-${suffix}'
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'Standard'
  }
  properties: {}
  tags: tags
}

module monitoring 'monitoring.bicep' = {
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
    telemetryPublishers: concat([
      {
        principalId: gateway.identity.principalId
        principalType: 'ServicePrincipal'
      }
    ], empty(principalId) ? [] : [
      {
        principalId: principalId
        principalType: 'User'
      }
    ])
  }
}

// Category groups require the current preview contract; the catalog recommends the older stable API.
#disable-next-line use-recent-api-versions
resource foundryDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'send-to-log-analytics'
  scope: foundry
  properties: {
    workspaceId: monitoring.outputs.workspaceId
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

output gatewayId string = gateway.id
output gatewayName string = gateway.name
output principalId string = gateway.identity.principalId
output hostname string = gateway.properties.frontend.defaultHostname
output applicationInsightsId string = monitoring.outputs.appInsightsId
output foundryId string = foundry.id
output foundryName string = foundry.name
output logWorkspaceId string = monitoring.outputs.workspaceId
output logWorkspaceName string = monitoring.outputs.workspaceName
