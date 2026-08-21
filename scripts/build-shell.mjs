import { build } from "esbuild";
import { cp, mkdir, rm } from "node:fs/promises";

await rm("dist", { recursive: true, force: true });
await mkdir("dist", { recursive: true });
await build({
  entryPoints: ["packages/bittune-runtime/src/bittune.ts"],
  outfile: "dist/bittune.js",
  bundle: true,
  format: "esm",
  platform: "node",
  target: "node22",
  packages: "external",
  banner: { js: "#!/usr/bin/env node" },
  legalComments: "inline",
});
await mkdir("dist/modes/interactive/theme", { recursive: true });
await Promise.all(
	["dark.json", "light.json"].map((name) =>
		cp(`packages/bittune-runtime/src/modes/interactive/theme/${name}`, `dist/modes/interactive/theme/${name}`),
	),
);
