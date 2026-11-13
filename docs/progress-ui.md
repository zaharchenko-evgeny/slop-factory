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
view and the human-attention counts. **Ticket visibility** defaults to **All
tickets**. **Unfinished** hides closed tickets and configured final stages.
**Active only** also hides the `draft` stage. Both filtered modes keep waiting
and unknown stages. The UI shows a ticket's actual workflow stage and its open
or closed state separately. A closed ticket remains visible in **All tickets**,
even if its workflow stage is not final.

Register each view with `registerProgressView` from
`ui/src/views/registry.tsx`. Follow `ui/src/views/builtins.tsx` for an example.
Each registration needs a unique `id`, a label, a description and a component.
The component receives the snapshot, filtered tickets, ticket selection action
and selected ticket key.

The Workflow view shows configured stages in a pan and zoom graph. The default
Focused neighborhood shows the selected stage and only the endpoints of its
direct incoming and outgoing declared routes. Use **Inspect stage** or a stage
heading button to change the selected stage. If the neighborhood is too wide to
read, the graph centers the selected stage and keeps its neighbors available by
pan and zoom. Use **Full workflow** to show every declared stage and route in a
left-to-right graph. The inspector stays focused on the selected stage in both
modes and lists only its direct route evidence, filtered tickets, and execution
nodes. Dashed amber edges mark a route back to an earlier stage or a self-loop.
Each stage node shows at most two compact ticket previews; use **+N more** to
inspect all filtered tickets for that stage. The Columns view remains a
horizontal ticket board. A ticket in an undeclared stage gets an unconnected
`Unknown stage: <id>` node. The graph does not infer routes from stage order.

Eligibility is a projection from available configuration and event data, not a
record that a node ran. Missing condition facts and missing event evidence
appear as unknown. Missing event evidence becomes not matched only when a
completed local poll covers supported event types. Manual, scheduled, webhook,
unsupported and incomplete sources remain unknown. Execution is always
unknown. Unknown times, node data, nullable links, and empty CI, head or PR
state keep an explicit unknown or unavailable label. Only HTTP and HTTPS URLs
become links. If a PR read fails, ticket details warn that PR evidence may be
stale; an empty PR list then does not confirm that no PR exists. Do not present
unknown as a positive or negative result.

## Snapshot loading and refresh

The browser starts a background read with `GET /api/snapshot?refresh=1` when it
opens, and when the user or auto refresh asks for new data. The server returns
the last cached snapshot and the refresh status at once. The browser shows that
snapshot while the server reads sources. If no cached snapshot exists, the page
shows source progress until it can show data.

The browser reads `GET /api/status` while the requested run is active. When the
server reports a snapshot for that run, the browser reads `/api/snapshot`
without `refresh=1` and replaces the cached data. A snapshot read without
`refresh=1` never starts a new read. Add `older=1` to these requests to use the
older snapshot scope.

The progress bar counts sources marked complete. It does not show an estimate
or elapsed-time percentage. Each source row shows its current step and ticket
counts when the server knows them. A completed source with errors shows a
warning count. If the source list has not arrived, the progress bar stays
indeterminate and the page does not show a ticket count.

If the first snapshot cannot load, the page shows an error and a retry action.
If a later refresh fails, the page keeps the last available snapshot and shows
an error with a retry action. An older-snapshot load keeps the current view in
place until older data is available. If an older snapshot cannot load, the page
shows an inline error and a retry action. Do not present a missing field or
source error as an empty, complete result.

## Startup

Starting the UI is always an explicit user action. Do not add `factory ui` to
local setup, an Air startup script, or an Air environment setup script. The UI
does not make changes to tickets, labels, repositories, or factory files.
