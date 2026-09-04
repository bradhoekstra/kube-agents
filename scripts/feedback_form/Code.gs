/**
 * kube-agents external feedback intake.
 *
 * A Google Form anyone can submit without a Google or GitHub account. Each
 * submission becomes an issue on gke-labs/kube-agents labelled
 * `external-feedback`. This exists because enterprise-managed GitHub accounts
 * cannot open issues on repositories outside their own enterprise, so people
 * at those companies had no way to reach the tracker. README.md next to this
 * file has the setup steps and the operating notes.
 *
 * Runs in Google Apps Script, not in this repository. Paste it into a new
 * project at script.google.com, run `setup` once, then set the GitHub token as
 * a script property.
 */

const REPO = 'gke-labs/kube-agents';
const LABEL = 'external-feedback';
const TOKEN_PROPERTY = 'GITHUB_TOKEN';
const FORM_ID_PROPERTY = 'FORM_ID';
const SUBMIT_HANDLER = 'onFormSubmit';
const FORM_TITLE = 'kube-agents feedback';
const FORM_DESCRIPTION =
  'Bug reports, feature requests, and questions for kube-agents ' +
  '(github.com/gke-labs/kube-agents). Everything you enter here, except the ' +
  'contact email, is posted as a public GitHub issue.';
const CONFIRMATION_MESSAGE =
  'Thanks. Your report is being filed as a public issue on ' +
  'github.com/gke-labs/kube-agents/issues and should appear there within a minute.';
const ISSUES_API_URL = 'https://api.github.com/repos/' + REPO + '/issues';
const GITHUB_API_VERSION = '2022-11-28';
const HTTP_CREATED = 201;
const TITLE_MAX_CHARS = 120;
const NOT_GIVEN = 'not given';
const DEFAULT_TITLE = 'Feedback from the form';

// Form questions in display order. `key` is how onFormSubmit reads an answer,
// `title` is what the responder sees and is also the match key, so a title
// edited in the form UI must be edited here too.
const QUESTIONS = [
  {
    key: 'title',
    title: 'One-line summary',
    type: 'text',
    required: true,
    help: 'Becomes the issue title.',
  },
  {
    key: 'kind',
    title: 'What kind of feedback is this?',
    type: 'choice',
    required: true,
    choices: ['Bug', 'Feature request', 'Question', 'Other'],
  },
  {
    key: 'happened',
    title: 'What happened?',
    type: 'paragraph',
    required: true,
    help: 'What you did and what you saw. For a bug, the steps to reproduce it.',
  },
  {
    key: 'expected',
    title: 'What did you expect instead?',
    type: 'paragraph',
    required: false,
  },
  {
    key: 'environment',
    title: 'Version and environment',
    type: 'text',
    required: false,
    help: 'Chart or image tag, GKE version, model provider. Whatever you know.',
  },
  {
    key: 'extra',
    title: 'Logs, links, or anything else',
    type: 'paragraph',
    required: false,
  },
  {
    key: 'name',
    title: 'Your name or handle',
    type: 'text',
    required: false,
    help: 'Goes in the issue so we can credit you. Leave blank to stay anonymous.',
  },
  {
    key: 'contact',
    title: 'Email for follow-up',
    type: 'text',
    required: false,
    help: 'Never posted to GitHub. Kept only in the form responses, visible to the form owner.',
  },
];

// Which extra label the "kind" answer adds. Unlisted kinds add none.
const KIND_LABELS = {
  Bug: 'bug',
  'Feature request': 'enhancement',
  Question: 'question',
};

// Sections of the issue body, in order: which answer, and its heading.
const BODY_SECTIONS = [
  { key: 'happened', heading: 'What happened' },
  { key: 'expected', heading: 'What was expected' },
  { key: 'environment', heading: 'Version and environment' },
  { key: 'extra', heading: 'Logs, links, or anything else' },
];

/**
 * One-time setup: creates the form, adds the questions, and installs the
 * submit trigger. Re-running it reports the existing form. The form id is
 * recorded the moment the form exists, so a later step throwing (the
 * external-sharing call below is the likely one) leaves a form this
 * function can find and finish rather than an orphan in Drive.
 */
