# Personal build server

The server accepts YAML over HTTP and runs the existing build runner in the
background. No new Python packages are required beyond `requirements.txt`.
Deployment as an LXC, Quadlet, or other service is left open for now.

## Start the server

Copy this program directory to the server and install its requirements in a
Python 3.10+ environment. Provision a local Bluefin Git checkout and the tools
required by its build recipes: Git, Just, Podman, and the other dependencies
you use for a working local build (including its registry inspection and
signature verification tools). Run the server as the same user who will build
images and has logged into the destination registry with Podman.

For an initial test from `ublue-os`, use the existing environment:

```sh
.direnv/python-3.14/bin/python bluefin-build-editor/server.py \
  --repo ./bluefin --build-dir ./tmp --jobs-dir ./build-jobs
```

It binds to `127.0.0.1:8080` by default. To listen on your trusted network or
Tailscale address, specify `--host ADDRESS` and optionally `--port PORT`.
This personal service intentionally uses HTTP without authentication; access
control is provided by your VPN or trusted network as requested.

On a separate machine, an example command is:

```sh
python /srv/bluefin-build-editor/server.py \
  --host 100.64.0.10 --port 8080 \
  --repo /srv/bluefin --build-dir /srv/build-checkouts \
  --jobs-dir /srv/build-jobs --fetch \
  --public-url http://100.64.0.10:8080
```

The address above is illustrative; use your server's address. `--fetch` fetches
all configured repository remotes before each build. Configure the remotes you
need, including forks whose commits you build. Submitted hashes must be present
in the server's checkout or reachable through those configured remotes. The
server does not upload source files or automatically add fork remotes.

Keep the jobs directory, build checkouts, Podman storage, repository, and login
credentials persistent when you choose a deployment method.

## Submit from the local TUI

```sh
just build-editor --server http://100.64.0.10:8080
```

This adds **Submit to server** below the local **Build and push** button.
Submission saves the current job and sends YAML to the server. It resolves
`HEAD` or a branch locally to a full commit hash before sending, so the server
builds the exact revision selected on your machine. Your saved YAML retains
the revision you entered. Set `BLUEFIN_BUILD_SERVER` instead of passing
`--server` if preferred.

For command-line submission:

```sh
just submit-job --server http://100.64.0.10:8080
```

It prints a job ID plus status and log URLs. A submission timeout can occur
after the server has accepted the job: inspect `/jobs` before retrying.

## HTTP interface and troubleshooting

```sh
curl -i -H 'Content-Type: application/yaml' \
  --data-binary @bluefin-build.yml http://100.64.0.10:8080/jobs
```

`POST /jobs` returns **202 Accepted** with a JSON job ID, current state, and
relative `status_url` and `log_url`. The `Location` header points to the status
endpoint. Invalid jobs return 400, oversized/empty bodies return 413.

Raw YAML submissions containing a branch or `HEAD` resolve against the
server's current checkout when accepted. Use a full hash or the Python
submission client to preserve your local revision. Full hashes are accepted
before fetching; an unavailable hash will fail during the build and appear
in the job log.

| Endpoint | Result |
| --- | --- |
| `GET /jobs` | All retained jobs, oldest first |
| `GET /jobs/JOB_ID` | State, timestamps, revision, error, notification result |
| `GET /jobs/JOB_ID/log` | Build stdout and stderr, including output so far |

Open the log URL in a browser, or use:

```sh
curl http://100.64.0.10:8080/jobs/JOB_ID
curl http://100.64.0.10:8080/jobs/JOB_ID/log
```

The log endpoint returns a snapshot; request it again for newer output. Jobs
move through `queued`, `running`, and `succeeded`, `failed`, or `interrupted`.
Each job has a directory containing `bluefin-build.yml`, `status.json`, and
`build.log`. Logs include retained checkout paths and publication digests.

A single worker builds jobs sequentially. The existing builder lock also
prevents overlap with the shell build helper. Queued jobs survive a restart.
Graceful shutdown stops the current process group and marks the job interrupted.
After an abrupt stop, a previously running job is marked interrupted on restart
and is not automatically retried; inspect its log and any remaining build
processes before resubmitting. Earlier pushes may already have completed.
Notification attempts are recorded but not automatically retried.

## Pushover notifications

Set these in the environment of the local builder or server:

```sh
export PUSHOVER_APP_TOKEN='your-application-token'
export PUSHOVER_USER_KEY='your-user-key'
```

Register an application to obtain its token and use your Pushover user key.
The implementation follows the [Pushover message API](https://pushover.net/api).
Credentials are kept out of job YAML and status responses.

The local `build.py` command (including the TUI local build) sends a notification
on success, failure, or interruption. Dry runs do not notify; `--no-notify`
disables notifications for an individual command. When running through the
server, the server sends one completion notification with the job ID and log
path. `--public-url` adds a clickable status link to that notification.

With neither credential set, notifications are disabled. A notification failure
does not change a successful build to a failed build: check the `notification`
field in the job status, or the local command's Pushover output. No real Pushover
messages are sent by the automated tests.
