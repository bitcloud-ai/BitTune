import { execFileSync } from "node:child_process";
import { cp, mkdir, readFile, rm } from "node:fs/promises";
import { join } from "node:path";

const packageJson = JSON.parse(await readFile("package.json", "utf8"));
const distribution = "release";
await rm(distribution, { recursive: true, force: true });
await mkdir(distribution, { recursive: true });
const npmCli = process.env.npm_execpath;
const npmInvocation = npmCli
  ? [process.execPath, [npmCli, "pack", "--pack-destination", distribution, "--json"]]
  : [process.platform === "win32" ? "npm.cmd" : "npm", ["pack", "--pack-destination", distribution, "--json"]];
const packed = JSON.parse(
  execFileSync(npmInvocation[0], npmInvocation[1], {
    encoding: "utf8",
    // Windows exposes npm as a .cmd wrapper, which requires a command shell
    // when this release script is invoked directly instead of through npm run.
    shell: process.platform === "win32" && !npmCli,
  }),
)[0];
const releaseName = `bittune-installer-${packageJson.version}`;
const stage = join(distribution, releaseName);
await mkdir(stage, { recursive: true });
await cp("install/install-ubuntu.sh", join(stage, "install-ubuntu.sh"));
await cp("install/build-offline-bundle-ubuntu.sh", join(stage, "build-offline-bundle-ubuntu.sh"));
await cp("install/offline-install-ubuntu.sh", join(stage, "offline-install-ubuntu.sh"));
await cp("install/offline-manifest.env", join(stage, "offline-manifest.env"));
await cp("install/requirements.txt", join(stage, "requirements.txt"));
await cp(join(distribution, packed.filename), join(stage, packed.filename));
await cp("README.md", join(stage, "README.md"));
execFileSync(process.execPath, ["scripts/generate-sbom.mjs", join(stage, "bittune-sbom.cdx.json")], {
  stdio: "inherit",
});
execFileSync("tar", ["-czf", `${releaseName}.tar.gz`, releaseName], { cwd: distribution, stdio: "inherit" });
console.log(join(distribution, `${releaseName}.tar.gz`));
