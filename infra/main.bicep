targetScope = 'subscription'

import { ModelDeployment } from 'modules/gateway.bicep'

@description('Azure Developer CLI environment name.')
param environmentName string = 'playground'

@description('Selected Azure location that supports AI Gateway.')
param location string

@description('Resource group owned by this playground.')
param resourceGroupName string = 'rg-${environmentName}-${uniqueString(subscription().subscriptionId, environmentName, location)}'

@description('Object ID of the developer who runs the local application.')
param principalId string = ''

@description('Three distinct model deployments to create in the playground Foundry account.')
param modelDeployments ModelDeployment[] = loadJsonContent('../config.example.json').modelDeployments

var resourceToken = uniqueString(subscription().subscriptionId, environmentName, location)
var tags = {
  'azd-env-name': environmentName
  purpose: 'ai-gateway-playground'
}

resource demoGroup 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module gateway 'modules/gateway.bicep' = {
  scope: demoGroup
  params: {
    location: location
    modelDeployments: modelDeployments
    resourceToken: resourceToken
    principalId: principalId
    tags: tags
  }
}

module modelAccess 'modules/model-access.bicep' = {
  scope: demoGroup
  params: {
    foundryName: gateway.outputs.foundryName
    principalId: gateway.outputs.principalId
  }
}

output gatewayId string = gateway.outputs.gatewayId
output gatewayName string = gateway.outputs.gatewayName
output hostname string = gateway.outputs.hostname
output applicationInsightsId string = gateway.outputs.applicationInsightsId
output modelAccessId string = modelAccess.outputs.roleAssignmentId
output foundryId string = gateway.outputs.foundryId
output foundryName string = gateway.outputs.foundryName
output logWorkspaceId string = gateway.outputs.logWorkspaceId
output logWorkspaceName string = gateway.outputs.logWorkspaceName
output AZURE_RESOURCE_GROUP string = demoGroup.name
output AZURE_MODEL_DEPLOYMENTS array = modelDeployments
output AZURE_TENANT_ID string = tenant().tenantId
