# Merge per-PR `gh pr view` detail onto the fetched PR list, and parse
# Renovate's PR body into structured update records.
#
# Run as: jq -s -f enrich.jq <fetched.json> <details.json>
#   .[0] = fetch-renovate-prs.sh output
#   .[1] = one detail record per PR (repo, number, body, labels, files,
#          statusCheckRollup, mergeable, mergeStateStatus)
#
# A PR with no matching detail record (deleted branch, permissions) is dropped.

def trim: gsub("^\\s+|\\s+$"; "");
def nums: [match("[0-9]+"; "g").string] | map(tonumber);

# Classify a from->to version change by the first numeric component that
# differs: index 0 = major, 1 = minor, deeper = patch.
def bump_of($f; $g):
  if ($f | not) or ($g | not) then "other"
  else ($f | nums) as $a | ($g | nums) as $b
  | ( [range(0; [($a | length), ($b | length)] | max)]
      | map(select(($a[.] // -1) != ($b[.] // -1))) | first ) as $idx
  | if   $idx == null then "none"
    elif $idx == 0    then "major"
    elif $idx == 1    then "minor"
    else                   "patch" end
  end;

# Renovate renders one markdown table row per updated package:
#   | [pkg](link) ([changelog](link)) | `>=49,<50` -> `>=50,<51` | age | conf |
# The version cell holds whatever the manifest expresses -- a bare semver, a
# constraint range, or a digest -- so never assume it parses as a semver.
def parse_row:
  (. / "|") as $cell
  | (($cell[1] // "") | trim) as $pkg
  | (($cell[2] // "") | trim) as $chg
  | (($chg | [match("`([^`]*)`"; "g")]) | map(.captures[0].string)) as $vers
  | { package: (([$pkg | match("\\[([^\\]]+)\\]")] | first | .captures[0].string) // $pkg | trim),
      from: ($vers | first),
      to:   ($vers | last),
      change: $chg }
  | . + { bump: bump_of(.from; .to) };

def parse_updates:
  (. // "") | split("\n")
  | map(select(startswith("|") and (test("→|->"))))
  | map(parse_row)
  | map(select(.package | length > 0));

# Manifest path -> GitHub Advisory Database ecosystem. Helm charts, plain
# Dockerfile FROM bumps and docker-compose images have NO advisory
# ecosystem -- they must be reported as unscored, not silently treated safe.
def ecosystem_of:
  if   test("(^|/)(pyproject\\.toml|uv\\.lock|poetry\\.lock|Pipfile(\\.lock)?|setup\\.(py|cfg)|requirements[^/]*\\.txt)$") then "PIP"
  elif test("(^|/)(package\\.json|package-lock\\.json|yarn\\.lock|pnpm-lock\\.yaml|bun\\.lock(b)?)$") then "NPM"
  elif test("(^|/)go\\.(mod|sum)$")                       then "GO"
  elif test("(^|/)(Gemfile(\\.lock)?|[^/]*\\.gemspec)$")  then "RUBYGEMS"
  elif test("(^|/)Cargo\\.(toml|lock)$")                  then "RUST"
  elif test("(^|/)(pom\\.xml|build\\.gradle(\\.kts)?)$")  then "MAVEN"
  elif test("(^|/)composer\\.(json|lock)$")               then "COMPOSER"
  elif test("(^|/)([^/]*\\.csproj|packages\\.config)$")   then "NUGET"
  elif test("(^|/)pubspec\\.(yaml|lock)$")                then "PUB"
  elif test("^\\.github/workflows/")                      then "ACTIONS"
  else null end;

.[0] as $base | .[1] as $detail
| $base
| map(. as $pr
    | ($detail[] | select(.repo == $pr.repo and .number == $pr.number)) as $d
    | $pr + {
        body: ($d.body // ""),
        labels: [($d.labels // [])[].name],
        files: [($d.files // [])[].path],
        ecosystems: ([($d.files // [])[].path | ecosystem_of] | map(select(. != null)) | unique),
        # Renovate embeds the advisory IDs it is fixing directly in the body.
        # This is far more precise than inferring them from package+version,
        # and it is what the advisory lookup keys off.
        ghsa_ids: ([$d.body // "" | match("GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}"; "g").string] | unique),
        cve_ids:  ([$d.body // "" | match("CVE-[0-9]{4}-[0-9]+"; "g").string] | unique),
        updates: ($d.body // "" | parse_updates),
        checks: ([($d.statusCheckRollup // [])[]
                  | (.conclusion // .state // "") | ascii_downcase]
                 | { failing: (map(select(. == "failure" or . == "timed_out" or . == "startup_failure")) | length),
                     pending: (map(select(. == "" or . == "pending" or . == "in_progress" or . == "queued")) | length),
                     passing: (map(select(. == "success" or . == "neutral" or . == "skipped")) | length) }),
        mergeable: ($d.mergeable // "UNKNOWN"),
        mergeStateStatus: ($d.mergeStateStatus // "UNKNOWN")
      }
    | . + { grouped: ((.updates | length) > 1),
            # Not every Renovate PR bumps a package. "Lock file maintenance"
            # renders a different table (| Update | Change | with no version
            # arrow) and "Pin dependencies" only adds exact pins, so both
            # parse to zero updates -- that is correct, not a parse failure.
            kind: (if   ($d.body // "") | test("lockFileMaintenance") then "lockfile_maintenance"
                   elif ($pr.title | test("pin dependencies"; "i"))   then "pin"
                   else "dependency" end),
            # A grouped PR has one bump per member; the PR as a whole is as
            # risky as its riskiest member.
            bump: ( [.updates[].bump]
                    | if   index("major") then "major"
                      elif index("minor") then "minor"
                      elif index("patch") then "patch"
                      elif length == 0    then "unknown"
                      else "other" end ) })
