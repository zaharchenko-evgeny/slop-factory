# Factory progress UI

A read-only React and shadcn/ui app. Layout: prototype B, full canvas (compact header and toolbar, no permanent sidebar).
Prototypes A and C stay under `.factory/ui-prototype`.

## Commands

- `npm --prefix ui ci`: install.
- `npm --prefix ui test`: component tests.
- `npm --prefix ui run dev`: dev server. It serves `GET /api/snapshot` from `ui/dev-stub/snapshot.json`, an S1 snapshot
  (`factory/ui/snapshot.py`), until `factory ui` (S2) serves the real one.
- `npm --prefix ui run build`: build the bundle into `factory/ui/assets/`. Commit the result. File names have no hash,
  and `factory/tests/ui/test_ui_bundle_in_sync.py` fails when the committed bundle differs from a fresh build.

## Register a view

A view is a component that reads only the snapshot it receives. It never fetches or writes. The Python side has the
same interface (`factory/ui/views.py`).

```tsx
import { registerView } from "@/views/registry";

registerView({ id: "by-repo", title: "By repository", component: ({ snapshot }) => <p>{snapshot.tickets.length}</p> });
```

`id` is a unique lowercase slug. `default: true` marks the first view shown (one only). The app lists the registry, so a
new view needs no edit of the built-in views (`src/views/builtin.tsx`), `App.tsx` or the tracker adapters. The built-in
views `workflow` (default) and `columns` register through this same call; they are empty placeholders for now.
