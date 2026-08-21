export const BITTUNE_UNAVAILABLE_SLASH_COMMANDS = [
	"/settings",
	"/scoped-models",
	"/export",
	"/import",
	"/share",
	"/changelog",
	"/trust",
	"/model",
	"/login",
	"/logout",
	"/reload",
	"/debug",
	"/arminsayshi",
	"/dementedelves",
] as const;

export function isBittuneUnavailableSlashCommand(text: string): boolean {
	return BITTUNE_UNAVAILABLE_SLASH_COMMANDS.some(
		(command) => text === command || text.startsWith(`${command} `),
	);
}
