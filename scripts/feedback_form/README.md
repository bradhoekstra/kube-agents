# External feedback form

A public Google Form that files each submission as an issue on `gke-labs/kube-agents`,
labelled `external-feedback`. `Code.gs` is the Google Apps Script behind it; it runs in
Apps Script, not in this repository, and this file is the record of how it is set up.

The live form is
<https://docs.google.com/forms/d/e/1FAIpQLSfw5eGttWOrii7bvSUmALmRbqpxDRWKmdHoImEZZNe6hOtVtQ/viewform>.
The docs site serves <https://gke-labs.github.io/kube-agents/feedback> as a redirect to it,
configured in `docs/site/astro.config.mjs`; that short link is the one to publish, and the
line above is where to check the long one against if the form is ever recreated.

## Why it exists

The repository is public and Issues are open to any GitHub account, but an
enterprise-managed GitHub account cannot open issues, comment, or fork on any repository
outside its own enterprise. GitHub reports that to the person as a restriction on the
target repository, so people at those companies concluded the tracker was closed to them.
The form gives them a path that needs no GitHub or Google account. The
[contributing guide](../../docs/site/src/content/docs/contributing.md#where-to-file-issues)
is where readers are pointed at it.

## What a submission becomes

An issue on `gke-labs/kube-agents`, opened by the account whose token the script holds,
with:

- the one-line summary as the title, truncated at 120 characters;
- labels `external-feedback` plus `bug`, `enhancement`, or `question` when the reporter
  picked Bug, Feature request, or Question;
- a body that names the reporter as they typed it, links back to the form, and carries the
  free-text answers under headings. Empty optional answers produce no section.

The follow-up email is never posted. It stays in the form's own response store, which only
the form owner sees. Everything else the reporter types is public the moment it is filed,
and the form's description says so.

## Setup

One person owns the form and the script; today that is the maintainer who created it.

1. Open <https://script.google.com>, create a new project, replace the default file with
   `Code.gs`, and save.
2. Run `setup` once from the editor and grant the permissions it asks for: Forms,
   Drive (to create the form), external requests (GitHub), and Mail (failure alerts).
   It logs the share link and the edit link. Re-running it reports the existing form rather
   than creating another. It records the form id in the `FORM_ID` script property as soon
   as the form exists; if the form is ever deleted, remove that property before running
   `setup` again, and it tells you so.
3. Create a GitHub token for the script. A fine-grained personal access token, resource
   owner `gke-labs`, repository access limited to `kube-agents`, permission Issues:
   Read and write, nothing else. Fine-grained tokens expire; set a calendar reminder for the
   expiry, because the first sign of an expired token is the failure email below.
4. In the Apps Script project, Project Settings, Script Properties, add `GITHUB_TOKEN` with
   that value. The token lives only there.
5. Submit the form once yourself and check the issue arrives with the right labels. Close
   that issue.

The `external-feedback` label exists on the repository. If a label named in the script did
not, GitHub would create it on the first issue, default grey and with no description, rather
than fail the call. A misspelt label name therefore shows up as a stray new label, not as
an error.

`setup` opens the form to anyone with the link and no sign-in. If the Workspace domain that
owns the account forbids external sharing, that call throws and the form has to be created
from an account whose domain allows it.

## When filing fails

The trigger emails the form owner the complete issue text and the GitHub error, then
records the failure in the project's Executions view. The submission is also kept in the
form's responses, so nothing is lost; file it by hand from the email and fix the cause.
The usual causes are an expired or revoked token (401), a token without Issues write on
this repository (403), and a GitHub outage.

## Operating it

- **Changing a question.** The script matches answers to questions by the question's title.
  Edit the title in the form UI and in `QUESTIONS` together, or the answer is dropped from
  the issue.
- **Abuse.** Anyone with the link can file, and free text goes into a public issue, including
  any `@mentions` it contains. If that is abused, set the form to require sign-in from its
  settings, or close it to responses; both take effect immediately and neither needs a code
  change. Issues already filed are ordinary issues and can be closed or deleted like any
  other. Every issue is authored by the account that owns the token, so a flood lands on
  that account's record with GitHub as well as on the repository. If volume grows, move the
  token to a machine account with write access to the repository and nothing else.
- **Rotating the token.** Replace the `GITHUB_TOKEN` script property. Nothing else changes.
- **Retiring it.** Close the form to responses, delete the trigger, revoke the token, and
  remove the link from the contributing guide.
