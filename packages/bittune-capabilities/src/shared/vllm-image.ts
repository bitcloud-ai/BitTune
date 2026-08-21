import { runCommand } from "./process.ts";

interface DockerImage {
  Id?: string;
  RepoDigests?: string[];
  Size?: number;
  Created?: string;
  Descriptor?: { digest?: string; annotations?: Record<string, string> };
  Identity?: { Pull?: Array<{ Repository?: string }> };
}

export interface ResolvedVllmImage {
  image_id: string;
  image_reference: string;
  run_reference: string;
  repo_digests: string[];
  size_bytes?: number;
  created_at?: string;
  source: "repo_digest" | "offline_image_id";
}

export interface VllmImageResolution {
  image?: ResolvedVllmImage;
  diagnostics: string;
}

function repositoryMatches(image: DockerImage, repository: string): boolean {
  const expected = new Set([repository, `docker.io/${repository}`, `index.docker.io/${repository}`]);
  const pulls = image.Identity?.Pull ?? [];
  if (pulls.some((item) => item.Repository && expected.has(item.Repository))) return true;
  const source = image.Descriptor?.annotations?.["containerd.io/distribution.source.docker.io"];
  return source === repository;
}

function resolved(image: DockerImage, reference: string, source: ResolvedVllmImage["source"]): ResolvedVllmImage | undefined {
  if (!image.Id) return undefined;
  return {
    image_id: image.Id,
    image_reference: reference,
    run_reference: source === "repo_digest" ? reference : image.Id,
    repo_digests: image.RepoDigests ?? [],
    ...(image.Size === undefined ? {} : { size_bytes: image.Size }),
    ...(image.Created === undefined ? {} : { created_at: image.Created }),
    source,
  };
}

export async function resolveVllmImage(input: { repository: string; digest: string }, signal?: AbortSignal): Promise<VllmImageResolution> {
  const reference = `${input.repository}@${input.digest}`;
  const direct = await runCommand("docker", ["image", "inspect", reference, "--format", "{{json .}}"], { signal, timeoutMs: 15_000 });
  if (direct.exit_code === 0) {
    const image = resolved(JSON.parse(direct.stdout) as DockerImage, reference, "repo_digest");
    return image ? { image, diagnostics: `${direct.stdout}\n${direct.stderr}` } : { diagnostics: `${direct.stdout}\n${direct.stderr}` };
  }

  // docker load retains OCI descriptor identity but may drop RepoTags and RepoDigests.
  const loaded = await runCommand("docker", ["image", "inspect", input.digest, "--format", "{{json .}}"], { signal, timeoutMs: 15_000 });
  const diagnostics = `${direct.stdout}\n${direct.stderr}\n${loaded.stdout}\n${loaded.stderr}`;
  if (loaded.exit_code !== 0) return { diagnostics };
  const image = JSON.parse(loaded.stdout) as DockerImage;
  if (image.Descriptor?.digest !== input.digest || !repositoryMatches(image, input.repository)) return { diagnostics };
  const resolvedImage = resolved(image, reference, "offline_image_id");
  return resolvedImage ? { image: resolvedImage, diagnostics } : { diagnostics };
}
