const assert = require('node:assert/strict');
const Module = require('node:module');
const originalLoad = Module._load;
let rule;
Module._load = function (request, parent, isMain) {
  if (request === '@jetbrains/youtrack-scripting-api/entities') {
    return { Issue: { onChange: value => (rule = value) } };
  }
  return originalLoad.call(this, request, parent, isMain);
};
require('../factory_plugins/youtrack/comment-wake/comment-wake.js');
Module._load = originalLoad;

const cases = [
  ['no comments', [], false],
  ['missing body', [{}], true],
  ['ordinary comment', [{ text: 'Please fix this.' }], true],
  ['factory marker', [{ text: 'Done. <!-- factory:from dev -->' }], false],
  ['factory ask marker', [{ text: 'Question. <!-- factory:from dev --><!-- factory:ask review -->' }], true],
  ['mixed factory and user comments', [{ text: '<!-- factory:from dev -->' }, { text: 'Please fix this.' }], true],
];

for (const [name, comments, shouldTag] of cases) {
  const tagged = [];
  const added = { size: comments.length, isNotEmpty: () => comments.length > 0, get: i => comments[i] };
  const ctx = { issue: { comments: { added }, addTag: tag => tagged.push(tag) } };
  assert.equal(rule.guard(ctx), comments.length > 0, `${name}: guard`);
  rule.action(ctx);
  assert.deepEqual(tagged, shouldTag ? ['factory:comment'] : [], name);
}
