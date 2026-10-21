---
status: proposed
date: 2026-10-21
decision-makers: Akhil
---

# Host the image engine behind a fail-closed launcher unit

## Context and problem statement

aihub's chat is to make images on this PC with stable-diffusion.cpp's `sd-server` (plan: `~/.claude/plans/aihub-image-generation-design.md`, phases P1 to P5). This record covers P1, the host that runs the engine, built and tested before the engine itself. The engine needs the GPU, which aihub-web's sandbox does not allow (`PrivateDevices`, `MemoryDenyWriteExecute`), and it must have no network: `sd-server` has no authentication and, according to the plan's review of its code, reflects the request's Origin. What runs it on a systemd user manager so that it starts on demand, holds at most one model in memory, and can be reached only through a private socket?

## Decision drivers

- The engine has no network, and the host refuses to run if that stops being true.
- One engine at a time: the lab measured 7 to 7.6 GB of pinned RAM for each loaded engine. Nothing stays resident when idle.
- A start that fails costs one CUDA initialisation per 30 s and never loops.
- Prompts stay out of the persistent journal.
- Client bytes are parsed by one small piece of code, with every size and time bounded.
- It has to work on this PC's systemd 262 user manager, where only the main process of a unit gets `PrivateNetwork`'s namespace.

## Considered options

1. One socket unit and one service whose main process is a Python launcher that runs the engine as its child.
2. Three units per model: a socket, `systemd-socket-proxyd` joined to the engine's namespace with `JoinsNamespaceOf`, and the engine started with an `ExecStartPost` readiness loop.
3. A template unit per model with `Conflicts=` drop-ins for mutual exclusion.
4. `systemd-socket-proxyd` started from a shell wrapper in one unit.
5. `sd-server` on host TCP, `sd-cli` once per image, or aihub-web starting the engine itself.

## Decision outcome

Chosen option: "one socket unit and one service with a launcher" (option 1), because it is the only one that gives routing by model, 410 for a poll when the engine is gone, a 503 with the engine's error text, a kill route and an idle exit in one process, and still fails closed. The launcher is `systemd/images/launcher.py`, the units are `systemd/aihub-images.socket` and `systemd/aihub-images.service`, and `systemd/images/models.json` names the engine and the per-model arguments.

### How the host works

