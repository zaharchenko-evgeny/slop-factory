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

## Air startup script

`.air/cloud/startup.sh` is the `startup` of every environment. On each launch it
installs pinned `uv` and `gh` (version and SHA-256), the engine at the `engine`
ref from `engine_repository`, and the `factory` wrapper. It then installs the
skills of the roles that `factory env roles "$FACTORY_ENV"` prints, plus
`factory-step`, runs each role's `setup.sh`, and runs `environments/setup.sh`
and `environments/$FACTORY_ENV/setup.sh` when present. It installs nothing
specific to one team: put team tools in `environments/<env>/setup.sh`.
The script is an example the factory-setup skill copies into a new team
repository; the engine never runs it and a team owns and edits its copy. A role
file or skill missing from the team repository makes it exit 1.
`FACTORY_DRY=1` prints the skills it would install and exits.
