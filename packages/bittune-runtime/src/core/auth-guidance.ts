const UNKNOWN_PROVIDER = "unknown";

export function getBittuneConfigurationHelp(): string {
	return "Run `bittune configure --base-url <url> --model-id <id>`, export the API key environment variable supplied to configure, then run `bittune doctor`.";
}

export function formatNoModelsAvailableMessage(): string {
	return `No Bittune Agent model is configured. ${getBittuneConfigurationHelp()}`;
}

export function formatNoModelSelectedMessage(): string {
	return `No Bittune Agent model is selected. ${getBittuneConfigurationHelp()}`;
}

export function formatNoApiKeyFoundMessage(provider: string): string {
	const providerDisplay = provider === UNKNOWN_PROVIDER ? "the selected model" : provider;
	return `No API key is available for ${providerDisplay}. ${getBittuneConfigurationHelp()}`;
}