- **Network check.** The launcher exits 78 unless `/sys/class/net` lists only `lo`. In the unit that is true because systemd 262 mounts a second, read-only sysfs over `/sys` for `PrivateNetwork=yes`. `unshare --net` alone does not do that, so the tests run the launcher in `unshare --user --map-root-user --net --mount`, mount sysfs again and bring `lo` up. `socket.if_nameindex()` cannot be used: it needs `AF_NETLINK`, which the unit forbids.
- **Socket.** It exits 78 unless `LISTEN_PID` is its own pid (1 in the unit, because of `PrivatePIDs=yes`), `LISTEN_FDS` is `1`, and fd 3 is a listening unix stream socket. It removes every `LISTEN_*` variable from the engine's environment and does not pass fd 3 on.
- **Host sockets, logs, core dumps and terminals.** `TemporaryFileSystem=/dev/shm /run:ro /var/log:ro /var/lib/systemd/coredump:ro /dev/pts:ro` gives the unit an empty `/run`, `/var/log`, core dump directory and `/dev/pts`. Without them, `PrivateUsers=yes` did not keep a process in the sandbox from connecting to `/run/docker.sock` (`root:docker`, mode 0660; the user is in `docker`, which is root-equivalent) or to the system bus. Read ACLs for the user and for `wheel` let it read the 135 core dumps (the memory of other processes) and 14 journal files that were there on 2026-10-21, and `PrivateDevices=yes` binds the host's devpts, so the user's four open terminals were reachable. A scan of every file, directory, device node, fifo and socket outside `/proc` and `/sys` for ones that the process can reach and the world cannot now finds only the sandbox's own empty `/root` and `/dev/shm`, and six sockets of root's with mode 0600. The launcher's socket reaches it as an inherited descriptor, so it needs nothing in `/run`, and `ProtectHome=tmpfs` already hides `/run/user`. A path directive such as `ReadOnlyPaths=/run` puts the host's `/run` back over the empty one, so `tests/test_images_units.py` fails on any directive it does not know, and `scripts/images/isolation_probe.sh` checks the running unit, drop-ins included.
- **Requests.** One request per connection, `Connection: close`. Head at most 16 KiB, body at most 32 MiB, at most 16 connections in progress, 30 s to send a request. Only `GET /launcher/status`, `POST /launcher/kill`, `POST /e/<model>/sdcpp/v1/img_gen`, `GET /e/<model>/sdcpp/v1/jobs/<id>` and `POST /e/<model>/sdcpp/v1/jobs/<id>/cancel` are routed, and everything else is 404. A model id is `[a-z0-9-]{1,32}` and a job id `[A-Za-z0-9_-]{1,64}`.
- **Forwarding.** The launcher parses the request (ASCII only, CRLF only, no folded lines, one `Content-Length`, no `Transfer-Encoding`) and builds a new one for the engine: the method, the path from the route, `Host`, and for img_gen `Content-Type: application/json` and the body it counted. A client's header never reaches the engine. The engine's reply must have one `Content-Length` of at most 16 MiB, no chunking and a head of at most 16 KiB; anything else is a 502, and a stall of 60 s a 504.
- **Lifecycle.** `GET /launcher/status` never starts an engine. A poll or cancel is forwarded only while the engine for that model is ready, otherwise 410 with the last error lines. A request for another model stops the running engine first (SIGTERM, SIGKILL after 5 s). Never two engines: `sd-server` listens with `SO_REUSEPORT`, so a second one on the port would share it and take half the requests. No engine starts while the previous one has not been seen to exit 10 s after SIGKILL (the state is `failed`, nothing is passed on to it, and `POST /launcher/kill` answers 504 `engine_not_gone`), or while anything accepts connections on the engine's port. An engine that exits or does not listen within 90 s gives a 503, and no new start is attempted for 30 s, for any model. After 300 s with no connection the launcher stops the engine and exits 0, and the socket starts it again.
- **Engine output.** `sd-server` prints `[E]` lines on stderr and `[W]`, `[I]`, `[D]` and `[V]` lines on stdout (`examples/common/log.cpp` at the pin), so the launcher reads both. It keeps the `[E]` and `[W]` lines, the last 20 for the status route and the first 200 per start in its own stderr; the `[I]` lines, which carry prompts, are dropped. `-v`, `--verbose` and `--log-level` in `models.json` make the launcher exit 78.
- **Engine lifetime.** The engine must not outlive the launcher. Under systemd, `KillMode=control-group` (left at its default) ends every process of the unit when the main process exits, and with `PrivatePIDs=yes` the launcher is PID 1, so the kernel ends the namespace with it. Elsewhere the engine is started with `PR_SET_PDEATHSIG`, so a launcher killed with SIGKILL takes it along. Because the launcher is PID 1 it installs SIGTERM and SIGINT handlers: PID 1 ignores a signal that has none, so `systemctl stop` would wait out `TimeoutStopSec` and then kill it.
- **Test timings.** The seven timings above are the defaults of `AIHUB_IMAGES_IDLE_S`, `_BACKOFF_S`, `_START_S`, `_GRACE_S`, `_KILL_S`, `_CLIENT_S` and `_ENGINE_S`. The launcher reads them once at start so that tests do not wait minutes. `tests/test_images_units.py` fails if the unit, the socket or `models.json` mention any of them.
- **Stand-in engine.** `tests/fixtures/images-engine/fake_sd_server.py` speaks the three native routes with the status flow queued, generating (with preview step counts), completed (a real WebP of the requested size) and failed. Keywords in the prompt make a job slow, failing, crashing or noisy, or break the reply. How the start goes (exit 3, abort, hang, slow, ignore SIGTERM, flood) is set by an argument in `models.json`, because a prompt can only arrive once the engine listens.

### Consequences