function setup() {
  const props = PropertiesService.getScriptProperties();
  const existing = props.getProperty(FORM_ID_PROPERTY);
  if (existing) {
    let form;
    try {
      form = FormApp.openById(existing);
    } catch (err) {
      throw new Error(
        'Script property ' + FORM_ID_PROPERTY + ' names form ' + existing + ', which cannot be opened (' +
          err + '). If that form was deleted, remove the property and run setup again.'
      );
    }
    Logger.log('Form already exists. Share: %s  Edit: %s', form.getPublishedUrl(), form.getEditUrl());
    return;
  }

  const form = FormApp.create(FORM_TITLE);
  props.setProperty(FORM_ID_PROPERTY, form.getId());
  form.setDescription(FORM_DESCRIPTION);
  form.setConfirmationMessage(CONFIRMATION_MESSAGE);
  // Anyone with the link, no sign-in. This is the whole point: the people this
  // form serves cannot use their work identity here. Throws if the Workspace
  // domain forbids external forms; see README.md.
  form.setRequireLogin(false);
  form.setCollectEmail(false);
  form.setLimitOneResponsePerUser(false);
  form.setAllowResponseEdits(false);

  QUESTIONS.forEach(function (q) {
    addQuestion(form, q);
  });

  ScriptApp.newTrigger(SUBMIT_HANDLER).forForm(form).onFormSubmit().create();

  Logger.log('Share this link: %s', form.getPublishedUrl());
  Logger.log('Edit the form here: %s', form.getEditUrl());
  if (!props.getProperty(TOKEN_PROPERTY)) {
    Logger.log('Now set the %s script property (Project Settings > Script Properties).', TOKEN_PROPERTY);
  }
}

function addQuestion(form, q) {
  let item;
  if (q.type === 'paragraph') {
    item = form.addParagraphTextItem();
  } else if (q.type === 'choice') {
    item = form.addMultipleChoiceItem();
    item.setChoiceValues(q.choices);
  } else {
    item = form.addTextItem();
  }
  item.setTitle(q.title).setRequired(q.required);
  if (q.help) {
    item.setHelpText(q.help);
  }
}

/**
 * Installed trigger: files the submission as a GitHub issue. On failure it
 * emails the form owner the full issue so nothing is lost, then rethrows so
 * the failure also shows in the Apps Script executions log. The submission
 * itself stays in the form's responses either way. A failure to send the
 * email is logged and does not replace the GitHub error being rethrown.
 */
function onFormSubmit(e) {
  const answers = answersByKey(e.response);
  const issue = buildIssue(answers, e.source.getPublishedUrl());
  try {
    const url = createIssue(issue);
    Logger.log('Filed %s', url);
  } catch (err) {
    try {
      notifyOwner(issue, err);
    } catch (mailErr) {
      Logger.log('Could not email the owner about the failure below: %s', mailErr);
    }
    throw err;
  }
}

function answersByKey(response) {
  const keyByTitle = {};
  QUESTIONS.forEach(function (q) {
    keyByTitle[q.title] = q.key;
  });
  const answers = {};
  response.getItemResponses().forEach(function (r) {
    const key = keyByTitle[r.getItem().getTitle()];
    if (key) {
      answers[key] = String(r.getResponse() || '').trim();
    }
  });
  return answers;
}

function buildIssue(answers, formUrl) {
  // Truncate on code points, not UTF-16 units, so a 120-unit cut cannot
  // split an emoji into a lone surrogate.
  const title = Array.from(answers.title || DEFAULT_TITLE).slice(0, TITLE_MAX_CHARS).join('');
  const reporter = answers.name || NOT_GIVEN;
  const labels = [LABEL];
  if (Object.prototype.hasOwnProperty.call(KIND_LABELS, answers.kind)) {
    labels.push(KIND_LABELS[answers.kind]);
  }

  const lines = [
    '_Filed from the [feedback form](' + formUrl + ') on behalf of an outside reporter. ' +
      'Reporter: ' + reporter + '. Contact details, if given, are in the form responses, not here._',
    '',
    '**Kind:** ' + (answers.kind || NOT_GIVEN),
  ];
  BODY_SECTIONS.forEach(function (s) {
    const text = answers[s.key];
    if (text) {
      lines.push('', '## ' + s.heading, '', text);
    }
  });

  return { title: title, body: lines.join('\n'), labels: labels };
}

function createIssue(issue) {
  const token = PropertiesService.getScriptProperties().getProperty(TOKEN_PROPERTY);
  if (!token) {
    throw new Error('Script property ' + TOKEN_PROPERTY + ' is not set. See README.md.');
  }
  const res = UrlFetchApp.fetch(ISSUES_API_URL, {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + token,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': GITHUB_API_VERSION,
    },
    payload: JSON.stringify(issue),
    muteHttpExceptions: true,
  });
  if (res.getResponseCode() !== HTTP_CREATED) {
    throw new Error('GitHub returned ' + res.getResponseCode() + ': ' + res.getContentText());
  }
  return JSON.parse(res.getContentText()).html_url;
}

function notifyOwner(issue, err) {
  const to = Session.getEffectiveUser().getEmail();
  const body =
    'The feedback form could not file this issue on ' + REPO + '.\n\n' +
    'Error: ' + err + '\n\n' +
    'File it by hand:\n\n' +
    'Title: ' + issue.title + '\n' +
    'Labels: ' + issue.labels.join(', ') + '\n\n' +
    issue.body;
  MailApp.sendEmail(to, '[kube-agents feedback form] failed to file: ' + issue.title, body);
}
