import { Type } from "typebox";
import { BittuneError } from "../../shared/errors.ts";
import { requireSuccess, runCommand } from "../../shared/process.ts";
import { RunRecorder } from "../../shared/run-recorder.ts";
import { createBittuneTool } from "../../shared/tool.ts";

const Empty = Type.Object({});
const MIB = 1024 * 1024;

function parseKeyValue(text: string): Record<string, string> {
  return Object.fromEntries(text.split(/\r?\n/).map((line) => line.split("=", 2)).filter(([key]) => Boolean(key)).map(([key, value]) => [key!, value?.replace(/^"|"$/g, "") ?? ""]));
}

export function createEnvironmentTools(recorder: RunRecorder) {
  return [
    createBittuneTool({
      name: "inspect_gpu", label: "检测 GPU", parameters: Empty, recorder,
      description: "测量当前 NVIDIA GPU、Driver、Compute Capability、显存、利用率、功耗和占用进程。仅在需要当前 GPU 事实时使用；不启动容器、不部署或压测。返回 measured Observation。",
      async execute(_params, context) {
        context.update("正在读取 NVIDIA GPU 状态…");
        const gpu = requireSuccess(await runCommand("nvidia-smi", ["--query-gpu=index,uuid,name,driver_version,compute_cap,memory.total,memory.free,utilization.gpu,power.draw", "--format=csv,noheader,nounits"], { signal: context.signal, timeoutMs: 10_000 }));
        const processes = await runCommand("nvidia-smi", ["--query-compute-apps=gpu_uuid,pid,process_name,used_memory", "--format=csv,noheader,nounits"], { signal: context.signal, timeoutMs: 10_000 });
        await context.artifact("nvidia-smi-gpu", gpu.stdout);
        if (processes.stdout || processes.stderr) await context.artifact("nvidia-smi-processes", `${processes.stdout}\n${processes.stderr}`);
        const gpus = gpu.stdout.trim().split(/\r?\n/).filter(Boolean).map((line) => {
          const [index, uuid, name, driverVersion, computeCapability, total, free, utilization, power] = line.split(",").map((value) => value.trim());
          return { index: Number(index), uuid, name, driver_version: driverVersion, compute_capability: computeCapability, memory_total_bytes: Number(total) * MIB, memory_free_bytes: Number(free) * MIB, utilization_percent: Number(utilization), power_watts: Number(power) };
        });
        if (!gpus.length) throw new BittuneError("gpu_not_found", "nvidia-smi 未检测到可用 NVIDIA GPU。", false);
        const computeProcesses = processes.exit_code === 0 ? processes.stdout.trim().split(/\r?\n/).filter(Boolean).map((line) => {
          const [gpuUuid, pid, processName, memory] = line.split(",").map((value) => value.trim());
          return { gpu_uuid: gpuUuid, pid: Number(pid), process_name: processName, memory_bytes: Number(memory) * MIB };
        }) : [];
        return { summary: `检测到 ${gpus.length} 张 NVIDIA GPU；第一张为 ${gpus[0]!.name}，可用显存 ${(gpus[0]!.memory_free_bytes / 1024 ** 3).toFixed(1)} GiB。`, provenance_type: "measured", data: { host_id: gpus.map((item) => item.uuid).join(","), gpus, compute_processes: computeProcesses }, provider: { name: "nvidia-smi" } };
      },
    }),
    createBittuneTool({
      name: "inspect_linux_host", label: "检测 Linux 主机", parameters: Empty, recorder,
      description: "测量当前 Linux 主机的 OS、Kernel、CPU、内存、根文件系统空间和架构。仅在需要实时宿主机事实时使用；不改变系统状态。",
      async execute(_params, context) {
        context.update("正在读取 Linux 主机状态…");
        const [uname, cpu, memory, disk] = await Promise.all([
          runCommand("uname", ["-srm"], { signal: context.signal, timeoutMs: 10_000 }),
          runCommand("lscpu", ["-J"], { signal: context.signal, timeoutMs: 10_000 }),
          runCommand("free", ["-b"], { signal: context.signal, timeoutMs: 10_000 }),
          runCommand("df", ["-B1", "/"], { signal: context.signal, timeoutMs: 10_000 }),
        ]);
        for (const item of [uname, cpu, memory, disk]) requireSuccess(item);
        const osRelease = await runCommand("cat", ["/etc/os-release"], { signal: context.signal, timeoutMs: 10_000 });
        const raw = [uname, cpu, memory, disk, osRelease].map((item) => `$ ${item.command} ${item.args.join(" ")}\n${item.stdout}\n${item.stderr}`).join("\n");
        await context.artifact("linux-host", raw);
        const cpuJson = JSON.parse(cpu.stdout) as { lscpu?: Array<{ field: string; data: string }> };
        const cpuData = Object.fromEntries((cpuJson.lscpu ?? []).map((item) => [item.field.replace(/:$/, ""), item.data]));
        const memoryLine = memory.stdout.split(/\r?\n/).find((line) => line.startsWith("Mem:"))?.trim().split(/\s+/) ?? [];
        const diskLine = disk.stdout.trim().split(/\r?\n/).at(-1)?.trim().split(/\s+/) ?? [];
        return { summary: `主机 ${uname.stdout.trim()}，内存 ${(Number(memoryLine[1]) / 1024 ** 3).toFixed(1)} GiB。`, provenance_type: "measured", data: { os: osRelease.exit_code === 0 ? parseKeyValue(osRelease.stdout) : { id: "unknown" }, kernel: uname.stdout.trim(), cpu: cpuData, memory: { total_bytes: Number(memoryLine[1] ?? 0), available_bytes: Number(memoryLine[6] ?? 0) }, root_disk: { total_bytes: Number(diskLine[1] ?? 0), available_bytes: Number(diskLine[3] ?? 0) } }, provider: { name: "linux-cli" } };
      },
    }),
    createBittuneTool({
      name: "inspect_container_runtime", label: "检测容器运行时", parameters: Empty, recorder,
      description: "只读检测 Docker Client/Daemon 状态与已注册的 NVIDIA Container Runtime。用于部署前判断容器运行环境；不启动临时容器、不拉取镜像。",
      async execute(_params, context) {
        context.update("正在读取 Docker Runtime 状态…");
        const [version, info] = await Promise.all([
          runCommand("docker", ["version", "--format", "{{json .}}"], { signal: context.signal, timeoutMs: 10_000 }),
          runCommand("docker", ["info", "--format", "{{json .}}"], { signal: context.signal, timeoutMs: 10_000 }),
        ]);
        if (version.exit_code !== 0 || info.exit_code !== 0) {
          await context.artifact("docker-runtime-error", `${version.stderr}\n${info.stderr}`);
          throw new BittuneError("container_runtime_unavailable", "Docker daemon 不可用或当前用户无权访问。", true);
        }
        await context.artifact("docker-runtime", `${version.stdout}\n${info.stdout}`, "application/json");
        const versionData = JSON.parse(version.stdout) as Record<string, unknown>;
        const infoData = JSON.parse(info.stdout) as { Runtimes?: Record<string, unknown>; Driver?: string; ServerVersion?: string };
        const runtimes = Object.keys(infoData.Runtimes ?? {});
        return { summary: `Docker daemon 可用（${infoData.ServerVersion ?? "版本未知"}）；NVIDIA runtime ${runtimes.includes("nvidia") ? "已注册" : "未注册"}。`, provenance_type: "measured", data: { docker_version: versionData, daemon: { server_version: infoData.ServerVersion, storage_driver: infoData.Driver, runtimes, nvidia_runtime_registered: runtimes.includes("nvidia") } }, provider: { name: "docker-cli" } };
      },
    }),
  ];
}