- Good, because the isolation fails closed in two layers. With the full hardening list, an `ExecStartPre` makes systemd refuse the start (`226/NAMESPACE`) and no launcher runs. When systemd instead proceeds without the network namespace (reproduced with `ExecStartPre` plus `PrivateIPC=no`), the launcher sees the host's interfaces and exits 78. In both cases nothing listens on the host.
- Good, because a failing engine start costs one attempt: five requests to a stand-in that exits 3 gave five 503s, one start in the journal, and no `start-limit-hit`.
- Good, because the units score 2.4 (OK) in `systemd-analyze security`, offline and online, below the 3.0 the plan asks for.
- Bad, because the launcher is about 1,000 lines (the plan expected about 250) on a trust boundary, and it will need the same care as any parser of client bytes.
- Bad, because the check depends on how systemd mounts `/sys`. If a later systemd stops mounting a fresh sysfs, the launcher refuses to start; `/proc/self/net/dev` lists the same interfaces and is the check to switch to.
- Bad, because the unit sets neither `SystemCallFilter` nor `MemoryDenyWriteExecute` (CUDA's JIT and driver ioctls are untested under them). They are 2.1 of the 3.3 that the failed checks add up to in the analyzer's table.
- Bad, because killing the engine to stop a job costs the next image a model load of about 6 s (plan estimate).
- Bad, because no image has been made through the hardened unit yet. A CUDA context can be made inside its sandbox (see the table), but G1a(f), an image through the unit, has not been run.
- Bad, because the cage hides what only the user's identity can reach, not what any local user can read (system files under `/etc` and `/usr`, the cgroup tree under `/sys`).

### Confirmation

`tests/test_images_launcher.py` runs the launcher against the stand-in in a private network namespace and holds it to every behaviour above, with Python 3.12 and 3.14. `tests/test_images_units.py` fails if a planned directive changes, a hook appears, the unit sets a directive the tests do not know (`MemoryDenyWriteExecute` and `SystemCallFilter` are left out on purpose), `systemd-analyze verify` reports an unknown directive, or the offline score passes 3.0. `scripts/images/isolation_probe.sh` checks the running unit from outside and prints PASS or FAIL; run it after any change to the units and after a systemd upgrade.

## Pros and cons of the options

### Three units per model (option 2)

- Good, because each unit is plain systemd.
- Bad, because it does not work on a user manager: only the main process gets the private network namespace, so the proxy lands in the host's and gets "Connection refused", and an `ExecStartPost` or `ExecStartPre` runs outside it. Three reviewers reproduced that. I reproduced the `ExecStartPre` half on the final unit: systemd either refuses the start or proceeds without the namespace (see the table below).

### Template units with `Conflicts=` (option 3)

- Good, because mutual exclusion is systemd's job.
- Bad, because it needs more units and generated drop-ins for the mutual exclusion that `ensure()` in the launcher gives in a few lines, and each unit still needs its own fail-closed wrapper.

### `systemd-socket-proxyd` from a shell wrapper (option 4)

- Good, because it works.
- Bad, because it cannot route by model, answer a poll without starting an engine, return an error body or kill a job.

### Host TCP, `sd-cli` per image, or aihub-web as parent (option 5)

- Bad, because host TCP means an unauthenticated server that reflects Origin, `sd-cli` is 2 to 4 times slower with a CUDA context per image (the lab's figure, in the plan), and aihub-web's sandbox cannot start a GPU process.

## Measured while building it

All on 2026-10-21 with systemd 262.1 (Arch), kernel 7.2.9, Python 3.12.15 and 3.14.8, on the real user manager with copies of the units rendered for the worktree.

| What | Result |
| --- | --- |
| `/sys` in the unit | Two sysfs mounts: the inherited one (device 0:25) and a fresh read-only one (0:68) over it. `/sys/class/net` lists `lo`, and so does `/proc/<pid>/net/dev`. |
| `socket.if_nameindex()` in the unit | `OSError 97`, address family not supported. |
| PID namespace | The launcher is PID 1 and `/proc` lists only it. `LISTEN_PID` is `1`, equal to `getpid()`. |
| PID 1 and SIGTERM | A process without a handler ignores it. A probe that did so ended `failed` after `systemctl --user stop`. |
| User namespace and process | `uid_map` `1000 1000 1`; `CapEff` and `CapBnd` `0`; `NoNewPrivs` `1`; `Seccomp` `2`; core limit 0; `oom_score_adj` 500. |
| `/dev` in the unit | 22 entries: `char core fd full hugepages log mqueue null nvidia-uvm nvidia-uvm-tools nvidia0 nvidiactl ptmx pts random shm stderr stdin stdout tty urandom zero`. |
| `$HOME` in the unit | Only `work`; `.config`, `.ssh`, `.gnupg` and `.claude` are not there. |
| `ExecStartPre=/usr/bin/true` in a drop-in | systemd logs "network namespace setup not permitted, proceeding without", then "Failed to set up mount namespacing: /dev/mqueue: Operation not permitted" and the start ends `226/NAMESPACE`. Three tries give `start-limit-hit`, and the waiting client gets a reset. |
| The same plus `PrivateIPC=no` | systemd proceeds without the network namespace, the launcher lists `br-ebeebde36ec5 br-f6b3131f1e95 br-fc67419f786a docker0 enp6s0 lo wlp0s20f3`, refuses, and the unit ends `status=78/CONFIG`. Port 18234 is not open on the host. |
| A missing `BindReadOnlyPaths` source | The same `226/NAMESPACE` and `start-limit-hit`. |
| A missing working directory (`models`) | `Changing to the requested working directory failed`, `status=200/CHDIR`. A missing `bin` does not stop the unit. |
| The socket file after the socket unit stops | It stays, so aihub sees "connection refused", not "no socket". |
| `systemd-analyze security` | 2.4 OK offline and online. Costs: the eleven `SystemCallFilter` classes 2.0, `AF_INET` 0.3, `ProtectProc` 0.2, `IPAddressDeny` 0.2, and 0.1 each for `AF_UNIX`, `MemoryDenyWriteExecute`, `ProcSubset`, `RootDirectory`, `ProtectHome` and `DeviceAllow`. |
| A stand-in engine that exits with status 3 | Five requests gave five identical 503s with the engine's `[E]` line and `[E] launcher: the engine exited with status 3 before it listened`. The journal has one "starting the engine" line, both units stayed active, `start-limit-hit` did not appear, and the status route reports `failed`. |
| Idle exit | The unit goes inactive by itself 300 s after the last connection (checked twice, the second time at 301 s): no launcher process, no engine process, no cgroup, `Result=success`. The socket stays active. |
| Cold start through the socket | A status request that starts the launcher took 0.118 s, 0.095 s and, after a recovery, 0.094 s. |
| The launcher killed with SIGKILL | With the stand-in engine running, `kill -9` of the launcher's main pid left no engine process within 1.5 s and no cgroup. The unit ended `failed` (`Result=signal`, status 9) and the socket stayed active. |
| Four launcher starts in a minute | I caused this by stopping the service by hand three times in a row. The service ends `start-limit-hit` and the socket `service-start-limit-hit`, so connections are refused. `systemctl --user reset-failed aihub-images.socket aihub-images.service` and `systemctl --user start aihub-images.socket` bring it back. |
| CUDA smoke (G1a f) | **Not passed: the GPU cannot create a CUDA context.** Through the unit, with the lab's `sd-server` at 0518b34, klein 4B at 512 px, both locks held, 5632 MiB free and no Ollama model: the engine started and listened, the job failed with `cudaMalloc failed: CUDA-capable device(s) is/are busy or unavailable`, and the kernel log gained 10 lines, five pairs of `NVRM: GPU0 rpcRmApiAlloc_GSP: GspRmAlloc failed ... hClass=0x0000a06c ... status=0x00000063` and `kchangrpapiConstruct_IMPL: KernelChannelGroupApi alloc RPC to vGpu Host failed`. A control outside any sandbox, the lab's `cuda_probe.py`, failed the same way three minutes later (`cuInit 0`, `cuCtxCreate -> 210`, 2 more lines). That is the wedge in the plan's risk table, back after the plan's 07:52 PDT check, where the probe passed. In the unit's namespaces the four NVIDIA nodes are readable and writable, `cuInit` returns 0 and the device is listed as an RTX 4060, with no kernel errors. Whether the hardening lets a context be made was therefore still open (next rows). |
| Host sockets, logs, core dumps and terminals in the unit (after the reboot, kernel 7.2.11) | In a transient unit with the unit file's sandbox directives and only `/dev/shm` emptied, a process connected to `/run/docker.sock` and `/run/dbus/system_bus_socket`, and `find /run -type s` printed 37 paths and 12 read errors. `access()` also said yes to all 135 core dumps in `/var/lib/systemd/coredump` (a read ACL for the user), to 14 files in `/var/log/journal` (ACLs for the user, `wheel` and `adm`), and to read and write on the four terminals in `/dev/pts` (nothing was read, written or opened). With `TemporaryFileSystem=/dev/shm /run:ro /var/log:ro /var/lib/systemd/coredump:ro /dev/pts:ro` the connections give "No such file or directory", `/dev/pts` is empty, and the scan described above finds only the sandbox's own empty `/root` and `/dev/shm` and six sockets of root's with mode 0600 under `/etc/pacman.d/gnupg`. `/run` then holds only the empty `user` tmpfs that `ProtectHome=tmpfs` mounts. Adding `ReadOnlyPaths=/run` brings the host's `/run` back (37 socket paths, `docker.sock` connects). On the real user manager with rendered copies of the units, the launcher still starts through the socket and the probe prints PASS. A drop-in with `ReadOnlyPaths=/run` makes the probe print FAIL for `/run/docker.sock` and for the 49 socket paths and errors under `/run`. A drop-in that restores the list without `/dev/pts:ro` makes it print FAIL for `/dev/pts` and the loaded `TemporaryFileSystem`, and one that empties only `/run` (tried before `/dev/pts:ro` was added) FAIL for `/var/log`, the core dump directory and the loaded `TemporaryFileSystem`. A probe that cannot look inside the unit prints FAIL for every directory check. |
| CUDA context in the sandbox (after the reboot, kernel 7.2.11) | The calls of `cuda_probe.py` (`cuInit`, `cuCtxCreate`) in a transient unit with the unit file's directives and `BindPaths` for the NVIDIA nodes return 0, with the host's `/run` and with the unit's emptied directories, and the NVRM count in the kernel log stays at 1. The engine itself has not been run through the unit. |

## More information

- Before the first start, `~/work/aihub-images/models` must exist: the unit binds `~/work/aihub-images` read-only (missing: `226/NAMESPACE`) and starts in its `models` directory (missing: `200/CHDIR`). After three failed starts in a minute the units stop with `start-limit-hit`; `systemctl --user reset-failed aihub-images.socket aihub-images.service` clears that. With `models` there and no `bin/sd-server` yet, the launcher answers an image request with a 503 that names the missing file. The unit also empties `/var/log`, `/var/lib/systemd/coredump` and `/dev/pts`; a missing one of those ends the start with `226/NAMESPACE` too (systemd-tmpfiles recreates the core dump directory at boot: `d /var/lib/systemd/coredump` in `/usr/lib/tmpfiles.d/systemd.conf`).
- G1a(f) still has to be run. After the reboot on 2026-10-21 the GPU makes a context (`python3 -I ~/work/aihub-image-lab/comfy/tools/cuda_probe.py` exits 0 and `journalctl -k -b | grep -c NVRM` stays at 1), but the wedge came and went before, so check both again first. The recipe: link copies of the units with the paths of the checkout (and of a scratch directory for `%h/work/aihub-images`); add a drop-in that adds `BindReadOnlyPaths=` for the lab's `sdcpp/build-cuda/bin` and `models` and points `ExecStart` at a `models.json` whose `engine` is the lab's `sd-server`; take `flock` on the lab's `gpu.lock` and on `$XDG_RUNTIME_DIR/aihub-images`; post one klein 4B 512 px job and poll it; then `POST /launcher/kill`, remove the drop-in, and compare the NVRM counts before and after. If a context still fails, add `/dev/nvidia-modeset`, then `/dev/dri/renderD128`, one at a time to `BindPaths=` and note which one mattered.
- sd-server API at the pin: `git show 1d0a58a:examples/server/api.md` in `~/work/aihub-image-lab/sdcpp/src`.
- Revisit when systemd is upgraded (run the probe), when the CUDA major version or gcc changes (the engine is rebuilt), or if a launcher bug makes the start limit trip.
- Not in this record: the chat database, the web backend and the UI (phases P2 and P3), and the engine build and its gates (P4).
