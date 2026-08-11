# Fold the contributionsCollection buckets and the search-derived repo list
# into one ranked [{repo, signals, total}] array.
#
# Run as: jq -n --arg org <org> --argjson contrib <collection>
#               --argjson searched <[repo,...]> --argjson min <N>
#               -f active-repos.jq

def tally($key):
  ($contrib[$key] // [])
  | map({repo: .repository.nameWithOwner, n: .contributions.totalCount});

( [ (tally("commitContributionsByRepository")            | map(. + {kind: "commits"})),
    (tally("pullRequestContributionsByRepository")        | map(. + {kind: "prs"})),
    (tally("pullRequestReviewContributionsByRepository")   | map(. + {kind: "reviews"})),
    (tally("issueContributionsByRepository")               | map(. + {kind: "issues"})),
    ($searched | group_by(.) | map({repo: .[0], n: length, kind: "searched"}))
  ] | add )

# contributionsCollection is account-wide, not org-scoped, so a personal repo
# like <login>/scratch comes back too. Filter to the requested org.
| map(select(.repo | startswith($org + "/")))
| group_by(.repo)
| map({
    repo: .[0].repo,
    signals: (map({(.kind): .n}) | add
              | {commits: (.commits // 0), prs: (.prs // 0),
                 reviews: (.reviews // 0), issues: (.issues // 0),
                 searched: (.searched // 0)}),
    # `searched` overlaps the other signals, so it is deliberately excluded
    # from the total -- it exists to admit repos, not to inflate ranking.
    total: (map(select(.kind != "searched") | .n) | add // 0)
  })
| map(select(.total >= $min or .signals.searched > 0))
| sort_by(-.total)
