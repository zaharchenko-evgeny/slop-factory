# {{PROJECT_NAME}} factory

The definition of this team's software factory (ADR-0021 of the engine).

- `factory.yaml`: what the factory has: tracker, models, skills, roles, agents, environments, webhooks.
- `workflow.yaml`: how it works: stages, shared settings, and the nodes in delivery order.
- `roles/<role>/role.md`: each role's instructions (Jinja, rendered with the node's settings);
  `roles/<role>/setup.sh` installs the role's tools.
- `.air/cloud/startup.sh`: the Air startup script; it runs `factory setup`.

Check a change with `factory validate --team .`; deploy with the engine's `factory-deploy` skill.
