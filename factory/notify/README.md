# Notifier adapters (ADR-0022)

## slack-workflow

The adapter sends Slack DMs through a Slack Workflow Builder webhook. It cannot take replies. Each message ends with
`Answer on GitHub: <issue or PR url>`. The adapter has no `ack`.

Create the workflow once, as any workspace member:

1. In Slack, create a workflow in Workflow Builder. Choose **Starts with a webhook**.
2. Add these variables, all text except `recipient`:
   - `recipient`: type **Slack user email**
   - `title`, `text`, `issue_url`, `run_url`
3. Add the step **Send a message to** `recipient`. Put the variables in the message.
4. Publish the workflow. Copy its webhook URL into the secret `SLACK_WORKFLOW_URL`. The URL is a secret: the factory never prints it.

Team `factory.yaml`:

```yaml
notify:
  adapters:
    slack-workflow: {url_env: SLACK_WORKFLOW_URL}
  people:
    <github-login>:
      via: [slack-workflow]
      slack-workflow: <Slack user email>
```

A non-2xx answer fails the send with its status code. `run_url` is the `Factory-Run:` value of the latest `[factory]`
stage comment, else the notify run.
