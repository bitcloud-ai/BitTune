# Host Runner

`autopilot-runner` is a separate systemd service. FastAPI is served by
Uvicorn over `/opt/autopilot/runtime/runner.sock`; it does not expose TCP,
raw Shell, process arguments, Docker options, arbitrary environment maps,
volume maps, or host paths.

The HTTP surface is rooted at `/runner/v1` and contains only the typed
environment, deployment, benchmark, Job status, and Artifact routes defined
by the architecture document. Uvicorn provides the HTTP/UDS transport; the
Runner does not maintain a second framing protocol.

Production deployment must:

- install `runner/systemd/autopilot-runner.service` and configure immutable
  image digests in a deployment-specific drop-in after G0 verification;
- put the Worker in the dedicated group that can access `runner.sock`; the API
  container must not mount the socket;
- provide credentials with `LoadCredential=` and pass only logical `SecretRef`
  values in typed requests;
- register only the four logical roots: `model-cache`, `output`, `temporary`,
  and `runtime`;
- run the Docker/Linux/systemd contract and integration tests on the target
  Linux host.

The Windows development environment runs only model, lease, path, and Fake
Adapter tests. Real UDS, systemd, Docker, and GPU validation remain a Linux
acceptance task.
