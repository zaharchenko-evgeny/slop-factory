Deliver one notification for the ticket or webhook payload in the trigger.
{% if settings.provider == "github-inbox" %}
Add it to the pinned issue `{{ settings.inbox_title }}` and assign
`{{ settings.github_user }}`. Write one line per notification; never edit older lines.
{% endif %}
Report `notified`.
