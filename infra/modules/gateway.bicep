@description('Selected Azure location.')
param location string

@description('Model deployment definitions.')
param modelDeployments {
  name: string
  model: string
  version: string
  sku: string
  capacity: int
}[]

var suffix = uniqueString(resourceGroup().id)

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'logs-playground-${suffix}'
  location: location
  properties: {
    retentionInDays: 30
    sku: {
      name: 'PerGB2018'
    }
  }
}

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: 'foundry-playground-${suffix}'
  location: location
  kind: 'AIServices'
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
  name: 'aigw-playground-${suffix}'
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'Consumption'
  }
  properties: {}
  tags: {
    purpose: 'ai-gateway-playground'
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-playground-${suffix}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

output gatewayId string = gateway.id
output gatewayName string = gateway.name
output principalId string = gateway.identity.principalId
output hostname string = gateway.properties.frontend.defaultHostname
output applicationInsightsId string = appInsights.id
output foundryId string = foundry.id
output foundryName string = foundry.name
output logWorkspaceId string = workspace.id
output logWorkspaceName string = workspace.name
