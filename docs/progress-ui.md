# Progress UI

The progress UI is a local, read-only browser for the factory state. It reads
the selected team's files and opens a page on your computer. It does not start
when you install or set up a local or Air factory.

## Build and install

The published Python wheel contains the built browser files. A user who
installs the wheel does not need Node.js or npm.

To rebuild the browser files from this repository, run:

```sh
cd ui
npm ci
npm run build
cd ..
uv build --wheel
```

The build writes the browser files to `factory/factory_ui/static/`. Then
install the wheel artifact from the repository root:

```sh
uv tool install --force dist/slop_factory-0.1.0-py3-none-any.whl
```

When making a source distribution, include `ui/` source and lockfiles and the
built files in `factory/factory_ui/static/`. Do not include `ui/node_modules/`.

## Start the browser

Run the command when you want to inspect a team's progress:

```sh
factory ui --team /path/to/team
```

The server listens on localhost port 8765 by default and opens the browser.
This fixed port keeps browser preferences at the same origin across launches.
Use `--no-open` to keep the browser closed or `--port N` to select another
fixed port, for example when you run two team browsers at once:

```sh
factory ui --team /path/to/team --no-open --port 8765
```

Browser preferences are scoped to the site origin. Use `--port 0` only when
you want the server to choose an unused port; its changing origin keeps
preferences separate from those saved at port 8765 and may change each launch.

Automatic refresh is off by default. Turn on **Auto** to refresh every minute,
or select an interval of 15 seconds, 30 seconds, one minute, or five minutes.
Select **Off** to stop automatic refresh. Use the refresh button at any time to
read new state. The page shows the snapshot time and the last successful source
sync.

## Views and state

Filters are saved with the selected view and apply to every registered progress
view and the human-attention counts. **Active only** is off by default. When
enabled, it hides the `draft` stage and all configured final stages. It keeps
waiting and unknown stages. **Hide completed** remains a separate filter that
hides configured final stages only.

Register each view with `registerProgressView` from
`ui/src/views/registry.tsx`. Follow `ui/src/views/builtins.tsx` for an example.
Each registration needs a unique `id`, a label, a description and a component.
The component receives the snapshot, filtered tickets, ticket selection action
and selected ticket key.

The graph shows declared stages and nodes. Unknown stage references do not
create configured graph stages or edges. Outcome destinations appear as
outcome targets. A ticket in an undeclared stage gets an `Unknown stage: <id>`
lane. Eligibility is a projection from available configuration and event data,
not a record that a node ran. Missing condition facts and missing event
evidence appear as unknown. Missing event evidence becomes not matched only
when a completed local poll covers supported event types. Manual, scheduled,
webhook, unsupported and incomplete sources remain unknown. Execution is
always unknown. Unknown times, node data, nullable links, and empty CI, head or
PR state keep an explicit unknown or unavailable label. Only HTTP and HTTPS
URLs become links. If a PR read fails, ticket details warn that PR evidence may
be stale; an empty PR list then does not confirm that no PR exists. Do not
present unknown as a positive or negative result.

If the first snapshot cannot load, the page shows an error and a retry action.
If a source fails during a snapshot refresh, the page keeps the last available
snapshot and shows a warning with the source error. An unavailable older
snapshot shows an inline error and a retry action. If a later snapshot request
fails, the page keeps the previous snapshot and shows a refresh error. Do not
present a missing field or source error as an empty, complete result.

## Startup

Starting the UI is always an explicit user action. Do not add `factory ui` to
local setup, an Air startup script, or an Air environment setup script. The UI
does not make changes to tickets, labels, repositories, or factory files.
