# {{PROJECT_NAME}} factory

The settings of a Slop factory project (ADR-0016). The engine reads only this
repository's files and falls back to its own for the rest.

- `factory.project.yaml`: repositories, tracker, models, environments, and the
  states and commands with the label each is stored in.
- `workflows/delivery.yaml`: the nodes, naming only states, commands and events.
- `knowledge/`: what the retro and distill roles learn about this project.
- `roles/<name>/` or `skills/<name>/`: add one only to replace the engine's file
  at the same path.

Check the settings with `factory validate --team .`. After an engine upgrade,
`factory upgrade --diff --team .` shows what changed in the engine's template
since the `engine` version above; `factory upgrade --apply PATH --team .` takes
one file.
