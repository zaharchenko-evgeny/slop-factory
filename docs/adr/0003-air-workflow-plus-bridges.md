# ADR-0003: Air Workflow for the loop, automations as event bridges

Status: accepted (28 Oct 2026)

Options: (a) chain automations through tracker tags, as JetFlow does; (b) Air Workflow for the per-ticket loop, automations only for events.

Decision: (b). The implement → review → fix → approve → publish loop is an Air Workflow; its state lives in the instance. Automations start it and react to PR review, merge and schedules. Locally the same definition is a skill.

Consequence: the workflow definition must be renderable both as an Air graph and as a local skill. Open check: workflows inside a project.
