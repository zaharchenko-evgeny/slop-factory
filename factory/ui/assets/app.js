// Placeholder bundle: the frontend slice of #1370 replaces these assets. Read-only: it only issues GET requests.
fetch("/api/snapshot")
  .then((r) => r.json())
  .then((snap) => {
    document.getElementById("status").textContent = `${snap.tickets.length} tickets, ${snap.errors.length} source errors`;
    document.getElementById("snapshot").textContent = JSON.stringify(snap, null, 2);
  })
  .catch((e) => { document.getElementById("status").textContent = `Could not load the snapshot: ${e}`; });
