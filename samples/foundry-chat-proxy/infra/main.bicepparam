// Example parameter file. Copy, edit, and pass with:
//   az deployment group create -g <rg> -f infra/main.bicep -p infra/main.bicepparam
using './main.bicep'

param appName = 'foundrychat'
param foundryEndpoint = 'https://<your-foundry-resource>.cognitiveservices.azure.com/'
param foundryDeployment = 'gpt-4o-mini'
param foundryApiVersion = '2024-10-21'

// OPERATOR GATE: leave the key empty here. Set it after deploy, or replace the
// param with a Key Vault reference. Never commit a real key to this file.
param foundryApiKey = ''
