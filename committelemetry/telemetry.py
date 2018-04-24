# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""
Functions for calculating commit telemetry and serializing the results.
"""
import re
from enum import Enum

from mozautomation.commitparser import parse_bugs
import requests

# Bugzilla attachment types
ATTACHMENT_TYPE_MOZREVIEW = 'text/x-review-board-request'
ATTACHMENT_TYPE_GITHUB = 'text/x-github-request'
ATTACHMENT_TYPE_PHABRICATOR = 'text/x-phabricator-request'

# FIXME turn this into an environment variable
BMO_API_URL = 'https://bugzilla.mozilla.org/rest'

# Match "Differential Revision: https://phabricator.services.mozilla.com/D861"
PHABRICATOR_COMMIT_RE = re.compile(
    'Differential Revision: ([\w:/.]*)(D[0-9]{3,})'
)

# Match "Backed out 4 changesets (bug 1448077) for xpcshell failures at..."
BACKOUT_RE = re.compile('^backed out ', re.IGNORECASE)


class ReviewSystem(Enum):
    """The review system used for a commit.

    Enum values are serialized for sending as telemetry.
    """
    phabricator = 'phabricator'
    mozreview = 'mozreview'
    bmo = 'bmo'
    unknown = 'unknown'
    not_applicable = 'not_applicable'


def is_patch(attachment):
    """Is the given BMO attachment JSON for a patch attachment?"""
    # Example: https://bugzilla.mozilla.org/rest/bug/1447193/attachment?exclude_fields=data
    return (
        attachment['is_patch'] == 1 or attachment['content_type'] in (
            ATTACHMENT_TYPE_MOZREVIEW,
            ATTACHMENT_TYPE_GITHUB,
            ATTACHMENT_TYPE_PHABRICATOR,
        )
    )


def fetch_attachments(bug_id):
    # Example: https://bugzilla.mozilla.org/rest/bug/1447193/attachment?exclude_fields=data
    url = f'{BMO_API_URL}/bug/{bug_id}/attachment?exclude_fields=data'
    response = requests.get(url)
    response.raise_for_status()
    attachments = response.json()['bugs'][str(bug_id)]
    return attachments


def fetch_bug_history(bug_id):
    # Example: https://bugzilla.mozilla.org/rest/bug/1447193/history
    url = f'{BMO_API_URL}/bug/{bug_id}/history'
    response = requests.get(url)
    response.raise_for_status()
    history = response.json()['bugs'][0]['history']
    return history


def has_phab_markers(revision_description):
    return bool(re.search(PHABRICATOR_COMMIT_RE, revision_description))


def has_backout_markers(revision_description):
    return bool(re.search(BACKOUT_RE, revision_description))


def has_merge_markers(revision_json):
    # If the node has more than one parent then it's a merge.
    return len(revision_json['parents']) > 1


def has_mozreview_markers(attachments):
    patches = [a for a in attachments if is_patch(a)]
    for patch_attachment in patches:
        if patch_attachment['content_type'] == ATTACHMENT_TYPE_MOZREVIEW:
            return True
    return False


def has_bmo_patch_review_markers(attachments, bug_history):
    # 1. Does this bug have at least one patch attached?
    # Check for raw patches only, not x-phabricator-request or similar
    # patch attachments.
    patches = [a for a in attachments if a['is_patch'] == 1]
    if not patches:
        return False

    # 2. Does this bug have a review+ flag?
    # Don't balance review? and review+ changes, just assume that if there is
    # one review+ flag then a review was complete and the change landed based
    # on it.
    for change_group in bug_history:
        for change in change_group['changes']:
            if change['field_name'] != 'flagtypes.name':
                continue
            flags_added = change['added'].split(',')
            if 'review+' in flags_added:
                return True

    return False


def payload_for_changeset(changesetid, repo_url):
    """Build a telemetry.mozilla.org ping payload for the given changeset ID.

    The payload conforms to the 'commit-pipeline/mozilla-central-commit' schema.

    Args:
        changesetid: The 40 hex char changeset ID for the given repo.
        repo: The URL of the repo the changeset lives in.

    Returns:
        A dict that can be turned into JSON and posted to the
        telemetry.mozilla.org service.
    """
    # Example URL: https://hg.mozilla.org/mozilla-central/json-rev/deafa2891c61
    response = requests.get(f'{repo_url}/json-rev/{changesetid}')

    if response.status_code == 404:
        raise NoSuchChangeset(
            f'The changeset {changesetid} does not exist in repository {repo_url}'
        )

    response.raise_for_status()

    system = determine_review_system(response.json())

    return {'changesetID': changesetid, 'reviewSystemUsed': system.value}


def determine_review_system(revision_json):
    summary = revision_json['desc']

    # 0. Check for changesets that don't need review.
    if has_backout_markers(summary) or has_merge_markers(revision_json):
        return ReviewSystem.not_applicable

    # 1. Check for Phabricator because it's easiest.
    # TODO can we rely on BMO attachments for this?
    if has_phab_markers(summary):
        return ReviewSystem.phabricator

    # TODO handle multiple bugs?
    try:
        bug_id = parse_bugs(summary).pop()
    except IndexError:
        # We couldn't find a bug ID in the summary.
        # FIXME: how do we account for these in analytics?
        return ReviewSystem.unknown

    attachments = fetch_attachments(bug_id)
    bug_history = fetch_bug_history(bug_id)

    # 2. Check BMO for MozReview review markers because that's next-easiest.
    if has_mozreview_markers(attachments):
        return ReviewSystem.mozreview

    # 3. Check for a review using just BMO attachments, e.g. splinter
    if has_bmo_patch_review_markers(attachments, bug_history):
        return ReviewSystem.bmo

    return ReviewSystem.unknown


class Error(Exception):
    """Generic error class for this module."""


class NoSuchChangeset(Error):
    """Raised if the given changeset ID does not exist in the target system."""


# Test revs:
# Reviewed with BMO: deafa2891c61a4570bcadb80b90adac0930b1d10
# https://hg.mozilla.org/mozilla-central/rev/deafa2891c61

# Reviewed with Phabricator: e0cb209d9f3f307826944eae2d552b5a5bbe83e4
# https://hg.mozilla.org/mozilla-central/rev/e0cb209d9f3f

# Reviewed with MozReview: 2926745a0fee53547f6e464321cbe4915c2fff7f
# https://hg.mozilla.org/mozilla-central/rev/2926745a0fee

# Backed out single change
# https://hg.mozilla.org/mozilla-central/rev/b5065c61bbd7

# Backed out multiple revs
# https://hg.mozilla.org/mozilla-central/rev/daa5f1f165ed