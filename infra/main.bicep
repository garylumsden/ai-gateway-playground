targetScope = 'subscription'

@description('Selected Azure location that supports AI Gateway.')
param location string

@description('Resource group owned by this playground.')
param resourceGroupName string = 'rg-ai-gateway-playground'

@description('Three distinct model deployments to create in the playground Foundry account.')
param modelDeployments {
  name: string
  model: string
  version: string
  sku: string
  capacity: int
}[]

resource demoGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: {
    purpose: 'ai-gateway-playground'
  }
}

module gateway 'modules/gateway.bicep' = {
  scope: demoGroup
  params: {
    location: location
    modelDeployments: modelDeployments
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
