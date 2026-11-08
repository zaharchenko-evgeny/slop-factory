const entities = require('@jetbrains/youtrack-scripting-api/entities');

exports.rule = entities.Issue.onChange({
  title: 'Add factory:comment when a user comments',
  guard: ctx => ctx.issue.comments.added.isNotEmpty(),
  action: ctx => {
    const added = ctx.issue.comments.added;
    let hasUserComment = false;
    for (let i = 0; i < added.size; i++) {
      const body = added.get(i).text || '';
      if (body.indexOf('<!-- factory:from ') >= 0 && body.indexOf('<!-- factory:ask ') < 0) {
        continue;
      }
      hasUserComment = true;
    }
    if (hasUserComment) ctx.issue.addTag('factory:comment');
  },
  requirements: {}
});
