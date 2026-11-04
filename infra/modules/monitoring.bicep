type TelemetryPublisher = {
  principalId: string
  principalType: 'ServicePrincipal' | 'User' | 'Group'
}

param location string
param resourceToken string
param tags object = {}
param telemetryPublishers TelemetryPublisher[] = []

// Monitoring Metrics Publisher: https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/monitor
var monitoringMetricsPublisherRoleId = '3913510d-42f4-4e42-8a64-420c390055eb'

resource workspace 'Microsoft.OperationalInsights/workspaces@2025-07-01' = {
  name: 'logs-playground-${resourceToken}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-playground-${resourceToken}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    DisableLocalAuth: true
    SamplingPercentage: 100
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

resource publisherRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for publisher in telemetryPublishers: {
    name: guid(appInsights.id, publisher.principalId, monitoringMetricsPublisherRoleId)
    scope: appInsights
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringMetricsPublisherRoleId)
      principalId: publisher.principalId
      principalType: publisher.principalType
    }
  }
]

output workspaceId string = workspace.id
output workspaceName string = workspace.name
output appInsightsId string = appInsights.id
