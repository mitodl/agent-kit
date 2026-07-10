# GraphQL reference

`gh pr view` / `gh api repos/.../pulls/.../comments` (REST) can tell you a
comment's text and position, but **not** whether its review thread is
resolved — that's a GraphQL-only concept. This skill's scripts wrap the two
queries below so you rarely need to write GraphQL by hand; this doc is for
when you need to debug them or do something the scripts don't cover.

## Fetching review threads, with pagination

`reviewThreads` is paginated like everything else in the GitHub GraphQL API —
a long-running PR with many rounds of review can exceed a single page. Fixed
`first: N` with no cursor-following (a pattern that shows up in a lot of
one-off `gh api graphql` snippets) silently truncates once a PR passes that
count. `fetch-feedback.sh` follows `pageInfo.hasNextPage`/`endCursor` in a
loop so it doesn't matter how many rounds of review a PR has been through:

```graphql
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id            # GraphQL node ID, e.g. "PRRT_kwDO..." -- pass this to resolve-thread.sh
          isResolved
          isOutdated    # true if the diff has moved since the comment was made
          path
          line
          comments(first: 50) {
            nodes { databaseId body author { login } createdAt }
          }
        }
      }
    }
  }
}
```

`comments(first: 50)` inside a thread is not cursor-paginated by the
scripts — a single review thread with 50+ replies is not a realistic case in
practice. If you ever hit it, extend the query with the same
`pageInfo`/`after` pattern one level down.

`isOutdated: true` means the diff has changed since the comment was left —
the line numbers may no longer point at the code being discussed. Still
requires judgment: the underlying concern may still apply even if the exact
line moved, or it may have been resolved incidentally by other changes. Don't
treat `isOutdated` as "safe to ignore."

## Resolving a thread

Resolving and replying are two different mutations. Replying alone does
**not** resolve the thread, and there is no REST equivalent for resolution —
this is why raw `gh api repos/.../pulls/.../comments/{id}/replies` (a REST
POST) never shows up as "resolved" in the PR UI even though it looks like a
reply.

```graphql
mutation($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $threadId, body: $body}) {
    comment { id url }
  }
}
```

```graphql
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
```

`resolve-thread.sh` runs the reply mutation first (only if `--comment` was
given) and then the resolve mutation, so the reply is visible before the
thread flips to resolved.

## Permission errors

`resolveReviewThread` requires the authenticated `gh` user to be the PR
author, a participant in the thread, or a repo maintainer with write access.
A permission error here usually means `gh auth status` is authenticated as
an account that isn't a collaborator on the target repo — check that before
assuming the mutation itself is wrong.

## Discussion comments vs. review threads

Top-level PR conversation comments (`gh api repos/.../issues/{n}/comments` —
yes, PR discussion comments live under the *issues* REST endpoint, not
`pulls`) have no thread/resolution concept at all. You can only reply to
them with a new top-level comment (`reply-comment.sh`, i.e. `gh pr comment`);
there's nothing to mark resolved.
